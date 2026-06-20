"""Read, dispatch, and stop the agent-view supervisor's background sessions (bg-agents panel).

Claude Code's agent view (2.1.139+, research preview) hosts ``claude --bg``
background sessions under a per-user supervisor daemon. Its on-disk surface is
listed on the agent-view docs page — an acknowledged interface, not
reverse-engineering:

* ``~/.claude/jobs/<id>/state.json`` — per-session state (what agent view renders)
* ``~/.claude/daemon/roster.json``   — live workers (pid + procStart), kept by the
  supervisor for its own reconnect-after-restart

This module lists jobs for the dashboard (:func:`list_background_jobs`),
dispatches new ``claude --bg`` jobs (:func:`dispatch_background_job`), and stops
running ones (:func:`stop_background_job`). Conventions applied on the way in:

* **Tolerant per job, never silent** — one malformed ``state.json`` skips that
  job with a warning instead of failing the listing (or being swallowed).
* **Redact before display** — free-text fields (detail/result/intent/name/needs)
  pass the same redaction as bridge logs; the supervisor writes them verbatim.
  Structured identifiers (``sessionId``/``bridgeSessionId``) pass through like
  the equivalent fields on ``/api/instances`` — the API is auth-gated and they
  are this job's own resume/cloud handles, not foreign secrets.
* **PID-reuse defense** — a roster worker counts as alive only when the pid's
  create-time matches the roster's ``procStart`` (jiffies), the same posture as
  :class:`~clauster.models.BridgePointer` liveness. Fail-closed: an unparsable
  ``procStart`` reads as not-alive rather than trusting a bare pid.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .claude_cli import resolve_binary
from .models import BackgroundJob
from .procutil import child_env, jiffies_to_epoch, proc_create_time
from .redact import redact_for_disk
from .trust import trust_directory

JOBS_DIR = Path("~/.claude/jobs").expanduser()
ROSTER_JSON = Path("~/.claude/daemon/roster.json").expanduser()

# Start-time slack for the jiffies→epoch comparison — same bound as
# procutil.is_live_bridge uses for a pointer's independently-derived jiffies.
_PROC_START_TOLERANCE = 2.0

# Cap on the redacted output.result text returned to the API/UI; the full text
# stays in the session transcript (transcript_path) — nothing is lost.
_RESULT_MAX_CHARS = 4000

_log = logging.getLogger("clauster.supervisor")


def _clean(value: object, *, limit: int | None = None) -> str:
    """Coerce a free-text field to a redacted ``str`` (``""`` for non-strings)."""
    if not isinstance(value, str):
        return ""
    text = redact_for_disk(value)
    if limit is not None and len(text) > limit:
        text = text[:limit] + " …<truncated>"
    return text


def _opt_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _opt_path(value: object) -> Path | None:
    """Coerce an expected path field to ``Path`` (None for non-strings/empty)."""
    return Path(value) if isinstance(value, str) and value else None


def load_roster_workers(roster_json: Path | None = None) -> dict[str, dict]:
    """Return the roster's ``workers`` map (short id → entry), ``{}`` when absent.

    The roster only exists while the supervisor runs, so a missing file is the
    normal idle state — only an unreadable or malformed file warns. ``None``
    resolves to :data:`ROSTER_JSON` at call time (late-bound so tests can
    monkeypatch the module constant).
    """
    if roster_json is None:
        roster_json = ROSTER_JSON
    try:
        raw = roster_json.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _log.warning("could not read %s: %s", roster_json, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("malformed roster %s: %s", roster_json, exc)
        return {}
    workers = data.get("workers") if isinstance(data, dict) else None
    return workers if isinstance(workers, dict) else {}


def worker_alive(
    pid: object, proc_start: object, *, tolerance: float = _PROC_START_TOLERANCE
) -> bool:
    """Whether a roster worker's pid is alive AND matches its recorded start time.

    Fail-closed: a recycled pid (create-time mismatch), a dead/zombie pid, or an
    unparsable ``procStart`` all read as not-alive — never trust a bare pid.
    (Unlike ``procutil.is_live_bridge`` there is no cmdline shape to fall back
    on for these workers, so the start-time match is the only trust signal.)
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    created = proc_create_time(pid)
    if created is None:
        return False
    try:
        jiffies = int(str(proc_start))
    except (TypeError, ValueError):
        return False
    expected = jiffies_to_epoch(jiffies)
    if expected is None:
        return False
    return abs(created - expected) <= tolerance


