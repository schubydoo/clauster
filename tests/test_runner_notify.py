"""Runner wiring for crash notifications: fire on a CRASHED transition, not via Stop."""

from __future__ import annotations

import asyncio

from clauster.config import ClausterConfig
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


class _RecordingNotifier:
    """Stand-in notifier capturing anotify calls (active by default)."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.calls: list[tuple[str, str]] = []

    async def anotify(self, title: str, body: str) -> None:
        self.calls.append((title, body))


def _runner(runner_config, *, notifications: dict | None = None) -> SessionRunner:
    config, claude_json = runner_config
    cfg = ClausterConfig(
        projects_root=config.projects_root,
        state_dir=config.state_dir,
        claude={"binary": config.claude.binary},
        notifications=notifications or {},
    )
    return SessionRunner(cfg, claude_json=claude_json)


async def test_notify_crash_fires_when_active(runner_config):
    runner = _runner(runner_config, notifications={"enabled": True, "urls": ["slack://x"]})
    rec = _RecordingNotifier()
    runner._notifier = rec
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)

    runner._notify_event("crash", inst)
    await asyncio.gather(*runner._notify_tasks)

    assert len(rec.calls) == 1
    title, body = rec.calls[0]
    assert "crashed" in title.lower()
    assert "alpha" in body


async def test_notify_crash_noop_when_notifier_inactive(runner_config):
    # Default config: notifications disabled -> real Notifier is inactive -> no task.
    runner = _runner(runner_config)
    assert runner._notifier.active is False
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    runner._notify_event("crash", inst)
    assert not runner._notify_tasks


async def test_notify_crash_noop_when_crash_alerts_off(runner_config):
    runner = _runner(
        runner_config,
        notifications={"enabled": True, "urls": ["slack://x"], "notify_on_crash": False},
    )
    runner._notifier = _RecordingNotifier()
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    runner._notify_event("crash", inst)
    assert not runner._notify_tasks


async def test_poll_once_crash_fires_notification(runner_config, monkeypatch):
    # Drive the real poll_once path: a RUNNING bridge whose process is gone (and not an
    # intentional stop) reconciles to CRASHED and fires the crash notification.
    from clauster import inspector  # noqa: F401 - patched by string path below

    runner = _runner(runner_config, notifications={"enabled": True, "urls": ["slack://x"]})
    rec = _RecordingNotifier()
    runner._notifier = rec
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING, bridge_pid=4242
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.reap_if_exited", lambda *a, **k: None)
    monkeypatch.setattr("clauster.runner.inspector.list_working_sessions", lambda *a, **k: [])

    await runner.poll_once()
    assert runner._instances["alpha"].status is InstanceStatus.CRASHED
    # The same transition bumps the per-project crash counter (#352 metrics).
    assert runner.crash_counts() == {"alpha": 1}
    await asyncio.gather(*runner._notify_tasks)
    assert len(rec.calls) == 1


async def test_reconcile_to_crashed_then_notify(runner_config):
    # The transition guard in poll_once fires only RUNNING/STARTING -> CRASHED. Verify
    # _reconcile_status produces CRASHED for an unexpected exit, which is what gates notify.
    runner = _runner(runner_config, notifications={"enabled": True, "urls": ["slack://x"]})
    rec = _RecordingNotifier()
    runner._notifier = rec
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)

    prev = inst.status
    runner._reconcile_status(inst, alive=False)  # unexpected death (not intentional_stop)
    assert inst.status is InstanceStatus.CRASHED
    if prev is not InstanceStatus.CRASHED and inst.status is InstanceStatus.CRASHED:
        runner._notify_event("crash", inst)
    await asyncio.gather(*runner._notify_tasks)
    assert len(rec.calls) == 1


# ----- lifecycle webhooks (#371) -------------------------------------------


class _RecordingWebhooks:
    """Stand-in webhook emitter capturing aemit calls (active by default)."""

    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.calls: list[tuple[str, dict]] = []

    def wants(self, event: str) -> bool:
        return self.active

    async def aemit(self, event: str, payload: dict) -> None:
        self.calls.append((event, payload))


class _AliveProc:
    """A minimal subprocess.Popen stand-in whose poll() reports a live process."""

    def poll(self):
        return None


async def test_webhook_emit_gated_by_wants(runner_config):
    runner = _runner(runner_config)
    runner._webhooks = _RecordingWebhooks(active=False)  # wants() → False
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    runner._emit_webhook("spawn", inst)
    assert not runner._notify_tasks  # gated out before creating a task
    assert runner._webhooks.calls == []


async def test_webhook_spawn_payload(runner_config):
    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="pty"
    )
    runner._emit_webhook("spawn", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(event, payload)] = rec.calls
    assert event == "spawn"
    # Assert the full payload contract, not just a couple of fields. With no starter
    # session yet, session_ref is None (None in -> None out).
    assert payload == {
        "project": "alpha",
        "label": "alpha",
        "status": "starting",
        "resume_mode": "pty",
        "spawn_mode": inst.spawn_mode,
        "session_ref": None,
    }


async def test_webhook_payload_hashes_session_id_never_leaks_raw(runner_config):
    # Item-8 (#408): a starter session id is bearer-equivalent (redaction strips it
    # everywhere else), so the webhook egresses an HMAC correlation token (keyed by the
    # per-deployment secret), never the raw session_<ULID>.
    import hashlib
    import hmac

    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    sid = "session_01TESTSTARTERAAAAAAAAAA"
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    inst.starter_session_id = sid
    runner._emit_webhook("ready", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(_event, payload)] = rec.calls
    # HMAC keyed by the runner's session_ref secret — unverifiable without that key.
    expected = hmac.new(
        runner._session_ref_key(), sid.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:16]
    assert payload["session_ref"] == expected
    # A bare SHA-256 of the id would be VERIFIABLE by anyone holding the plaintext;
    # the HMAC must differ from it (proves the key is actually mixed in).
    assert payload["session_ref"] != hashlib.sha256(sid.encode("utf-8")).hexdigest()[:16]
    assert "session_id" not in payload  # the raw field is gone
    assert sid not in payload.values()  # and the raw value never appears anywhere


def test_session_ref_key_fails_open_to_ephemeral_key(runner_config, monkeypatch):
    # Item-8 (#408): a webhook is fire-and-forget — if the session secret can't be
    # loaded (e.g. a misconfigured CLAUSTER_SESSION_SECRET), `_session_ref_key` must
    # fall back to an ephemeral per-process key, never raise into the lifecycle.
    runner = _runner(runner_config)

    def _boom(_state_dir):
        raise ValueError("CLAUSTER_SESSION_SECRET must be at least 32 bytes")

    monkeypatch.setattr("clauster.runner.auth.load_or_create_secret", _boom)
    key = runner._session_ref_key()
    assert isinstance(key, bytes) and len(key) == 32  # ephemeral fallback, no raise
    assert runner._session_ref_key() is key  # cached — loaded once, stable per process


async def test_webhook_ready_fires_on_transition_only(runner_config):
    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    runner._apply_pty_info(inst, {"state": "ready"}, _AliveProc())
    assert inst.status is InstanceStatus.RUNNING
    await asyncio.gather(*runner._notify_tasks)
    assert [e for e, _ in rec.calls] == ["ready"]
    # A second observe while already RUNNING must NOT re-emit (not every poll).
    runner._apply_pty_info(inst, {"state": "ready"}, _AliveProc())
    await asyncio.gather(*runner._notify_tasks)
    assert [e for e, _ in rec.calls] == ["ready"]


async def test_webhook_stop_fires(runner_config):
    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING
    )  # no bridge_pid → the no-pid STOPPED path
    runner._instances[fake.instance_id] = fake
    await runner.stop(fake.instance_id)
    await asyncio.gather(*runner._notify_tasks)
    assert [e for e, _ in rec.calls] == ["stop"]  # exactly one stop, nothing else


async def test_webhook_crash_fires_on_poll_once(runner_config, monkeypatch):
    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING, bridge_pid=4242
    )
    runner._instances[fake.instance_id] = fake
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    monkeypatch.setattr("clauster.runner.procutil.reap_if_exited", lambda *a, **k: None)
    monkeypatch.setattr("clauster.runner.inspector.list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()
    assert fake.status is InstanceStatus.CRASHED
    await asyncio.gather(*runner._notify_tasks)
    assert [e for e, _ in rec.calls] == ["crash"]  # exactly one crash event


# ----- extended lifecycle events (#432) ------------------------------------


async def test_emit_event_gated_by_wants(runner_config):
    # A default-off #432 event (wants() → False) is dropped before a task is created.
    runner = _runner(runner_config)
    runner._webhooks = _RecordingWebhooks(active=False)
    runner.emit_event("clone-done", {"event_type": "clone-done", "project": "alpha"})
    assert not runner._notify_tasks
    assert runner._webhooks.calls == []


async def test_emit_event_fires_payload_verbatim(runner_config):
    # When enabled, emit_event forwards the event + payload as-is (already redacted).
    runner = _runner(runner_config)
    rec = _RecordingWebhooks()
    runner._webhooks = rec
    payload = {"event_type": "clone-done", "project": "alpha", "status": "done", "error": None}
    runner.emit_event("clone-done", payload)
    await asyncio.gather(*runner._notify_tasks)
    assert rec.calls == [("clone-done", payload)]


# ----- extended notification event types (#541) ----------------------------


def _notify_runner(runner_config, **toggles):
    """A runner with a recording notifier (active) and a no-op webhook emitter.

    Disabling webhooks (wants → False) keeps the task set to the notification task(s)
    only, so a test can assert the notifier's calls without webhook tasks interfering.
    """
    runner = _runner(
        runner_config,
        notifications={"enabled": True, "urls": ["slack://x"], **toggles},
    )
    runner._notifier = _RecordingNotifier()
    runner._webhooks = _RecordingWebhooks(active=False)
    return runner


async def test_notify_event_ready_gated_off_by_default(runner_config):
    # notify_on_ready defaults OFF, so a ready event fires no notification.
    runner = _notify_runner(runner_config)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    runner._notify_event("ready", inst)
    assert not runner._notify_tasks


async def test_notify_event_ready_fires_when_enabled(runner_config):
    runner = _notify_runner(runner_config, notify_on_ready=True)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    runner._notify_event("ready", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(title, body)] = runner._notifier.calls
    assert "ready" in title.lower()
    assert "alpha" in body


async def test_notify_event_unknown_event_is_noop(runner_config):
    # An unknown event has no notify_on_* toggle -> event_enabled False -> no task.
    runner = _notify_runner(runner_config, notify_on_ready=True)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    runner._notify_event("not-an-event", inst)
    assert not runner._notify_tasks


async def test_emit_lifecycle_stop_fires_stop_notification(runner_config):
    # A normal stop (Stop button) on a non-session bridge -> notify_on_stop event.
    runner = _notify_runner(runner_config, notify_on_stop=True)
    inst = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, intentional_stop=True
    )
    runner._emit_lifecycle("stop", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(title, _body)] = runner._notifier.calls
    assert "stopped" in title.lower()


async def test_emit_lifecycle_session_end_uses_session_ended_event(runner_config):
    # A single-shot `session` bridge that exits without an explicit Stop is a
    # session-ended notification, NOT a stop. notify_on_stop is irrelevant here.
    runner = _notify_runner(runner_config, notify_on_session_end=True, notify_on_stop=False)
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.STOPPED,
        spawn_mode="session",
        intentional_stop=False,
    )
    runner._emit_lifecycle("stop", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(title, _body)] = runner._notifier.calls
    assert "session ended" in title.lower()


async def test_emit_lifecycle_spawn_fires_no_notification(runner_config):
    # spawn never carries a notification (it still records history). Assert the *notifier*
    # was untouched rather than the shared _notify_tasks set (which holds the history task).
    runner = _notify_runner(runner_config, notify_on_ready=True)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    runner._emit_lifecycle("spawn", inst)
    await asyncio.gather(*runner._notify_tasks)
    assert runner._notifier.calls == []


async def test_emit_lifecycle_crash_still_notifies(runner_config):
    # No regression: the crash notification still fires through the lifecycle chokepoint.
    runner = _notify_runner(runner_config)  # notify_on_crash defaults ON
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    runner._emit_lifecycle("crash", inst)
    await asyncio.gather(*runner._notify_tasks)
    [(title, _body)] = runner._notifier.calls
    assert "crashed" in title.lower()


async def test_notify_app_event_gated_off_by_default(runner_config):
    # permission-needed defaults OFF -> notify_app_event is a no-op.
    runner = _notify_runner(runner_config)
    runner.notify_app_event("permission-needed", "t", "b")
    assert not runner._notify_tasks


async def test_notify_app_event_fires_when_enabled(runner_config):
    runner = _notify_runner(runner_config, notify_on_permission=True)
    runner.notify_app_event("permission-needed", "the title", "the body")
    await asyncio.gather(*runner._notify_tasks)
    assert runner._notifier.calls == [("the title", "the body")]


async def test_notify_app_event_noop_when_notifier_inactive(runner_config):
    # Outbound channel off -> real Notifier inactive -> no task even when the toggle is on.
    runner = _runner(runner_config, notifications={"notify_on_permission": True})
    assert runner._notifier.active is False
    runner.notify_app_event("permission-needed", "t", "b")
    assert not runner._notify_tasks


class _GatedNotifier:
    """A notifier whose anotify blocks until ``release`` is set (timing control)."""

    def __init__(self) -> None:
        self.active = True
        self.release = asyncio.Event()
        self.finished = False

    async def anotify(self, title: str, body: str) -> None:
        await self.release.wait()
        self.finished = True


async def test_shutdown_drains_pending_notify_tasks(runner_config):
    # shutdown() must await an in-flight fire-and-forget notify rather than leave it
    # pending (a pending task is GC-cancelled at exit: "Task was destroyed but it is
    # pending", #734). With the send completing inside the grace, the task finishes and
    # the set drains.
    runner = _notify_runner(runner_config)
    gated = _GatedNotifier()
    runner._notifier = gated
    runner._notify_event("crash", _inst())
    assert len(runner._notify_tasks) == 1
    gated.release.set()  # the send can complete the moment shutdown awaits it

    await runner.shutdown()

    assert gated.finished is True  # drained to completion, not GC-cancelled
    assert not runner._notify_tasks  # done-callback removed it from the set


async def test_shutdown_cancels_notify_tasks_past_grace(runner_config, monkeypatch):
    # A notify send that outlives the drain grace must not block shutdown forever: the
    # grace elapses and the straggler is cancelled (bounded teardown).
    monkeypatch.setattr("clauster.runner._NOTIFY_DRAIN_GRACE", 0.05)
    runner = _notify_runner(runner_config)
    gated = _GatedNotifier()  # never released → the send never completes on its own
    runner._notifier = gated
    runner._notify_event("crash", _inst())
    [task] = list(runner._notify_tasks)

    await asyncio.wait_for(runner.shutdown(), timeout=1.0)  # returns despite the stuck send

    assert task.cancelled()  # the straggler was cancelled at the grace boundary
    assert gated.finished is False


def _inst() -> RemoteControlInstance:
    return RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
