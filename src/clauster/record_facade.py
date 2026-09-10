"""Notify / webhook / session-event surface for the bridge lifecycle (part of #1157).

:class:`RecordFacade` is the third collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157). It owns the three
best-effort, fire-and-forget lifecycle sinks — the session-event history append,
the outbound lifecycle webhook, and the outbound notification — plus the single
chokepoint (:meth:`_emit_lifecycle`) the runner calls on every spawn / ready /
stop / crash transition.

Unlike :class:`~clauster.bridge_launch.BridgeLaunch` and
:class:`~clauster.bridge_prune.BridgePrune` (config + paths only), this collaborator
holds mutable runtime state, so a few ownership rules are load-bearing:

- **Task ownership (async).** The fire-and-forget emitters (:meth:`_notify_event`,
  :meth:`_record_event`, :meth:`_emit_webhook`, :meth:`emit_event`,
  :meth:`notify_app_event`) add their task to :attr:`_notify_tasks` and register
  ``task.add_done_callback(self._notify_tasks.discard)``, exactly as on the runner.
  ``RecordFacade`` OWNS that ONE set; ``SessionRunner`` exposes it read-only as
  ``self._notify_tasks`` (a property) and ``SessionRunner.shutdown()`` drains THIS set
  (grace ``_NOTIFY_DRAIN_GRACE``). No emitter's task escapes the set, so none is lost at
  drain / GC-cancelled at interpreter exit. Correctness therefore depends on the runner
  holding EXACTLY ONE ``RecordFacade`` (built once in ``SessionRunner.__init__`` as
  ``self._record``).
- **Session-ref signing (security-adjacent).** :meth:`_session_ref_key` derives the
  HMAC key for the webhook / history ``session_ref`` from the per-deployment session
  secret, loaded once and cached in :attr:`_session_ref_secret`. The derivation is moved
  BYTE-IDENTICAL: same :func:`clauster.auth.load_or_create_secret` source, same
  ephemeral-fallback, and the same :func:`_hash_session_ref` HMAC-SHA256 truncation — so
  every previously-issued ``session_ref`` still verifies and redaction matching is
  unaffected. ``auth`` is imported as the module object so a
  ``clauster.runner.auth.load_or_create_secret`` monkeypatch still lands here.
- **``_history`` is NOT owned here.** The run-history store stays owned by
  ``SessionRunner`` (it is not extracted until a later PR of #1157); it is passed in once
  as an injected reference (:attr:`_history`), never duplicated. Tests that swap
  ``runner._history.append`` therefore still reach the same object.
- **No registry.** The emitters take an ``instance: RemoteControlInstance`` argument, so
  this collaborator never reads ``_instances``. The project-path lookup a terminal history
  row needs is injected as the ``project_path`` callable (``SessionRunner._project_path``),
  mirroring how ``BridgeLaunch`` receives ``stderr_path_for``.

The runner delegates the moved methods to the single instance, preserving the exact
public signatures (and the ``staticmethod`` form of :meth:`_notify_message`) that the
tests call directly on the runner. :meth:`emit_event` and :meth:`notify_app_event` are
the public members of that surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

from . import auth
from .config import ClausterConfig
from .db.stores import CostSnapshot, SessionHistoryStore
from .models import RemoteControlInstance
from .notify import Notifier
from .usage import ProjectUsage, aggregate_project_usage_cached
from .webhooks import WebhookEmitter

_log = logging.getLogger("clauster.record_facade")


def _hash_session_ref(session_id: str | None, secret: bytes) -> str | None:
    """Return a stable, non-reversible correlation token for a starter session id.

    ``None`` in, ``None`` out. Otherwise a 16-hex-char (64-bit) HMAC-SHA256 prefix
    keyed by a per-deployment ``secret``: stable across an instance's lifecycle
    events so a webhook receiver can group them, but it never carries the
    bearer-equivalent ``session_<ULID>`` itself (which redaction strips from every
    other egress surface — see ``redact.py``). Keying with the secret (rather than a
    bare SHA-256) means a receiver can't even *verify* a guessed session id against
    the token without the secret — matching how ``session_<ULID>`` is treated as a
    bearer credential everywhere else.
    """
    if not session_id:
        return None
    return hmac.new(secret, session_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


class RecordFacade:
    """Own the notify / webhook / session-event sinks for the bridge lifecycle."""

    # Maps the internal webhook event name to the persisted history ``kind``.
    _HISTORY_KIND = {"spawn": "spawned", "ready": "ready", "stop": "ended", "crash": "crashed"}

    def __init__(
        self,
        *,
        config: ClausterConfig,
        history: SessionHistoryStore,
        project_path: Callable[[str], Path | None],
        claude_projects_dir: Path,
    ) -> None:
        """Bind to config + the injected history store, project-path lookup, and transcript dir.

        ``history`` is :meth:`SessionRunner._history` (kept owned by the runner — not
        extracted until a later PR of #1157) passed by reference, never duplicated.
        ``project_path`` is :meth:`SessionRunner._project_path`, injected so a terminal
        history row's cost snapshot can resolve the project without this collaborator
        ever holding ``_instances``. ``claude_projects_dir`` locates the per-session
        transcripts the cost/token rollup reads.
        """
        self._config = config
        self._history = history
        self._project_path = project_path
        self._claude_projects_dir = claude_projects_dir
        # Best-effort outbound notifications (Apprise; optional extra). No-op unless
        # enabled + configured + Apprise installed. Fire-and-forget crash alerts are
        # tracked in `_notify_tasks` so the tasks aren't garbage-collected mid-flight.
        self._notifier = Notifier(config.notifications)
        self._notify_tasks: set[asyncio.Task] = set()
        # Outbound lifecycle webhooks (#371). Fail-open and fire-and-forget, sharing the
        # GC-safety task set above; no-op unless enabled with a usable url.
        self._webhooks = WebhookEmitter(config.webhooks)
        # Lazily-loaded per-deployment key for the webhook session_ref HMAC (#408).
        # Loaded on first webhook emit, not at construction, so a bad
        # CLAUSTER_SESSION_SECRET can't break runner construction or the bridge
        # lifecycle (webhooks are fail-open by design).
        self._session_ref_secret: bytes | None = None

    @staticmethod
    def _notify_message(event: str, instance: RemoteControlInstance) -> tuple[str, str]:
        """Build the (title, body) for a bridge-lifecycle notification ``event`` (#541)."""
        mode = f"{instance.resume_mode}/{instance.spawn_mode}"
        proj = repr(instance.project)
        bodies = {
            "crash": (
                f"clauster: bridge crashed — {instance.label}",
                f"The bridge for project {proj} exited unexpectedly (not via Stop) — mode {mode}.",
            ),
            "ready": (
                f"clauster: bridge ready — {instance.label}",
                f"The bridge for project {proj} finished starting — mode {mode}.",
            ),
            "stop": (
                f"clauster: bridge stopped — {instance.label}",
                f"The bridge for project {proj} was stopped — mode {mode}.",
            ),
            "session-ended": (
                f"clauster: session ended — {instance.label}",
                f"The session for project {proj} ended — mode {mode}.",
            ),
            "reconnect-failed": (
                f"clauster: reconnect failed — {instance.label}",
                f"Resuming the bridge for project {proj} failed — mode {mode}.",
            ),
        }
        return bodies.get(
            event,
            (f"clauster: {event} — {instance.label}", f"Project {proj} — mode {mode}."),
        )

    def _notify_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle notification (fire-and-forget, #541).

        ``event`` is a key of :data:`~clauster.config._NOTIFY_EVENTS`. No-op unless the
        outbound notifier is active AND this event's per-event toggle is on. Routes
        through the same fire-and-forget Apprise path the crash alert always used.

        Must be called on the event loop (it schedules a task); the send itself runs
        off-thread, never blocks, and swallows its own errors.
        """
        if not self._notifier.active or not self._config.notifications.event_enabled(event):
            return
        title, body = self._notify_message(event, instance)
        # Fire-and-forget: anotify sends off-thread and swallows its own errors. Keep a
        # reference so the task isn't GC'd mid-send; drop it on completion.
        task = asyncio.create_task(self._notifier.anotify(title, body))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _session_ref_key(self) -> bytes:
        """Return the per-deployment HMAC key for ``session_ref``, loading it once.

        Reuses the session-signing secret so the correlation token is unverifiable
        without it. Fail-open: if the secret can't be loaded (e.g. a misconfigured
        ``CLAUSTER_SESSION_SECRET``), fall back to a process-stable random key so the
        webhook still emits a non-reversible token and the bridge lifecycle is never
        affected — webhooks are best-effort by design.
        """
        if self._session_ref_secret is None:
            try:
                self._session_ref_secret = auth.load_or_create_secret(self._config.state_dir)
            except Exception:
                # Never let a secret-load error break a fire-and-forget webhook.
                _log.warning(
                    "webhook session_ref: session secret unavailable; using an "
                    "ephemeral per-process key (correlation works within this run only)"
                )
                self._session_ref_secret = secrets.token_bytes(32)
        return self._session_ref_secret

    def _emit_lifecycle(self, event: str, instance: RemoteControlInstance) -> None:
        """Single chokepoint for a lifecycle transition: history + webhook + notification.

        ``event`` is one of ``spawn`` / ``ready`` / ``stop`` / ``crash``. All three sinks
        are best-effort and off the loop, so none can affect the bridge lifecycle: the
        history append is fail-closed (a lost row is logged, never raised), the webhook is
        fail-open (a broken endpoint is swallowed), and the notification is fire-and-forget.

        Notifications use a finer-grained event taxonomy than webhooks (#541): a ``stop``
        for a single-shot ``session`` bridge that wasn't ended via the Stop button is a
        ``session-ended`` notification, not a ``stop``. ``spawn`` carries no notification.
        """
        self._record_event(event, instance)
        self._emit_webhook(event, instance)
        notify_event = event
        if event == "stop" and instance.spawn_mode == "session" and not instance.intentional_stop:
            notify_event = "session-ended"
        if notify_event != "spawn":
            self._notify_event(notify_event, instance)

    def _record_event(self, event: str, instance: RemoteControlInstance) -> None:
        """Append a session-history row for ``event`` off-loop (#363; best-effort).

        Snapshots the loop-owned instance fields *here* (the worker thread must never
        read ``self._instances``), then does the transcript parse + DB write in a
        background task. A terminal (``stop`` / ``crash``) event carries the project's
        cumulative end-of-session cost/token snapshot from :mod:`clauster.usage`;
        non-terminal rows carry no cost. Any failure is swallowed by the store — a
        lost history row never affects a spawn or stop.

        Called on the event loop, through :meth:`_emit_lifecycle`. The no-running-loop
        branch is a defensive fallback (a direct best-effort synchronous append) that no
        current caller reaches — ``_emit_lifecycle``'s other two sinks would raise out of
        ``asyncio.create_task`` on that path whenever webhooks/notifications are enabled.

        Fail-closed end-to-end: the cheap in-memory prologue, the off-loop cost
        snapshot, and the append are all logged-and-swallowed, on both the async and
        the synchronous path, so a history hiccup never raises into the spawn/stop/
        crash caller. The cost-snapshot's project-path lookup + transcript read both
        live inside ``_usage_snapshot`` so a filesystem hiccup degrades the *cost* to
        null without dropping the row itself — the terminal row is always recorded.
        """
        kind = self._HISTORY_KIND.get(event)
        if kind is None:  # unknown event name — never persist a bogus kind
            return

        def _append(
            project: str, mode: str, session_ref: str | None, usage: ProjectUsage | None
        ) -> None:
            """Append one session-history row, attaching a cost snapshot when usage is known."""
            totals = usage.totals if usage is not None else None
            cost = (
                CostSnapshot(
                    cost_usd=usage.cost_usd(),
                    input_tokens=totals.input if totals is not None else None,
                    output_tokens=totals.output if totals is not None else None,
                    cache_creation_tokens=totals.cache_creation if totals is not None else None,
                    cache_read_tokens=totals.cache_read if totals is not None else None,
                )
                if usage is not None
                else CostSnapshot()
            )
            self._history.append(
                project_name=project,
                mode=mode,
                kind=kind,
                session_ref=session_ref,
                cost=cost,
            )

        try:
            # "hosted" sessions run on the claustrum channel; otherwise the resume axis
            # (standard remote-control vs the pty keeper) is the mode worth recording.
            # Fall back to "standard" if the resume axis is somehow unresolved: ``mode``
            # is NOT NULL, so a None here would make the INSERT drop the row entirely.
            mode = (
                "hosted" if instance.channel == "hosted" else (instance.resume_mode or "standard")
            )
            project = instance.project
            # Snapshot the loop-owned values now; the off-loop task only touches locals.
            session_ref = _hash_session_ref(instance.starter_session_id, self._session_ref_key())
            terminal = kind in ("ended", "crashed")
        except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
            _log.warning(
                "could not prepare session event (%s/%s): %s", instance.project, kind, exc
            )
            return

        def _usage_snapshot() -> ProjectUsage | None:
            """Cost/token rollup for a terminal row, or None (non-terminal / unreadable).

            The project-path lookup (``_project_path`` walks the filesystem) and the
            transcript read both live here, so a discovery / transcript I/O error
            degrades the *cost* to null — the terminal row is still written by the
            caller. This is the documented "an unreadable transcript must not drop the
            terminal row" invariant: only the cost is best-effort, never the row.
            """
            if not terminal:
                return None
            try:
                project_path = self._project_path(project)
                if project_path is None:
                    return None
                return aggregate_project_usage_cached(
                    project_path,
                    project_name=project,
                    claude_projects_dir=self._claude_projects_dir,
                )
            except Exception as exc:  # noqa: BLE001 — cost is best-effort; the row is not
                # OSError from a transcript/discovery read, ValueError from a malformed
                # transcript, etc. — any of them degrades the cost only, never the row.
                _log.warning("session-history cost snapshot failed for %s: %s", project, exc)
                return None

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (a synchronous status-apply call path): do the snapshot +
            # append inline. Best-effort — the store's append already fails closed, so a
            # DB error is swallowed there; guard the snapshot's own non-OSError too.
            try:
                _append(project, mode, session_ref, _usage_snapshot())
            except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
                _log.warning("could not record session event (%s/%s): %s", project, kind, exc)
            return

        async def _write() -> None:
            """Snapshot usage and append the event off-thread, never raising into the loop."""
            # Mirror the sync path's swallow-and-log so a parser/DB error surfaces as a
            # tidy warning, not asyncio's "Task exception was never retrieved" noise.
            try:
                usage = await asyncio.to_thread(_usage_snapshot)
                await asyncio.to_thread(_append, project, mode, session_ref, usage)
            except Exception as exc:  # noqa: BLE001 — history must never break the lifecycle
                _log.warning("could not record session event (%s/%s): %s", project, kind, exc)

        task = asyncio.create_task(_write())
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def _emit_webhook(self, event: str, instance: RemoteControlInstance) -> None:
        """Fire a best-effort lifecycle webhook (fire-and-forget, fail-open, #371).

        ``event`` is one of ``spawn`` / ``ready`` / ``stop`` / ``crash``. No-op unless
        webhooks are active and this event is enabled. Must be called on the event loop
        (it schedules a task); the POST itself never blocks or raises — a slow or broken
        endpoint can't affect the bridge lifecycle.
        """
        if not self._webhooks.wants(event):
            return
        payload = {
            "project": instance.project,
            "label": instance.label,
            "status": instance.status.value,
            "resume_mode": instance.resume_mode,
            "spawn_mode": instance.spawn_mode,
            # Item-8 (#408): the raw starter_session_id is a session_<ULID> that
            # redaction treats as bearer-equivalent everywhere else (anyone holding
            # it can open a New Session composer for the bridge) — see redact.py and
            # the WS log-stream stripping. Egressing it raw to an arbitrary operator
            # webhook endpoint is the same leak that surface forbids, so we send a
            # hashed, non-reversible correlation token instead: a receiver can still
            # correlate the spawn/ready/stop/crash events of one session without ever
            # holding the credential-equivalent value. Keyed with a per-deployment
            # secret so it can't even be VERIFIED against a guessed session id.
            "session_ref": _hash_session_ref(instance.starter_session_id, self._session_ref_key()),
        }
        task = asyncio.create_task(self._webhooks.aemit(event, payload))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def emit_event(self, event: str, payload: dict) -> None:
        """Fire a non-bridge lifecycle webhook off-loop (fire-and-forget, fail-open, #432).

        The runner owns the single :class:`WebhookEmitter`, so subsystems that don't
        hold one (the bg-agent supervisor, the hosted manager, the clone manager) route
        their lifecycle events through here. ``event`` is a #432 key — ``bg-settled`` /
        ``permission-needed`` / ``clone-done`` — and ``payload`` is an already-redacted,
        event-shaped dict (NOT the bridge ``RemoteControlInstance`` shape). No-op unless
        webhooks are active and this event is enabled (these default OFF). The POST is
        fire-and-forget and fail-open — it can never block or break the caller's path.

        Must be called on the event loop (it schedules a task). Callers off the loop
        (e.g. the threaded supervisor stop) marshal via ``loop.call_soon_threadsafe``.
        """
        if not self._webhooks.wants(event):
            return
        task = asyncio.create_task(self._webhooks.aemit(event, payload))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    def notify_app_event(self, event: str, title: str, body: str) -> None:
        """Fire a non-bridge notification off-loop (fire-and-forget, fail-closed, #541).

        Mirrors :meth:`emit_event` for the *notification* channel: subsystems that
        don't hold the runner's :class:`Notifier` (the hosted manager's parked-prompt
        callback, the resume route) route an event whose source isn't a bridge
        ``RemoteControlInstance`` — e.g. ``permission-needed`` / ``reconnect-failed`` —
        through here with a ready-made title/body. No-op unless the outbound notifier is
        active and this event's per-event toggle is on (these default OFF). The send is
        fire-and-forget and swallows its own errors, so it never affects the caller.

        Must be called on the event loop (it schedules a task).
        """
        if not self._notifier.active or not self._config.notifications.event_enabled(event):
            return
        task = asyncio.create_task(self._notifier.anotify(title, body))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)