def _job_from_state(job_id: str, data: dict, workers: dict[str, dict]) -> BackgroundJob:
    """Map one parsed ``state.json`` object to a :class:`BackgroundJob`."""
    output = data.get("output")
    result = output.get("result") if isinstance(output, dict) else None
    worker = workers.get(job_id)
    pid = worker.get("pid") if isinstance(worker, dict) else None
    alive = worker_alive(pid, worker.get("procStart")) if isinstance(worker, dict) else False
    return BackgroundJob(
        id=job_id,
        state=_clean(data.get("state")),
        detail=_clean(data.get("detail")),
        tempo=_clean(data.get("tempo")),
        needs=_clean(data.get("needs")) or None,
        result=_clean(result, limit=_RESULT_MAX_CHARS) or None,
        intent=_clean(data.get("intent")),
        name=_clean(data.get("name")) or None,
        cwd=_opt_path(data.get("cwd")),
        session_id=_opt_str(data.get("sessionId")),
        bridge_session_id=_opt_str(data.get("bridgeSessionId")),
        transcript_path=_opt_path(data.get("linkScanPath")),
        cli_version=_clean(data.get("cliVersion")),
        created_at=_clean(data.get("createdAt")),
        updated_at=_clean(data.get("updatedAt")),
        worker_pid=pid if alive and isinstance(pid, int) else None,
        worker_alive=alive,
    )


