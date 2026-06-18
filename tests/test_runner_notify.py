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

    runner._notify_crash(inst)
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
    runner._notify_crash(inst)
    assert not runner._notify_tasks


async def test_notify_crash_noop_when_crash_alerts_off(runner_config):
    runner = _runner(
        runner_config,
        notifications={"enabled": True, "urls": ["slack://x"], "notify_on_crash": False},
    )
    runner._notifier = _RecordingNotifier()
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.CRASHED)
    runner._notify_crash(inst)
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
        runner._notify_crash(inst)
    await asyncio.gather(*runner._notify_tasks)
    assert len(rec.calls) == 1