def list_background_jobs(
    jobs_dir: Path | None = None, roster_json: Path | None = None
) -> list[BackgroundJob]:
    """List agent-view background sessions from the supervisor's on-disk state.

    Newest-updated first. A missing jobs dir means the feature is unused (agent
    view is a research preview — absence is the common case) and returns ``[]``.
    ``None`` args resolve to the module constants at call time (late-bound so
    tests can monkeypatch :data:`JOBS_DIR` / :data:`ROSTER_JSON`).
    """
    if jobs_dir is None:
        jobs_dir = JOBS_DIR
    try:
        entries = sorted(path for path in jobs_dir.iterdir() if path.is_dir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        _log.warning("could not list %s: %s", jobs_dir, exc)
        return []
    workers = load_roster_workers(roster_json)
    jobs: list[BackgroundJob] = []
    for entry in entries:
        state_file = entry / "state.json"
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue  # half-created or just-reaped job dir — not an error
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("skipping background job %s: unreadable state.json (%s)", entry.name, exc)
            continue
        if not isinstance(data, dict):
            _log.warning("skipping background job %s: state.json is not an object", entry.name)
            continue
        jobs.append(_job_from_state(entry.name, data, workers))
    jobs.sort(key=lambda job: (job.updated_at, job.id), reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# Dispatch (BG-2) — fire a `claude --bg` background session.
# ---------------------------------------------------------------------------

# Generous wall-clock budget: the dispatcher backgrounds the worker and returns
# in seconds, but it may internally retry once on an `ack-timeout` (observed),
# so the bound leaves room for that without hanging the request forever.
_DISPATCH_TIMEOUT = 60.0

# `claude --bg` prints `backgrounded · <id>` on success; <id> is the first eight
# hex chars of the session UUID. The labelled form is authoritative; a bare
# 8-hex token is a fallback so a future banner reword still yields the id.
_DISPATCH_ID_RE = re.compile(r"backgrounded[^\n0-9a-f]*([0-9a-f]{8})\b")
_BARE_ID_RE = re.compile(r"\b[0-9a-f]{8}\b")


class DispatchError(RuntimeError):
    """A ``claude --bg`` dispatch failed (bad exit, timeout, or no job id printed)."""


def parse_job_id(stdout: str) -> str | None:
    """Extract the dispatched 8-hex job id from ``claude --bg`` stdout.

    The success banner is ``backgrounded · <id>``; that labelled form wins. A
    bare 8-hex token is accepted as a fallback. Returns None when no id-shaped
    token is present (so the caller can fail closed rather than invent an id).
    """
    labelled = _DISPATCH_ID_RE.search(stdout)
    if labelled is not None:
        return labelled.group(1)
    bare = _BARE_ID_RE.search(stdout)
    return bare.group(0) if bare is not None else None


def _flag_value_ok(value: object) -> bool:
    """Whether a flag value is a safe argv item: a non-empty str with no leading dash.

    Total over its input type (the body is arbitrary JSON) — a non-string reads as
    not-ok rather than raising, so the caller fails closed with a clear error.
    """
    return isinstance(value, str) and bool(value) and not value.startswith("-")


def build_dispatch_argv(
    binary: str,
    *,
    rc_name: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    prompt: str | None = None,
    resume: str | None = None,
) -> list[str]:
    """Build the ``claude --bg [...]`` argv. Pure (no side effects) — unit-testable.

    ``rc_name`` opens the cloud door (``--rc`` → a cloud-visible Remote Control
    session); omit it for a purely local background job. ``prompt`` is the
    trailing positional (the session's initial instruction) and may be omitted.
    ``resume`` is a prior session's full UUID — ``--bg --resume <uuid>`` continues
    that conversation in a new bg job inheriting its transcript (#336).
    """
    argv = [binary, "--bg"]
    if resume:
        argv += ["--resume", resume]
    if rc_name:
        argv += ["--rc", rc_name]
    if model:
        argv += ["--model", model]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    if prompt:
        # `--` end-of-options separator (verified honored by `claude --bg`): a
        # prompt that starts with a dash is then a positional, never parsed as a
        # flag — so the prompt can't smuggle an argv flag past the flag guard.
        argv += ["--", prompt]
    return argv


def dispatch_background_job(
    cwd: Path,
    *,
    prompt: str | None = None,
    rc_name: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    binary: str = "claude",
    claude_json: Path | None = None,
    timeout: float = _DISPATCH_TIMEOUT,
    resume: str | None = None,
) -> str:
    """Dispatch a ``claude --bg`` background session in ``cwd``; return its job id.

    Fire-and-return: the CLI backgrounds the worker, prints ``backgrounded ·
    <id>``, and exits; the bg-agents panel (``GET /api/agents``) reflects the
    session's live state thereafter. Invariants:

    * **Validate before spawning** — ``cwd`` must be an existing directory, the
      binary resolves to an absolute path (``ClaudeNotFound`` otherwise), argv is
      a list (never ``shell=True``), and flag-valued options are rejected when
      they could be mistaken for an argv flag.
    * **Pre-trust** — the detached dispatcher cannot answer the one-time trust
      dialog (it wedges on "stuck on a startup dialog"), so the cwd is trusted
      first via the same writer the bridge spawn uses.
    * **Fail closed, never silently** — a non-zero exit, a timeout, or output
      with no job id raises :class:`DispatchError` (stderr redacted) rather than
      returning a bogus or empty id.

    ``rc_name`` opens the cloud door. NOTE: a consent-gated ``permission_mode``
    (auto / acceptEdits / bypassPermissions) is passed through, but the cloud
    surface may still re-gate mode entry — the panel surfaces that as the job's
    ``needs``; clauster does not assume the mode is live cloud-side.
    """
    cwd = Path(cwd)
    if not cwd.is_dir():
        raise DispatchError(f"dispatch cwd is not a directory: {cwd}")
    if prompt is not None and not isinstance(prompt, str):
        raise DispatchError(f"invalid prompt: {prompt!r}")
    flag_values = (("rc name", rc_name), ("model", model), ("permission mode", permission_mode))
    for label, value in flag_values:
        if value is not None and not _flag_value_ok(value):
            raise DispatchError(f"invalid {label}: {value!r}")
    # The resume arg is a session UUID, not a flag value: validate it to the RFC-4122
    # shape (the 8-hex job-id guard would reject it) so it can't smuggle an argv flag.
    if resume is not None and not valid_session_id(resume):
        raise DispatchError(f"invalid resume session id: {resume!r}")
    resolved = resolve_binary(binary)  # absolute path, or ClaudeNotFound
    if claude_json is None:
        trust_directory(cwd)
    else:
        trust_directory(cwd, claude_json)
    argv = build_dispatch_argv(
        resolved,
        rc_name=rc_name,
        model=model,
        permission_mode=permission_mode,
        prompt=prompt,
        resume=resume,
    )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),  # a `claude --bg` agent runs project code; keep secrets out
        )
    except subprocess.TimeoutExpired as exc:
        raise DispatchError(f"`claude --bg` timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        detail = _clean(proc.stderr) or _clean(proc.stdout) or f"exit {proc.returncode}"
        raise DispatchError(f"`claude --bg` failed (exit {proc.returncode}): {detail}")
    job_id = parse_job_id(proc.stdout or "")
    if job_id is None:
        raise DispatchError("`claude --bg` exited 0 but printed no job id")
    return job_id


# ---------------------------------------------------------------------------
# Stop (BG-3) — cloud-deregistering teardown of a `claude --bg` session.
# ---------------------------------------------------------------------------

# Job id shape (== sessionId[:8], `daemonShort`): eight lowercase hex. Validating
# it also guards the `claude rm <id>` argv against injection.
_JOB_ID_RE = re.compile(r"[0-9a-f]{8}")

# Session UUID shape (RFC-4122, 8-4-4-4-12 hex) — guards the `--resume <uuid>` argv
# against injection. Distinct from the 8-hex job id (#336).
_SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# How long to wait for the SIGINT'd session to actually exit (orderly settle).
_SETTLE_TIMEOUT = 20.0
_SETTLE_POLL = 0.5
# Gap between the two SIGINTs ("double-SIGINT" — the second is a harmless no-op
# once the first has settled it; belt-and-suspenders for the rare slow case).
_SIGINT_GAP = 1.5

# `claude rm` soft-fails with this wording when the transient supervisor has
# idle-exited — the process is already gone, so it's reported, not raised.
_RM_SOFT_FAIL = "couldn't confirm stopped"


class StopError(RuntimeError):
    """A background-session stop could not complete cleanly (raised fail-closed)."""


def valid_job_id(job_id: object) -> bool:
    """Whether ``job_id`` matches the 8-hex short-id shape (argv-injection guard)."""
    return isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id) is not None


def valid_session_id(session_id: object) -> bool:
    """Whether ``session_id`` matches the RFC-4122 UUID shape (resume argv guard, #336).

    The ``--resume`` argument is the full session UUID, NOT the 8-hex job id, so the
    job-id guard would wrongly reject it; this validates the UUID's own shape.
    """
    return isinstance(session_id, str) and _SESSION_ID_RE.fullmatch(session_id) is not None


class ResumeError(RuntimeError):
    """An ended `claude --bg` session could not be resumed (raised fail-closed, #336)."""


def resume_background_job(
    job_id: str,
    *,
    binary: str = "claude",
    claude_json: Path | None = None,
    jobs_dir: Path | None = None,
    roster_json: Path | None = None,
    timeout: float = _DISPATCH_TIMEOUT,
) -> str:
    """Resume an ended `claude --bg` session into a NEW bg job that inherits it (#336).

    Looks the job up by its 8-hex id, reads its full session UUID + cwd, then
    dispatches ``claude --bg --resume <uuid>`` in that cwd. Returns the **new** job
    id — a resume mints a fresh 8-hex id (and a new session UUID) that inherits the
    prior transcript, so the resumed agent surfaces as a new panel row.

    Fail-closed: a job that no longer exists, has no recorded session UUID, or has
    no cwd raises :class:`ResumeError` rather than dispatching a malformed resume.
    """
    job = next((j for j in list_background_jobs(jobs_dir, roster_json) if j.id == job_id), None)
    if job is None:
        raise ResumeError(f"no background job {job_id!r}")
    # Server-side guard mirroring the UI's `!agentLive(j)` gate: resuming a still-live
    # session would spawn a second worker over the same transcript. Don't rely on the UI.
    if job.worker_alive:
        raise ResumeError(f"job {job_id!r} is still live — stop it before resuming")
    if not valid_session_id(job.session_id):
        raise ResumeError(f"job {job_id!r} has no resumable session id")
    if job.cwd is None:
        raise ResumeError(f"job {job_id!r} has no recorded working directory")
    return dispatch_background_job(
        job.cwd,
        resume=job.session_id,
        binary=binary,
        claude_json=claude_json,
        timeout=timeout,
    )


def _live_session_pid(job_id: str, workers: dict[str, dict]) -> int | None:
    """Return the roster pid for ``job_id`` only if it passes the liveness guard.

    None when the job has no roster worker (already settled / supervisor down) OR
    the pid fails the pid+procStart match — i.e. we never return a pid we would
    not be safe to signal. Fail-closed: an unvalidated or recycled pid reads as
    "nothing live to stop", not "signal it anyway".
    """
    worker = workers.get(job_id)
    if not isinstance(worker, dict):
        return None
    pid = worker.get("pid")
    if not worker_alive(pid, worker.get("procStart")):
        return None
    return pid if isinstance(pid, int) else None


def _await_exit(pid: int, proc_start: object, *, timeout: float) -> bool:
    """Poll until the validated pid is no longer alive (orderly settle). True if it exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not worker_alive(pid, proc_start):
            return True
        time.sleep(_SETTLE_POLL)
    return not worker_alive(pid, proc_start)


def stop_background_job(
    job_id: str,
    *,
    binary: str = "claude",
    roster_json: Path | None = None,
    jobs_dir: Path | None = None,
    settle_timeout: float = _SETTLE_TIMEOUT,
) -> dict:
    """Stop a ``claude --bg`` session the cloud-deregistering way, then remove it.

    The clean teardown (empirically verified — probe ``dereg-probe``, 2026-06-11)
    is a **double-SIGINT to the session process**: the CLI runs an orderly
    shutdown, logs ``bg settled <id> (done)``, and DEREGISTERS the cloud bridge
    session. (``claude stop`` SIGKILLs → ``settled (killed)`` → a cloud orphan, so
    it is deliberately NOT used here.) All steps fail closed:

    1. Resolve the session pid from the roster (``state.json`` carries no pid) and
       validate it with the pid+procStart liveness check — NEVER signal an
       unvalidated or recycled pid. No live worker ⇒ the cloud-deregistering stop
       can't run or be confirmed: warn, report ``settled=False``, and still rm.
    2. SIGINT twice (idempotent — the second is a no-op once it has exited).
    3. Await the process actually exiting within ``settle_timeout``; raise
       :class:`StopError` if it does not. We do NOT escalate to SIGKILL —
       that would orphan the cloud session; escalation is the operator's call.
    4. ``claude rm <id>`` to drop the job dir. The transient supervisor may have
       idle-exited, making rm soft-fail; when the worker is confirmed dead (no
       live pid) clauster then drops the orphaned job dir itself so the row can
       still be forgotten (#485). A LIVE worker is never force-forgotten this way.

    Returns ``{"id", "settled": bool, "removed": bool, "detail": str}``. ``settled``
    is True ONLY for a confirmed cloud-deregistering double-SIGINT-and-exit; a
    no-live-worker stop returns ``settled=False`` (the session may already be stopped,
    or be an orphan whose worker died without deregistering — surfaced in ``detail``).
    """
    if not valid_job_id(job_id):
        raise StopError(f"invalid job id: {job_id!r}")
    resolved = resolve_binary(binary)  # absolute path, or ClaudeNotFound
    workers = load_roster_workers(roster_json)
    pid = _live_session_pid(job_id, workers)

    settled = False  # True ONLY once we CONFIRM the cloud-deregistering stop
    if pid is not None:
        proc_start = workers[job_id].get("procStart")
        try:
            os.kill(pid, signal.SIGINT)
            time.sleep(_SIGINT_GAP)
            # Re-validate before the second SIGINT: during the gap the first
            # signal may have settled the session and the kernel may have
            # recycled its pid to an unrelated process (the live clauster
            # service, this agent's own bridge). Never signal a recycled pid —
            # if it already exited/changed, the second SIGINT is unneeded anyway.
            if worker_alive(pid, proc_start):
                os.kill(pid, signal.SIGINT)  # second of the double-SIGINT
        except ProcessLookupError:
            pass  # exited between checks — that's the goal
        except OSError as exc:
            raise StopError(f"could not signal session {job_id} (pid {pid}): {exc}") from exc
        settled = _await_exit(pid, proc_start, timeout=settle_timeout)
        if not settled:
            raise StopError(
                f"session {job_id} did not settle within {settle_timeout:g}s "
                "(not force-killing — that would orphan the cloud session)"
            )
    else:
        # No validated-live worker to signal: the cloud-deregistering double-SIGINT
        # never ran, so we CANNOT confirm the cloud session was deregistered. It may
        # already be stopped, or its worker may have died WITHOUT deregistering (a cloud
        # orphan). Report settled=False and surface it, never a false clean stop.
        _log.warning(
            "stop_background_job: no live worker for %s — cloud deregistration not "
            "confirmed (already stopped, or a possible cloud orphan; re-check `claude agents`)",
            job_id,
        )

    removed, detail = _remove_job(resolved, job_id)
    # Stuck-orphan fallback (#485): when `claude rm` soft-fails (transient supervisor
    # down) the on-disk job dir is never dropped, so the row can never be forgotten
    # from the UI. We can drop that record ourselves — but ONLY when the worker is
    # confirmed dead (pid is None: the liveness guard found no live worker). Fail
    # closed: a still-live worker keeps the cloud-deregistering path and is NEVER
    # force-forgotten by deleting its dir out from under it.
    if not removed and pid is None:
        forced, forced_detail = _force_remove_job_dir(job_id, jobs_dir)
        if forced:
            removed = True
            detail = f"{detail}; {forced_detail}" if detail else forced_detail
    if not settled:
        note = "no live worker found — cloud stop not confirmed (re-check `claude agents`)"
        detail = f"{detail}; {note}" if detail else note
    return {"id": job_id, "settled": settled, "removed": removed, "detail": detail}


def _remove_job(resolved_binary: str, job_id: str) -> tuple[bool, str]:
    """Run ``claude rm <id>``; tolerate the supervisor-down soft-fail. (removed, detail)."""
    try:
        proc = subprocess.run(
            [resolved_binary, "rm", job_id],
            capture_output=True,
            text=True,
            timeout=30,
            env=child_env(),
        )
    except subprocess.TimeoutExpired:
        return False, "`claude rm` timed out"
    output = _clean(proc.stdout) or _clean(proc.stderr)
    if proc.returncode == 0:
        return True, output
    # A transient-supervisor-down rm reports the soft-fail wording; the process is
    # already settled, so surface it without raising (the row clears on next rm).
    if _RM_SOFT_FAIL in (proc.stdout + proc.stderr).lower():
        return False, output or _RM_SOFT_FAIL
    return False, output or f"`claude rm` exit {proc.returncode}"


def _force_remove_job_dir(job_id: str, jobs_dir: Path | None) -> tuple[bool, str]:
    """Drop a confirmed-dead job's on-disk dir when ``claude rm`` soft-failed (#485).

    Clauster lists these jobs by reading ``JOBS_DIR/<id>/state.json``; if ``claude
    rm`` can't reach the supervisor, that dir lingers and the row can never be
    forgotten. Deleting the dir clears the record. The caller gates this on the
    worker being confirmed dead, so this never races a live worker's own state.

    Fail-closed and quiet on the benign path: ``job_id`` is re-validated (it builds
    a filesystem path), an already-gone dir reads as removed, and a permission/IO
    error is reported (``False``) rather than swallowed. Returns ``(removed, detail)``.
    """
    if not valid_job_id(job_id):  # defence-in-depth: never build a path from an unvalidated id
        return False, "invalid job id"
    base = jobs_dir if jobs_dir is not None else JOBS_DIR
    target = base / job_id
    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return True, "job record already gone"  # nothing left to forget — the goal
    except OSError as exc:
        _log.warning("could not force-remove orphaned job dir %s: %s", target, exc)
        return False, f"could not drop local job record: {exc}"
    _log.info("forgot orphaned background job %s (dropped local record, rm soft-failed)", job_id)
    return True, "dropped orphaned local job record"
