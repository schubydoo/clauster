from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from clauster import bridge_log, code_sessions, inspector, pointers, procutil
from clauster.db.persistence import Persistence
from clauster.models import (
    Attribution,
    InstanceStatus,
    RemoteControlInstance,
    WorkingSession,
)
from clauster.runner import (
    _STALE_POINTER_TTL_SECONDS,
    AdoptionUnavailable,
    CapacityExceeded,
    InstanceStillLive,
    InvalidSpawnOption,
    NotTrusted,
    SessionRunner,
    UnknownProject,
)
from clauster.trust import is_trusted
from conftest import _raise_cancelled

# Fixed instance_id UUIDs for seeding StateStore (keyed by instance_id since #777).
_IID_ALPHA = "aaaaaaaa-0000-0000-0000-000000000001"
_IID_ALPHA2 = "aaaaaaaa-0000-0000-0000-000000000002"


def _make_runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


@contextlib.contextmanager
def _db_persistence(state_dir):
    """Yield a ``Persistence`` on ``state_dir``, disposing its engine on exit.

    A separate ``SessionRunner`` builds its own engine on the SAME SQLite file under
    ``state_dir`` (WAL + busy-timeout make concurrent engines safe), so it observes
    what the test wrote. Disposing on exit keeps a pooled connection from being GC'd
    undisposed (the ResourceWarning) or left holding the SQLite file open.
    """
    persistence = Persistence(state_dir)
    try:
        yield persistence
    finally:
        persistence.dispose()


def _db_save(state_dir, records):
    """Seed ``records`` into a DB-backed StateStore on ``state_dir``, then dispose."""
    with _db_persistence(state_dir) as persistence:
        persistence.state_store().save(records)


async def test_persist_tolerates_store_write_failure(runner_config, monkeypatch, caplog):
    # The state store is non-authoritative: a save OSError (disk full, revoked perms)
    # must not turn a successful spawn/stop into a 500. It degrades to a stale on-disk
    # record with a logged warning, and _last_saved is left unadvanced so the next
    # persist retries — mirroring hosted._persist's best-effort contract (#420).
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)

    def _boom(_subset):
        raise OSError("disk full")

    monkeypatch.setattr(runner._state, "save", _boom)

    with caplog.at_level("WARNING"):
        inst = await runner.spawn("alpha")  # spawn persists AFTER launch → save raises → swallowed
    assert inst.status is InstanceStatus.RUNNING  # spawn still succeeded, not a 500
    assert runner._last_saved is None  # not marked saved → next persist retries
    assert any("could not persist" in r.message for r in caplog.records)

    # stop() persists intentional_stop=True BEFORE signalling — a save failure there
    # must not abort the stop or drop the signal. Capture in a fresh block so the
    # stop-path warning is asserted independently of the spawn-path one.
    caplog.clear()
    with caplog.at_level("WARNING"):
        stopped = await runner.stop(inst.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    assert stopped.intentional_stop is True
    assert any("could not persist" in r.message for r in caplog.records)


async def test_persist_serializes_concurrent_callers(runner_config, monkeypatch):
    # The startup-watch / stop / poll loop can each call _persist on the same event
    # loop. The DB store's per-row prune raises StaleDataError if a racing writer
    # already removed the row, so _persist must hold _persist_lock — making each save
    # atomic. Prove it: a save that yields the loop mid-write must never overlap a
    # second concurrent save (#471).
    import time as _time

    runner = _make_runner(runner_config)

    in_flight = 0
    max_overlap = 0
    saves = 0

    def _slow_save(_subset):
        nonlocal in_flight, max_overlap, saves
        in_flight += 1
        max_overlap = max(max_overlap, in_flight)
        saves += 1
        try:
            # asyncio.to_thread runs this on a worker thread; this sleep overlaps the
            # event loop, so a second _persist would interleave here if the lock were
            # absent — _slow_save would re-enter and in_flight would reach 2.
            _time.sleep(0.02)
        finally:
            in_flight -= 1

    monkeypatch.setattr(runner._state, "save", _slow_save)

    # Each persist must compute a *distinct* subset that differs from _last_saved, or
    # the no-change early-return short-circuits before the save. Drive that directly so
    # the test doesn't fight _persist's own _persisted/_last_saved writeback.
    subsets = iter([{"alpha": {"label": "one"}}, {"beta": {"label": "two"}}])
    monkeypatch.setattr(runner, "_persist_subset", lambda: next(subsets))

    await asyncio.gather(runner._persist(), runner._persist())

    assert saves == 2  # both callers produced a distinct subset and actually saved
    assert max_overlap == 1  # the lock kept the two saves from overlapping


async def test_rediscover_persist_false_skips_state_write(runner_config, monkeypatch):
    # The headless read CLI (#775) calls rediscover(persist=False): it reattaches into
    # the in-memory registry but must NEVER write the shared state store, so a
    # `clauster status` run beside the live service can't clobber its state.json.
    runner = _make_runner(runner_config)
    calls = {"n": 0}

    async def spy_persist():
        calls["n"] += 1

    monkeypatch.setattr(runner, "_persist", spy_persist)
    await runner.rediscover(persist=False)
    assert calls["n"] == 0  # read-only: no write
    await runner.rediscover()  # the default still persists
    assert calls["n"] == 1


def _db_load(state_dir):
    """Return the persisted StateStore records on ``state_dir``, then dispose."""
    with _db_persistence(state_dir) as persistence:
        return persistence.state_store().load()


async def test_spawn_ready_then_stop(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.starter_session_id == "session_01TESTSTARTERAAAAAAAAAA"
    assert inst.bridge_id == "11111111-2222-3333-4444-555555555555"
    assert inst.url and inst.url.endswith("env_01TESTENVAAAAAAAAAAAAAAAA")
    assert runner.running_count() == 1

    stopped = await runner.stop(inst.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    assert stopped.intentional_stop is True
    assert runner.running_count() == 0


async def test_forget_drops_stopped_bridge_from_memory_and_disk(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    await runner.stop(inst.instance_id)
    assert runner.get_instance_for_project("alpha") is not None  # a stopped, resumable card

    await runner.forget(inst.instance_id)
    assert runner.get_instance_for_project("alpha") is None
    assert "alpha" not in runner._persisted  # dropped from the overlay base too
    # On disk: a fresh runner loads no record, so rediscover can't resurrect a card.
    assert "alpha" not in SessionRunner(config, claude_json=claude_json)._persisted


async def test_forget_drops_persisted_only_record(runner_config, monkeypatch):
    # A record that lives only in the persisted overlay (no in-memory instance) — e.g.
    # a stopped card not rebuilt as an instance — must still be forgettable: the method
    # skips the liveness block and just drops it from the overlay + disk.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    await runner.stop(inst.instance_id)
    runner._instances.pop(inst.instance_id)  # keep only the persisted overlay
    assert inst.instance_id in runner._persisted

    await runner.forget(inst.instance_id)
    assert inst.instance_id not in runner._persisted


async def test_forget_clears_pointer_with_relative_projects_root(runner_config, monkeypatch):
    # Greptile #868 P1: with a RELATIVE projects_root, forget must resolve to the bridge's
    # real (absolute) cwd so the pointer directory matches — otherwise the stale pointer
    # survives and the next launch reattaches it. Fails without the .resolve() in forget().
    config, claude_json = runner_config
    monkeypatch.chdir(config.projects_root.parent)
    rel_config = config.model_copy(update={"projects_root": Path(config.projects_root.name)})
    runner = SessionRunner(rel_config, claude_json=claude_json)
    # Seed through the store, not the in-memory cache: forget refreshes its merge
    # base from the DB at entry (#949), so a record must exist there to be found.
    runner._state.save({"iid": {"project_name": "alpha"}})

    proj_abs = (rel_config.projects_root / "alpha").resolve()
    pdir = runner._claude_projects_dir / pointers.sanitize_cwd(proj_abs)
    pdir.mkdir(parents=True, exist_ok=True)
    pointer = pdir / "bridge-pointer.json"
    pointer.write_text(
        json.dumps(
            {
                "sessionId": "session_x",
                "environmentId": "env_x",
                "source": "standalone",
                "pid": 81750,
                "procStart": "2590192",
            }
        )
    )
    await runner.forget("iid")
    assert not pointer.exists()


async def test_forget_without_project_name_skips_pointer_clear(runner_config, monkeypatch):
    # A legacy/malformed persisted record with no "project_name" is still forgettable;
    # the pointer clear is simply skipped (no project path to resolve). The DB store's
    # FK can't hold such a row, so stub the refresh source (#949: forget re-loads its
    # merge base from the store at entry) to hand back the legacy shape directly.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(runner._state, "load_strict", lambda: {"iid": {}})
    await runner.forget("iid")
    assert "iid" not in runner._persisted


async def test_forget_tolerates_live_pointer(runner_config, monkeypatch):
    # If the pointer somehow still looks live, forget leaves it (never yanks a live anchor)
    # but still drops the record — the clear is best-effort, not fatal.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    await runner.stop(inst.instance_id)

    def _raise_live(*_a, **_k):
        raise pointers.PointerStillLive("still live")

    monkeypatch.setattr("clauster.pointers.clear_pointer", _raise_live)
    await runner.forget(inst.instance_id)
    assert inst.instance_id not in runner._persisted


async def test_forget_tolerates_pointer_clear_oserror(runner_config, monkeypatch):
    # A filesystem hiccup clearing the pointer is logged, not raised — forget still drops
    # the record (it's already been removed from state.json by this point).
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    await runner.stop(inst.instance_id)

    def _raise_oserror(*_a, **_k):
        raise OSError("io error")

    monkeypatch.setattr("clauster.pointers.clear_pointer", _raise_oserror)
    await runner.forget(inst.instance_id)
    assert inst.instance_id not in runner._persisted


# ----- #867 L2: pre-spawn anchor health-check ---------------------------------------


def _write_nonlive_pointer(runner: SessionRunner, project_name: str) -> Path:
    """Write a well-formed, non-live bridge-pointer.json at the project's resolved dir."""
    proj = (runner._config.projects_root / project_name).resolve()
    pdir = runner._claude_projects_dir / pointers.sanitize_cwd(proj)
    pdir.mkdir(parents=True, exist_ok=True)
    pointer = pdir / "bridge-pointer.json"
    pointer.write_text(
        json.dumps(
            {
                "sessionId": "session_x",
                "environmentId": "env_x",
                "source": "standalone",
                "pid": 81750,  # long-dead PID -> not live
                "procStart": "2590192",
            }
        )
    )
    return pointer


def _pin_health(monkeypatch, health, *, calls: list | None = None) -> None:
    def _fake(*_a, **_k):
        if calls is not None:
            calls.append(1)
        return health

    monkeypatch.setattr("clauster.code_sessions.anchor_health_for_pointer", _fake)


async def test_heal_clears_poisoned_pointer(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _pin_health(monkeypatch, code_sessions.AnchorHealth.POISONED)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")
    assert not pointer.exists()  # archived/deleted anchor -> pointer cleared, cold start


async def test_heal_keeps_healthy_pointer(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _pin_health(monkeypatch, code_sessions.AnchorHealth.HEALTHY)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")
    assert pointer.exists()  # reattach as-is


async def test_heal_keeps_pointer_on_unknown(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _pin_health(monkeypatch, code_sessions.AnchorHealth.UNKNOWN)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")
    assert pointer.exists()  # indeterminate -> never destroy state


async def test_heal_noop_without_pointer(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    calls: list = []
    _pin_health(monkeypatch, code_sessions.AnchorHealth.POISONED, calls=calls)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")
    assert not calls  # cold start -> never probes the API


async def test_heal_skips_live_pointer(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    monkeypatch.setattr(pointers, "is_live", lambda ptr: True)
    calls: list = []
    _pin_health(monkeypatch, code_sessions.AnchorHealth.POISONED, calls=calls)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")
    assert pointer.exists() and not calls  # a running anchor is never probed or cleared


async def test_heal_tolerates_clear_error(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    _write_nonlive_pointer(runner, "alpha")
    _pin_health(monkeypatch, code_sessions.AnchorHealth.POISONED)

    def _boom(*_a, **_k):
        raise OSError("io error")

    monkeypatch.setattr("clauster.pointers.clear_pointer", _boom)
    await runner._clear_pointer_if_anchor_poisoned(config.projects_root / "alpha")  # no raise


# ----- #867 L3: poisoned-reattach detection + heal --------------------------------


class _FakeProc:
    """Minimal subprocess.Popen stand-in for the startup-watch tests."""

    def __init__(self, *, alive: bool = True, pid: int = 999999) -> None:
        self._alive = alive
        self.pid = pid
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def test_apply_markers_poison_beats_running(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    markers = bridge_log.BridgeMarkers(
        environment_id="env_x", poll_loop_started=True, poison_reason="archived"
    )
    runner._apply_markers(inst, markers, _FakeProc(alive=True))
    assert inst.status is InstanceStatus.ERROR  # poison surfaces, not a misleading RUNNING


async def test_heal_poisoned_reattach_stops_and_clears(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.ERROR)
    pointer = _write_nonlive_pointer(runner, "alpha")
    stopped: list = []
    monkeypatch.setattr(runner, "_signal_stop", lambda pid, **_k: stopped.append(pid))
    proc = _FakeProc(alive=False, pid=4242)  # already exited -> no wait
    await runner._heal_poisoned_reattach(inst, proc, config.projects_root / "alpha", "archived")
    assert stopped == [4242]
    assert not pointer.exists()  # stale pointer cleared for a cold restart
    assert "archived" in (inst.error_detail or "")


async def test_heal_force_kills_stuck_bridge(runner_config, monkeypatch):
    monkeypatch.setattr("clauster.runner._POISON_STOP_TIMEOUT", 0.05)
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.ERROR)
    _write_nonlive_pointer(runner, "alpha")
    monkeypatch.setattr(runner, "_signal_stop", lambda *a, **k: None)  # SIGINT ignored
    proc = _FakeProc(alive=True)  # never exits gracefully
    await runner._heal_poisoned_reattach(inst, proc, config.projects_root / "alpha", "deleted")
    assert proc.killed  # force-killed so no idle orphan is left


def test_await_ready_returns_poison_immediately(runner_config, tmp_path):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log = tmp_path / "b.log"
    log.write_text(
        "[bridge:work] Starting poll loop environmentId=env_01X\n"
        '{"reason":"archived","subtype":"end_session"}\n'
    )
    assert runner._await_ready(log, _FakeProc(alive=True)).poison_reason == "archived"


def test_await_ready_cold_start_skips_grace(runner_config, tmp_path):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log = tmp_path / "b.log"
    log.write_text(
        "[bridge:init] Created initial session session_01X\n"
        "[bridge:work] Starting poll loop environmentId=env_01X\n"
    )
    markers = runner._await_ready(log, _FakeProc(alive=True))
    assert markers.is_ready and markers.starter_session_id == "session_01X"
    assert markers.poison_reason is None


def test_await_ready_healthy_reattach_after_grace(runner_config, tmp_path, monkeypatch):
    monkeypatch.setattr("clauster.runner._POISON_GRACE", 0.1)
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log = tmp_path / "b.log"
    # reattach: poll loop, no "Created initial session", never poisoned -> grace elapses clean
    log.write_text("[bridge:work] Starting poll loop environmentId=env_01X\n")
    markers = runner._await_ready(log, _FakeProc(alive=True))
    assert markers.is_ready and markers.poison_reason is None


def test_await_ready_catches_poison_during_grace(runner_config, tmp_path, monkeypatch):
    # Poison that appears mid-grace (not present at readiness) is still caught.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    ready = bridge_log.BridgeMarkers(environment_id="env_x", poll_loop_started=True)
    poisoned = bridge_log.BridgeMarkers(
        environment_id="env_x", poll_loop_started=True, poison_reason="archived"
    )
    reads = iter([ready, poisoned])
    monkeypatch.setattr(runner, "_read_markers", lambda *_a: next(reads, poisoned))
    assert (
        runner._await_ready(tmp_path / "b.log", _FakeProc(alive=True)).poison_reason == "archived"
    )


def test_await_ready_proc_exit_during_grace_returns(runner_config, tmp_path, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log = tmp_path / "b.log"
    log.write_text("[bridge:work] Starting poll loop environmentId=env_01X\n")  # ready reattach
    proc = _FakeProc(alive=True)
    n = {"c": 0}

    def _poll():  # alive for the readiness + first grace check, then exits
        n["c"] += 1
        return None if n["c"] <= 2 else 0

    monkeypatch.setattr(proc, "poll", _poll)
    assert runner._await_ready(log, proc).is_ready


async def test_heal_kill_error_is_swallowed(runner_config, monkeypatch):
    monkeypatch.setattr("clauster.runner._POISON_STOP_TIMEOUT", 0.05)
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.ERROR)
    _write_nonlive_pointer(runner, "alpha")
    monkeypatch.setattr(runner, "_signal_stop", lambda *a, **k: None)

    class _KillBoom(_FakeProc):
        def kill(self):
            raise OSError("already gone")

    await runner._heal_poisoned_reattach(
        inst, _KillBoom(alive=True), config.projects_root / "alpha", "archived"
    )  # a kill error is logged, not raised


async def test_heal_clear_error_is_logged(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.ERROR)
    monkeypatch.setattr(runner, "_signal_stop", lambda *a, **k: None)

    def _boom(*_a, **_k):
        raise OSError("io error")

    monkeypatch.setattr("clauster.pointers.clear_pointer", _boom)
    await runner._heal_poisoned_reattach(
        inst, _FakeProc(alive=False), config.projects_root / "alpha", "deleted"
    )  # clear error logged, not raised


async def test_spawn_poison_marks_error_and_clears_pointer(runner_config, monkeypatch):
    # End-to-end: a spawn whose reattach is poisoned surfaces ERROR and clears the pointer.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    poison = bridge_log.BridgeMarkers(
        environment_id="env_x", poll_loop_started=True, poison_reason="archived"
    )
    monkeypatch.setattr(runner, "_await_ready", lambda *a, **k: poison)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR
    assert "archived" in (inst.error_detail or "")
    assert not pointer.exists()  # heal cleared the stale pointer for a cold restart


async def test_forget_refuses_running_bridge(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    with pytest.raises(InstanceStillLive):
        await runner.forget(inst.instance_id)
    assert runner.get_instance_for_project("alpha") is not None  # left intact, never killed
    await runner.stop(inst.instance_id)  # cleanup the fake process


async def test_forget_refuses_when_bridge_process_still_live_despite_status(
    runner_config, monkeypatch
):
    # Defense in depth: a STOPPED status with a still-live process must not be forgotten.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst is not None
    inst.status = InstanceStatus.STOPPED  # lagging status (e.g. a missed poll)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    with pytest.raises(InstanceStillLive):
        await runner.forget(inst.instance_id)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner.stop(inst.instance_id)  # cleanup


async def test_forget_refuses_when_keeper_process_still_live(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst is not None
    original_bridge_pid = inst.bridge_pid
    inst.status = InstanceStatus.STOPPED
    inst.bridge_pid = None  # skip the bridge check, exercise the keeper branch
    inst.keeper_pid = 4242
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 123.0)
    try:
        with pytest.raises(InstanceStillLive):
            await runner.forget(inst.instance_id)
    finally:
        inst.bridge_pid = original_bridge_pid
        inst.keeper_pid = None  # clear fake pid so stop() skips _cleanup_keeper(4242)
        await runner.stop(inst.instance_id)


async def test_forget_unknown_project_raises(runner_config):
    with pytest.raises(UnknownProject):
        await _make_runner(runner_config).forget("ghostproj")


async def test_redact_session_url_splits_raw_and_redacted_on_disk(runner_config, monkeypatch):
    # With logs.redact_session_url on, the bridge writes a private 0600 raw debug log
    # (the verbatim parse-source for readiness + the deep link), and the public on-disk
    # bridge log is a redacted mirror of it.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.logs.redact_session_url = True
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.RUNNING
    # Readiness + identifiers still resolve — parsed from the verbatim raw copy.
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.starter_session_id == "session_01TESTSTARTERAAAAAAAAAA"

    raw, public = inst.bridge_raw_log_path, inst.bridge_debug_log_path
    assert raw is not None and public is not None and raw != public

    raw_text = raw.read_text(encoding="utf-8")
    assert "session_01TESTSTARTERAAAAAAAAAA" in raw_text  # raw stays verbatim
    assert "env_01TESTENVAAAAAAAAAAAAAAAA" in raw_text
    if sys.platform != "win32":  # POSIX perms; Windows doesn't honor 0o600
        assert raw.stat().st_mode & 0o077 == 0  # private: no group/other access

    public_text = public.read_text(encoding="utf-8")
    assert "session_01TESTSTARTERAAAAAAAAAA" not in public_text  # public is redacted
    assert "env_01TESTENVAAAAAAAAAAAAAAAA" not in public_text
    assert "_<redacted>" in public_text

    await runner.stop(inst.instance_id)


async def test_no_redaction_keeps_a_single_verbatim_bridge_log(runner_config, monkeypatch):
    # Default (flag off): no split — the bridge log is the single verbatim file, exactly
    # as before. Readers and the WS tail point at the same path.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    assert config.logs.redact_session_url is False
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.bridge_raw_log_path == inst.bridge_debug_log_path
    assert inst.bridge_debug_log_path is not None
    if sys.platform != "win32":  # POSIX perms; Windows doesn't honor 0o600
        # The single verbatim log holds the unredacted session URL, so it is pre-created
        # 0600 (no group/other access) even with redaction off — never left to the
        # bridge's umask-default --debug-file open.
        assert inst.bridge_debug_log_path.stat().st_mode & 0o077 == 0
    assert "session_01TESTSTARTERAAAAAAAAAA" in inst.bridge_debug_log_path.read_text(
        encoding="utf-8"
    )
    await runner.stop(inst.instance_id)


def test_unique_log_path_distinct_within_same_millisecond(runner_config, monkeypatch):
    # Two same-project spawns in the same millisecond must get distinct log paths, so the
    # 0600 O_EXCL pre-create can't FileExistsError (the ms timestamp alone would collide,
    # and a retry on it wouldn't advance the clock).
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.runner.time.time", lambda: 1_700_000.0)  # frozen clock
    assert runner._unique_log_path("alpha") != runner._unique_log_path("alpha")


def test_flush_redacted_mirror_is_best_effort(runner_config, tmp_path):
    # The mirror flush must never raise on FS trouble (it runs in the poll loop and
    # at spawn): missing raw, an unreadable raw, and an unwritable public are all no-ops.
    runner = _make_runner(runner_config)
    raw, public = tmp_path / "b.raw.log", tmp_path / "b.log"
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_raw_log_path=raw,
        bridge_debug_log_path=public,
    )
    # Raw not written yet -> no-op, no public file created.
    runner._flush_redacted_mirror(inst)
    assert not public.exists()

    # Raw verbatim -> public becomes the redacted mirror.
    raw.write_text("session_01ABCDEFGHIJKLMNOP here\n", encoding="utf-8")
    runner._flush_redacted_mirror(inst)
    assert "session_01ABCDEFGHIJKLMNOP" not in public.read_text(encoding="utf-8")
    assert "session_<redacted>" in public.read_text(encoding="utf-8")

    # Unreadable raw (a directory) -> read OSError branch, no raise.
    bad_raw = tmp_path / "dir.raw.log"
    bad_raw.mkdir()
    inst.bridge_raw_log_path = bad_raw
    runner._flush_redacted_mirror(inst)

    # Unwritable public (a directory) -> write OSError branch, no raise.
    bad_public = tmp_path / "pub.dir"
    bad_public.mkdir()
    inst.bridge_raw_log_path, inst.bridge_debug_log_path = raw, bad_public
    runner._flush_redacted_mirror(inst)


def test_flush_redacted_mirror_noop_when_paths_coincide(runner_config, tmp_path):
    # Redaction off -> raw == public; the verbatim log must be left untouched.
    runner = _make_runner(runner_config)
    p = tmp_path / "b.log"
    p.write_text("session_01ABCDEFGHIJKLMNOP\n", encoding="utf-8")
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_raw_log_path=p,
        bridge_debug_log_path=p,
    )
    runner._flush_redacted_mirror(inst)
    assert "session_01ABCDEFGHIJKLMNOP" in p.read_text(encoding="utf-8")


async def test_stop_releases_proc_handle(runner_config, monkeypatch):
    # The dead Popen handle must be dropped from _procs on stop — it was never
    # removed, leaking dead handles across spawn/stop cycles.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.instance_id in runner._procs
    await runner.stop(inst.instance_id)
    assert inst.instance_id not in runner._procs


async def test_stop_signals_graceful_shutdown(runner_config, monkeypatch):
    # The bridge must receive the graceful stop signal (SIGINT on POSIX,
    # CTRL_BREAK on Windows) and log its shutdown marker before exiting — proves
    # stop() is graceful cross-platform, not a hard kill.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    log_path = inst.bridge_debug_log_path
    assert log_path is not None

    await runner.stop(inst.instance_id)
    assert "[bridge:shutdown]" in log_path.read_text()


async def test_stop_force_kills_when_signal_ignored(runner_config, monkeypatch):
    # If the bridge never clears the liveness check (ignored the signal, or a
    # wrapper process lingers), _await_exit exhausts its grace loop and force-kills.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    pid = inst.bridge_pid

    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    killed: list[int] = []
    from clauster import procutil

    real_force = procutil.force_kill_tree
    monkeypatch.setattr(
        "clauster.runner.procutil.force_kill_tree",
        lambda p: (killed.append(p), real_force(p))[0],
    )

    async def _nosleep(_seconds):
        return None

    monkeypatch.setattr("clauster.runner.asyncio.sleep", _nosleep)

    stopped = await runner.stop(inst.instance_id)
    assert stopped.status is InstanceStatus.STOPPED
    assert killed == [pid]  # force-kill fallback fired


async def test_spawn_unresolvable_binary_is_error(runner_config):
    # A claude binary that doesn't resolve must fail the instance to ERROR, not
    # leave it stuck in STARTING or raise out of spawn().
    config, claude_json = runner_config
    config.claude.binary = "definitely-not-a-real-claude-xyz"
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


async def test_spawn_unknown_project_rejected(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.spawn("does-not-exist")


async def test_spawn_is_idempotent_while_running(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    first = await runner.spawn("alpha")
    assert first.status is InstanceStatus.RUNNING
    second = await runner.spawn("alpha")  # already running -> returns the same instance
    assert second is first
    assert runner.running_count() == 1
    await runner.stop(first.instance_id)


async def test_concurrent_spawn_launches_one_bridge(runner_config, monkeypatch):
    # Two near-simultaneous spawns of the same project (double-click / retry / two
    # tabs) must not both pass the idempotency check across the awaits and launch
    # two bridges — the second would clobber the first in the registry and orphan
    # an untracked, unreapable process. The per-project lock serializes them.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)

    popen_calls = 0
    real_popen = runner._popen

    def counting_popen(*args, **kwargs):
        nonlocal popen_calls
        popen_calls += 1
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner, "_popen", counting_popen)

    first, second = await asyncio.gather(runner.spawn("alpha"), runner.spawn("alpha"))

    assert popen_calls == 1  # exactly one bridge process launched
    assert first is second  # both callers get the same instance
    assert len(runner._procs) == 1  # no orphaned, untracked process
    assert runner.running_count() == 1
    await runner.stop(first.instance_id)


async def test_rediscover_resurrects_dead_bridge_and_retains_metadata(runner_config):
    # A discovered project whose bridge isn't alive at rediscover time (its process
    # died while Clauster was down — e.g. a host reboot) is resurrected as a STOPPED,
    # resumable card from its persisted record, AND keeps that record in state.json
    # (not wiped on the post-rediscover save, which would later resume it with
    # default modes — a silent downgrade).
    config, claude_json = runner_config
    _db_save(
        config.state_dir,
        {
            _IID_ALPHA: {
                "project_name": "alpha",
                "label": "Custom Label",
                "permission_mode": "plan",
                "spawn_mode": "same-dir",
                "resume_mode": "standard",
                "intentional_stop": True,
            }
        },
    )
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.rediscover()  # bridge gone, but a persisted record exists

    inst = runner.get_instance_for_project("alpha")
    assert inst is not None
    assert inst.status is InstanceStatus.STOPPED  # surfaced as a resumable card
    assert inst.bridge_pid is None and inst.keeper_pid is None  # process is gone
    assert inst.permission_mode == "plan"  # persisted modes preserved
    assert inst.resume_mode == "standard"
    assert inst.label == "Custom Label"
    assert inst.intentional_stop is True  # carried through

    reloaded = _db_load(config.state_dir)
    alpha_rec = next(v for v in reloaded.values() if v.get("project_name") == "alpha")
    assert alpha_rec["permission_mode"] == "plan"
    assert alpha_rec["spawn_mode"] == "same-dir"
    assert alpha_rec["resume_mode"] == "standard"
    assert alpha_rec["label"] == "Custom Label"


async def test_rediscover_pty_orphan_resumable_and_skips_unpersisted(runner_config):
    # A "pty" bridge killed by a host reboot returns as a STOPPED card whose
    # resume_mode is preserved, so the UI offers true-resume (--continue restores the
    # conversation) — this is the dogfood bug. A discovered project with NO persisted
    # record is left absent: no phantom card offering to resume nothing.
    config, claude_json = runner_config
    _db_save(
        config.state_dir,
        {
            _IID_ALPHA: {
                "project_name": "alpha",
                "label": "alpha",
                "spawn_mode": "same-dir",
                "resume_mode": "pty",
                "intentional_stop": False,
            }
        },
    )
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.rediscover()

    alpha = runner.get_instance_for_project("alpha")
    assert alpha is not None
    assert alpha.status is InstanceStatus.STOPPED
    assert alpha.resume_mode == "pty"  # true-resume affordance survives the reboot
    assert alpha.intentional_stop is False  # interrupted, not a deliberate stop
    # Discovered but unpersisted -> no phantom.
    assert runner.get_instance_for_project("beta") is None


def test_reattach_pty_from_sidecar_without_instance_id_autogenerates(runner_config, monkeypatch):
    # #789 follow-up: _reattach_pty_from_sidecar takes an optional instance_id. rediscover
    # always passes the persisted record's id, so the `instance_id is None` default branch
    # (skip the kwarg -> the model auto-generates one) was left uncovered. Call it directly
    # without an instance_id and assert the reattached instance still gets a valid id.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.keeper.json").write_text(
        json.dumps(
            {
                "keeper_pid": 4321,
                "bridge_pid": 5678,
                "bridge_proc_start": 111.0,
                "state": "ready",
            }
        )
    )
    monkeypatch.setattr("clauster.runner.procutil.is_keeper_process", lambda pid: True)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    saved = {
        "project_name": "alpha",
        "label": "alpha",
        "spawn_mode": "same-dir",
        "resume_mode": "pty",
        "intentional_stop": False,
    }

    inst = runner._reattach_pty_from_sidecar("alpha", saved)  # no instance_id -> None branch

    assert inst is not None
    assert inst.status is InstanceStatus.RUNNING
    assert inst.resume_mode == "pty"
    assert inst.keeper_pid == 4321 and inst.bridge_pid == 5678
    # The None branch skipped kwargs["instance_id"], so the model supplied its own.
    assert inst.instance_id  # non-empty auto-generated id


async def test_stop_instance_without_pid_marks_stopped(runner_config):
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=None,
    )
    runner._instances[fake.instance_id] = fake
    inst = await runner.stop(fake.instance_id)
    assert inst.status is InstanceStatus.STOPPED and inst.intentional_stop is True


async def test_stop_serializes_on_spawn_lock(runner_config):
    # Regression: stop() must take the per-project spawn lock so it can't read bridge_pid=None
    # mid-spawn (while _spawn_locked is suspended in to_thread(_popen)) and orphan the bridge by
    # marking it STOPPED. Hold the lock (standing in for an in-flight spawn) and assert stop()
    # blocks until it's released — without the lock, stop() would complete immediately.
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.STARTING,
        bridge_pid=None,
    )
    runner._instances[fake.instance_id] = fake
    lock = runner._spawn_lock_for("alpha")
    await lock.acquire()  # stand in for spawn() holding the lock during to_thread(_popen)
    stop_task = asyncio.create_task(runner.stop(fake.instance_id))
    await asyncio.sleep(0.05)
    assert not stop_task.done()  # blocked on the spawn lock
    assert fake.status is InstanceStatus.STARTING  # not yet stopped
    lock.release()  # spawn finished and released the lock
    inst = await asyncio.wait_for(stop_task, timeout=1.0)
    assert inst.status is InstanceStatus.STOPPED


async def test_spawn_path_traversal_rejected(runner_config):
    runner = _make_runner(runner_config)
    # Invalid names never reach Popen (spec §9 path-traversal defense).
    for evil in ("../etc", "a/b", "..", "foo bar"):
        with pytest.raises(UnknownProject):
            await runner.spawn(evil)


async def test_spawn_untrusted_refused(runner_config, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    empty_trust = tmp_path / "untrusted.json"
    empty_trust.write_text("{}")
    runner = SessionRunner(config, claude_json=empty_trust)
    with pytest.raises(NotTrusted):
        await runner.spawn("alpha")


async def test_spawn_trust_true_trusts_untrusted_dir_then_spawns(
    runner_config, tmp_path, monkeypatch
):
    # #775 --trust: an untrusted directory is trusted as part of the spawn instead of
    # raising NotTrusted, and the bridge comes up.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    empty_trust = tmp_path / "untrusted.json"
    empty_trust.write_text("{}")
    runner = SessionRunner(config, claude_json=empty_trust)
    proj = runner._resolve_project("alpha")
    assert not is_trusted(proj.path, empty_trust)

    inst = await runner.spawn("alpha", trust=True)

    assert inst.status is InstanceStatus.RUNNING
    assert is_trusted(proj.path, empty_trust)  # trust was written as part of the spawn


async def test_spawn_trust_true_invalid_option_does_not_trust(
    runner_config, tmp_path, monkeypatch
):
    # #775 regression: --trust must be applied AFTER option validation, so a rejected
    # spawn (here a control-char custom name) never leaves the directory trusted.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    empty_trust = tmp_path / "untrusted.json"
    empty_trust.write_text("{}")
    runner = SessionRunner(config, claude_json=empty_trust)
    proj = runner._resolve_project("alpha")

    with pytest.raises(InvalidSpawnOption):
        await runner.spawn("alpha", trust=True, custom_name="bad\x01name")

    assert not is_trusted(proj.path, empty_trust)  # validation failed → no trust side effect


async def test_spawn_trust_true_capacity_full_does_not_trust(runner_config, tmp_path, monkeypatch):
    # #775 regression (Greptile P1, 2nd pass): --trust is applied AFTER the bridge-cap
    # check too, so a start rejected by CapacityExceeded also leaves NO trust side effect.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, _ = runner_config
    config = config.model_copy(deep=True)
    config.instance_defaults.max_bridges = 1
    empty_trust = tmp_path / "untrusted.json"
    empty_trust.write_text("{}")
    runner = SessionRunner(config, claude_json=empty_trust)
    proj = runner._resolve_project("alpha")
    # A live bridge for ANOTHER project already fills the single-bridge cap.
    _seed(runner, "beta", mode="standard", status=InstanceStatus.RUNNING)

    with pytest.raises(CapacityExceeded):
        await runner.spawn("alpha", trust=True)

    assert not is_trusted(proj.path, empty_trust)  # cap rejection → no trust side effect


async def test_spawn_crash_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "crash")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


async def test_spawn_no_poll_loop_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "no_marker")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR
    # markers before the poll loop are still captured
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"


async def test_spawn_trust_error_is_error(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "trust_error")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR


def test_apply_markers_status_branches(runner_config):
    """A slow-but-alive bridge stays STARTING (not a false 'Failed to start');
    only a dead proc or a trust rejection is a terminal ERROR."""
    runner = _make_runner(runner_config)

    def proc(alive: bool) -> subprocess.Popen:
        class _Proc:
            def poll(self):
                return None if alive else 0

        return cast(subprocess.Popen, _Proc())

    def fresh():
        return RemoteControlInstance(project="x", label="x", status=InstanceStatus.STARTING)

    ready = bridge_log.BridgeMarkers(poll_loop_started=True, environment_id="env_x")
    assert ready.is_ready

    # ready + alive -> RUNNING
    i = fresh()
    runner._apply_markers(i, ready, proc(alive=True))
    assert i.status is InstanceStatus.RUNNING

    # alive but no ready marker yet -> stays STARTING (slow start, not a failure)
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(), proc(alive=True))
    assert i.status is InstanceStatus.STARTING

    # exited before readiness -> ERROR (genuine, terminal)
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(), proc(alive=False))
    assert i.status is InstanceStatus.ERROR

    # trust rejected even while alive -> ERROR
    i = fresh()
    runner._apply_markers(i, bridge_log.BridgeMarkers(trust_error=True), proc(alive=True))
    assert i.status is InstanceStatus.ERROR


async def test_watch_startup_alive_unregistered_becomes_error(runner_config, monkeypatch):
    """Regression: a bridge that launches but never registers an environment
    (e.g. it can't authenticate to the controller) stays alive yet uncontrollable.
    It must never be reported RUNNING — it stays STARTING, then fails to ERROR."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")  # alive, never registers
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 0.3  # tiny grace so the test is fast
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING  # NOT a false RUNNING
    assert inst.url is None and inst.environment_id is None
    watch = runner._startup_watches[inst.instance_id]

    await watch
    assert inst.status is InstanceStatus.ERROR  # honest: alive but never usable
    assert inst.url is None and inst.environment_id is None
    assert runner.running_count() == 0

    await runner.stop(inst.instance_id)  # clean up the still-idling fake bridge


async def test_watch_startup_promotes_on_late_registration(runner_config, monkeypatch):
    """A genuinely slow bridge that registers *after* the synchronous readiness
    wait is promoted to RUNNING by the watch — but only once it actually has an
    environment, never on liveness alone."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    monkeypatch.setenv("FAKE_CLAUDE_SLOW", "0.5")  # registers ~0.5s in, after the wait
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 30
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING  # not ready within the 0.2s wait
    assert inst.url is None
    watch = runner._startup_watches[inst.instance_id]

    await watch
    assert inst.status is InstanceStatus.RUNNING
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.url and inst.url.endswith("env_01TESTENVAAAAAAAAAAAAAAAA")

    await runner.stop(inst.instance_id)


async def test_watch_startup_marks_crashed_if_bridge_dies(runner_config, monkeypatch):
    """If a STARTING bridge dies before registering, the watch defers to the same
    rule as the poll loop: an unintended same-dir exit is CRASHED."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stall")
    monkeypatch.setattr("clauster.runner._READY_TIMEOUT", 0.2)
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.05)
    config, claude_json = runner_config
    config.claude.startup_grace_seconds = 30  # long; we kill it well before grace
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.STARTING
    watch = runner._startup_watches[inst.instance_id]

    runner._procs[inst.instance_id].kill()  # die during startup (cross-platform hard kill)
    await watch
    assert inst.status is InstanceStatus.CRASHED


async def test_startup_watch_done_callback_logs_task_exception(runner_config, monkeypatch, caplog):
    # The startup-watch's done-callback logs (never swallows) an unexpected exception
    # raised by _watch_startup, keyed by instance_id (#777). Force the watch coroutine
    # to raise and assert the warning fires with the instance_id.
    runner = _make_runner(runner_config)

    async def _boom(_instance_id: str) -> None:
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(runner, "_watch_startup", _boom)
    with caplog.at_level("WARNING", logger="clauster.runner"):
        runner._start_startup_watch("iid-xyz")
        task = runner._startup_watches["iid-xyz"]
        with contextlib.suppress(RuntimeError):
            await task  # let the coroutine raise; the done-callback then logs it
        await asyncio.sleep(0)  # let the done-callback run
    assert any(
        "startup-watch for iid-xyz failed" in r.message and "watch exploded" in r.message
        for r in caplog.records
    )


async def test_spawn_auto_enables_remote_control(runner_config, monkeypatch):
    """Before launching a bridge, the runner marks remote control acknowledged in
    ~/.claude.json (hasUsedRemoteControl/remoteDialogSeen) so the bridge skips the
    interactive enable prompt a detached-stdin bridge could never answer."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    assert "hasUsedRemoteControl" not in json.loads(claude_json.read_text())

    inst = await runner.spawn("alpha")
    after = json.loads(claude_json.read_text())
    assert after["hasUsedRemoteControl"] is True
    assert after["remoteDialogSeen"] is True
    assert after["projects"]  # existing trust entries preserved

    await runner.stop(inst.instance_id)


async def test_spawn_auto_enable_can_be_disabled(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.claude.auto_enable_remote_control = False
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert "hasUsedRemoteControl" not in json.loads(claude_json.read_text())

    await runner.stop(inst.instance_id)


async def test_stop_unknown_instance_raises(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.stop("00000000-0000-0000-0000-000000000000")  # never spawned


async def test_stop_raises_if_forgotten_after_lock_acquire(runner_config, monkeypatch):
    # TOCTOU defense: stop() re-looks-up the instance INSIDE the per-project lock so a
    # concurrent forget() between the first lookup and the lock can't leave it signalling
    # a de-registered instance. Simulate that race — drop the row as the lock is taken —
    # and assert the inner guard raises UnknownProject rather than proceeding on a stale ref.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    iid = inst.instance_id

    real_lock_for = runner._spawn_lock_for

    def _evicting_lock_for(name):
        # Stand in for a concurrent forget() landing between stop()'s first lookup and
        # its re-lookup under the lock: remove the registry row here.
        runner._instances.pop(iid, None)
        return real_lock_for(name)

    monkeypatch.setattr(runner, "_spawn_lock_for", _evicting_lock_for)
    with pytest.raises(UnknownProject):
        await runner.stop(iid)


def test_external_sessions_by_project(runner_config):
    config, claude_json = runner_config
    runner = _make_runner(runner_config)
    root = config.projects_root

    def session(pid, rel, attribution):
        return WorkingSession(
            pid=pid,
            cwd=root / rel,
            kind="interactive",
            started_at=pid,
            local_uuid=f"uuid-{pid}",
            attribution=attribution,
        )

    runner._sessions = [
        session(111, "alpha", Attribution.EXTERNAL),  # surfaced
        session(222, "alpha", Attribution.EXTERNAL),  # grouped with the first
        session(333, "beta", Attribution.TRACKED),  # managed -> excluded
        session(444, "nope", Attribution.EXTERNAL),  # not a discovered project -> excluded
    ]

    grouped = runner.external_sessions_by_project()
    assert set(grouped) == {"alpha"}
    assert sorted(s.pid for s in grouped["alpha"]) == [111, 222]


def test_external_sessions_empty_when_none(runner_config):
    runner = _make_runner(runner_config)
    assert runner.external_sessions_by_project() == {}


def test_tracked_sessions_by_instance_groups_and_orders(runner_config):
    """A standard bridge's N live sessions group under its instance, ordered stably (#570)."""
    runner = _make_runner(runner_config)

    def session(pid, parent, started_at, uuid, attribution=Attribution.TRACKED):
        return WorkingSession(
            pid=pid,
            cwd=Path("/tmp") / (parent or "x"),
            kind="interactive",
            started_at=started_at,
            local_uuid=uuid,
            parent_instance=parent,
            attribution=attribution,
        )

    runner._sessions = [
        session(3, "alpha", 300, "uuid-c"),
        session(1, "alpha", 100, "uuid-a"),
        session(2, "alpha", 200, "uuid-b"),
        session(9, "beta", 50, "uuid-z"),
        # excluded: not tracked, or tracked-but-unattributed
        session(4, "alpha", 400, "uuid-d", attribution=Attribution.EXTERNAL),
        session(5, None, 500, "uuid-e"),
    ]

    grouped = runner.tracked_sessions_by_instance()
    assert set(grouped) == {"alpha", "beta"}
    # ordered by (started_at, local_uuid) — stable across polls
    assert [s.local_uuid for s in grouped["alpha"]] == ["uuid-a", "uuid-b", "uuid-c"]
    assert [s.pid for s in grouped["beta"]] == [9]


def test_tracked_sessions_stable_order_on_tie(runner_config):
    """Equal started_at falls back to local_uuid so the render order never flickers (#570)."""
    runner = _make_runner(runner_config)
    runner._sessions = [
        WorkingSession(
            pid=2,
            cwd=Path("/tmp/a"),
            kind="interactive",
            started_at=100,
            local_uuid="uuid-b",
            parent_instance="alpha",
            attribution=Attribution.TRACKED,
        ),
        WorkingSession(
            pid=1,
            cwd=Path("/tmp/a"),
            kind="interactive",
            started_at=100,
            local_uuid="uuid-a",
            parent_instance="alpha",
            attribution=Attribution.TRACKED,
        ),
    ]
    grouped = runner.tracked_sessions_by_instance()
    assert [s.local_uuid for s in grouped["alpha"]] == ["uuid-a", "uuid-b"]


def test_tracked_sessions_empty_when_none(runner_config):
    runner = _make_runner(runner_config)
    assert runner.tracked_sessions_by_instance() == {}


def test_live_session_uuids_matches_by_sanitized_cwd(runner_config):
    """live_session_uuids returns local_uuids of sessions writing into a project's dir (#614)."""
    config, _ = runner_config
    runner = _make_runner(runner_config)
    root = config.projects_root

    def session(uuid, rel, attribution=Attribution.TRACKED):
        return WorkingSession(
            pid=hash(uuid) & 0xFFFF,
            cwd=root / rel,
            kind="interactive",
            started_at=1,
            local_uuid=uuid,
            attribution=attribution,
        )

    runner._sessions = [
        session("u-bridge", "alpha"),  # under alpha -> live for alpha
        session("u-ext", "alpha", attribution=Attribution.EXTERNAL),  # any kind, same dir -> live
        session("u-beta", "beta"),  # different project -> not live for alpha
    ]
    assert runner.live_session_uuids(root / "alpha") == {"u-bridge", "u-ext"}
    assert runner.live_session_uuids(root / "beta") == {"u-beta"}
    assert runner.live_session_uuids(root / "gamma") == set()


def test_live_session_uuids_includes_worktree_sessions(runner_config):
    """A worktree session counts live for its project, in lockstep with the listing (#1020).

    usage.transcript_paths_for now lists worktree transcripts, so this must count them live.
    If it did not, a RUNNING worktree session would render as a dormant conversation and the
    Conversation (fork) picker's `!live && turn_count > 0` filter would surface it — the precise
    situation that filter exists to prevent.
    """
    config, _ = runner_config
    runner = _make_runner(runner_config)
    root = config.projects_root

    def session(uuid, cwd):
        return WorkingSession(
            pid=hash(uuid) & 0xFFFF,
            cwd=cwd,
            kind="interactive",
            started_at=1,
            local_uuid=uuid,
            attribution=Attribution.TRACKED,
        )

    (root / "alpha" / ".claude" / "worktrees" / "sess-1").mkdir(parents=True, exist_ok=True)
    (root / "alpha" / ".claude" / "worktrees-foo" / "sess-2").mkdir(parents=True, exist_ok=True)
    (root / "alpha" / "subdir").mkdir(parents=True, exist_ok=True)

    runner._sessions = [
        session("u-root", root / "alpha"),
        session("u-wt", root / "alpha" / ".claude" / "worktrees" / "sess-1"),
        # Greptile P1: this cwd sanitizes into the project's worktree prefix, so the
        # transcript scan LISTS it — matching liveness on `.claude/worktrees` containment
        # instead left it dormant, and the picker's `!live && turn_count > 0` filter would
        # then offer a RUNNING session as forkable. The two sides must use one rule.
        session("u-wt-lookalike", root / "alpha" / ".claude" / "worktrees-foo" / "sess-2"),
        # Under the project but NOT in the worktree-prefix family: a stray hand-run
        # `claude` must still not be claimed.
        session("u-stray", root / "alpha" / "subdir"),
    ]
    assert runner.live_session_uuids(root / "alpha") == {"u-root", "u-wt", "u-wt-lookalike"}


def test_live_session_uuids_empty_when_no_sessions(runner_config):
    config, _ = runner_config
    runner = _make_runner(runner_config)
    assert runner.live_session_uuids(config.projects_root / "alpha") == set()


# -- external-session adoption (FE-4b, #330) ---------------------------------


class _FakePtr:
    """Stand-in for a live Anthropic bridge-pointer.json (sessionId/env/pid/procStart)."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.proc_start = "1000"
        self.environment_id = "env_x"
        self.session_id = "session_x"


async def test_adopt_promotes_external_standard_session(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = _make_runner(runner_config)
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (runner_config[0].projects_root / "alpha").resolve(),
    )

    inst = await runner.adopt("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.bridge_pid == 4242
    assert inst.resume_mode == "standard"  # standard external bridge
    assert inst.keeper_pid is None  # no keeper for a standard bridge
    assert inst.environment_id == "env_x"
    assert "env_x" in (inst.url or "")
    assert runner.get_instance_for_project("alpha") is inst
    # Persisted so a clauster restart keeps managing it.
    fresh = SessionRunner(config, claude_json=claude_json)
    assert any(v.get("project_name") == "alpha" for v in fresh._persisted.values())


async def test_adopt_refuses_pty_or_dead_external(runner_config, monkeypatch):
    # is_live_standard_bridge is False for a pty (flag-form) bridge OR a pointer that
    # went stale between the poll and the click -> fail closed, never partially adopt.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: False)
    with pytest.raises(AdoptionUnavailable):
        await runner.adopt("alpha")
    assert runner.get_instance_for_project("alpha") is None


async def test_adopt_refuses_when_no_pointer(runner_config, monkeypatch):
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    with pytest.raises(AdoptionUnavailable):
        await runner.adopt("alpha")
    assert runner.get_instance_for_project("alpha") is None


async def test_adopt_refuses_already_managed(runner_config):
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.RUNNING)
    runner._instances[fake.instance_id] = fake
    with pytest.raises(InstanceStillLive):
        await runner.adopt("alpha")


async def test_adopt_unknown_project(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.adopt("does-not-exist")


async def test_adopt_pins_standard_over_stale_persisted_pty_mode(runner_config, monkeypatch):
    # A project that previously ran pty leaves resume_mode="pty" persisted. The LIVE
    # bridge is positively confirmed standard (cmdline gate), so the adopted instance
    # must pin "standard" — else stop() would wrongly use the pty double-SIGINT path.
    config, claude_json = runner_config
    _db_save(
        config.state_dir,
        {
            _IID_ALPHA: {
                "project_name": "alpha",
                "label": "my-alpha",
                "resume_mode": "pty",
                "spawn_mode": "same-dir",
            }
        },
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (runner_config[0].projects_root / "alpha").resolve(),
    )

    inst = await runner.adopt("alpha")
    assert inst.resume_mode == "standard"  # pinned, NOT the stale persisted "pty"
    assert inst.keeper_pid is None
    assert inst.label == "my-alpha"  # persisted label still overlaid
    assert inst.spawn_mode == "same-dir"  # only resume_mode is pinned; other modes kept


def test_adoptable_external_projects(runner_config, monkeypatch):
    config = runner_config[0]
    runner = _make_runner(runner_config)
    root = config.projects_root

    def session(pid, rel):
        return WorkingSession(
            pid=pid,
            cwd=root / rel,
            kind="interactive",
            started_at=pid,
            local_uuid=f"u{pid}",
            attribution=Attribution.EXTERNAL,
        )

    runner._sessions = [session(11, "alpha"), session(22, "beta"), session(33, "gamma")]
    # alpha + beta have a pointer; gamma has none. Only alpha's is a live STANDARD bridge
    # (beta's is a pty/flag-form bridge -> excluded from adoption).
    ptrs = {"alpha": _FakePtr(11), "beta": _FakePtr(22)}
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: ptrs.get(path.name))
    monkeypatch.setattr(
        "clauster.runner.procutil.is_live_standard_bridge", lambda pid, *a, **k: pid == 11
    )
    # Positive attribution (#951): pid 11's cwd IS alpha's directory.
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (root / "alpha").resolve() if pid == 11 else None,
    )
    assert runner.adoptable_external_projects() == {"alpha"}


def test_adoptable_excludes_sanitize_collided_foreign_bridge(runner_config, monkeypatch):
    # The Adopt affordance must apply the same cwd-attribution gate adopt() enforces:
    # a live standard bridge whose actual cwd is ANOTHER project's directory (a
    # sanitize_cwd pointer collision) is not advertised, so an offered Adopt can
    # never 409 on the attribution check.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    root = config.projects_root
    runner._sessions = [
        WorkingSession(
            pid=11,
            cwd=root / "alpha",
            kind="interactive",
            started_at=11,
            local_uuid="u11",
            attribution=Attribution.EXTERNAL,
        )
    ]
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr(11))
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.proc_cwd", lambda pid: (root / "beta").resolve())
    assert runner.adoptable_external_projects() == set()
    # An unreadable cwd is equally unattributable -> not advertised either.
    monkeypatch.setattr("clauster.runner.procutil.proc_cwd", lambda pid: None)
    assert runner.adoptable_external_projects() == set()


def test_adoptable_skips_undiscovered_project(runner_config, monkeypatch):
    # Defensive guard: if a project vanishes from discovery between
    # external_sessions_by_project() and adoptable's own _discovered() snapshot (a
    # filesystem race), the name is skipped — no crash, never adoptable.
    runner = _make_runner(runner_config)
    monkeypatch.setattr(runner, "external_sessions_by_project", lambda: {"ghost-project": []})
    assert runner.adoptable_external_projects() == set()


async def test_adopt_then_stop_uses_single_sigint(runner_config, monkeypatch):
    # The payoff of pinning standard: an adopted session Stops via a clean single SIGINT
    # to the pointer pid (twice=False), never the pty confirming double-SIGINT.
    runner = _make_runner(runner_config)
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (runner_config[0].projects_root / "alpha").resolve(),
    )
    adopted = await runner.adopt("alpha")

    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        SessionRunner,
        "_signal_stop",
        staticmethod(lambda pid, *, twice=False: calls.append((pid, twice))),
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)

    async def _noop_exit(self, *a, **k):
        return None

    monkeypatch.setattr(SessionRunner, "_await_exit", _noop_exit)
    inst = await runner.stop(adopted.instance_id)
    assert inst.status is InstanceStatus.STOPPED
    assert calls == [(4242, False)]  # single SIGINT to the adopted bridge's pid


async def test_poll_does_not_prune_freshly_adopted_running_instance(runner_config, monkeypatch):
    # Race regression: poll_once() is lock-free and snapshots live_projects BEFORE its
    # list_working_sessions suspension. A lock-held adopt() landing during that suspension
    # inserts a RUNNING instance the snapshot never saw — the prune loop must NOT delete it
    # (it targets only non-live STOPPED phantoms). Reproduced deterministically by inserting
    # the instance as a side effect of list_working_sessions, exactly when the race occurs.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    cwd = config.projects_root / "alpha"

    _fake_adopted = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        resume_mode="standard",
        bridge_pid=4242,
    )

    def list_then_adopt(*a, **k):
        # adopt() completes mid-suspension: a RUNNING instance for alpha appears AFTER
        # live_projects (empty — no managed bridge existed at snapshot) was computed.
        runner._instances[_fake_adopted.instance_id] = _fake_adopted
        return [
            WorkingSession(pid=999, cwd=cwd, kind="interactive", started_at=999, local_uuid="u")
        ]

    monkeypatch.setattr(inspector, "list_working_sessions", list_then_adopt)
    await runner.poll_once()
    # The adopted RUNNING instance survived the prune.
    assert runner.get_instance_for_project("alpha") is not None


async def test_poll_drops_phantom_stopped_shadowing_external(runner_config, monkeypatch):
    # A phantom STOPPED instance (e.g. `_stopped_from_persisted` from a stale pointer)
    # must not shadow a live EXTERNAL (flag-form/tmux) bridge at the same cwd: poll_once
    # drops it so the card shows "external session active" instead of Stopped/Resume.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    runner._instances[fake.instance_id] = fake
    sess = WorkingSession(
        pid=999,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=999,
        local_uuid="u",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    # The prune's premise is "the bridge IS alive, just unmanaged", so since #1096 the
    # external session must actually BE a bridge — a hand-run `claude` sharing the cwd is
    # EXTERNAL by design (#820) but is not evidence that this card is a phantom.
    monkeypatch.setattr("clauster.runner.procutil.is_bridge_process", lambda pid: True)
    await runner.poll_once()
    assert runner.get_instance_for_project("alpha") is None  # phantom dropped
    assert "alpha" in runner.external_sessions_by_project()  # now surfaced as external


async def test_poll_attributes_starting_bridge_session_not_external(runner_config, monkeypatch):
    # #713: a freshly-spawned bridge auto-creates its initial session, which `claude agents
    # --json` surfaces immediately — before the bridge's pid reads live (still STARTING, not in
    # live_projects). Reconcile must attribute that session to the STARTING bridge (TRACKED),
    # not classify it EXTERNAL/unmanaged (a transient "not managed by Clauster" phantom row that
    # used to flicker until the bridge went ready).
    config = runner_config[0]
    runner = _make_runner(runner_config)
    # bridge_pid is None -> the live-projects loop skips it, so alpha is NOT in live_projects.
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STARTING, resume_mode="pty"
    )
    runner._instances[fake.instance_id] = fake
    sess = WorkingSession(
        pid=843868,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=843868,
        local_uuid="u-starting",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner.external_sessions_by_project() == {}  # NOT a phantom external row
    tracked = runner.tracked_sessions_by_instance()
    # Keyed by instance_id, not project (#1020 A3).
    key = fake.instance_id
    assert tracked.get(key) and [s.local_uuid for s in tracked[key]] == ["u-starting"]
    inst = runner.get_instance_for_project("alpha")
    assert inst is not None and inst.status is InstanceStatus.STARTING  # still starting, kept


async def test_poll_attributes_starting_worktree_bridge_session_not_external(
    runner_config, monkeypatch
):
    # #713 (worktree arm): a STARTING worktree-spawn bridge runs its session in a per-session
    # worktree under <root>/.claude/worktrees/<id>. During the startup window it must attribute
    # to the bridge by containment (TRACKED), not read EXTERNAL — same race as the same-dir arm.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.STARTING,
        spawn_mode="worktree",
    )  # bridge_pid None -> not in live_projects
    runner._instances[fake.instance_id] = fake
    wt = config.projects_root / "alpha" / ".claude" / "worktrees" / "bridge-cse_x"
    wt.mkdir(parents=True, exist_ok=True)
    sess = WorkingSession(
        pid=843900,
        cwd=wt,
        kind="interactive",
        started_at=843900,
        local_uuid="u-wt",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner.external_sessions_by_project() == {}  # attributed by worktree containment
    tracked = runner.tracked_sessions_by_instance()
    key = fake.instance_id  # keyed by instance_id, not project (#1020 A3)
    assert tracked.get(key) and [s.local_uuid for s in tracked[key]] == ["u-wt"]


async def test_poll_ignores_background_session_at_stopped_cwd(runner_config, monkeypatch):
    # A `claude --bg` background session (agent view, 2.1.139+) at a STOPPED
    # project's cwd is NOT an external bridge: the stopped record must survive
    # (Resume stays available) and nothing surfaces as "external session active".
    config = runner_config[0]
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    runner._instances[fake.instance_id] = fake
    sess = WorkingSession(
        pid=999,
        cwd=config.projects_root / "alpha",
        kind="background",
        state="working",
        started_at=999,
        local_uuid="u-bg",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    # Kept — a bg session is not a bridge.
    assert runner.get_instance_for_project("alpha") is not None
    assert runner.external_sessions_by_project() == {}


async def test_poll_keeps_stopped_instance_without_external_session(runner_config, monkeypatch):
    # The reboot-orphan path still works: a STOPPED-resumable instance with NO live
    # session at its cwd is preserved (so Resume stays available).
    runner = _make_runner(runner_config)
    fake = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    runner._instances[fake.instance_id] = fake
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()
    assert runner.get_instance_for_project("alpha") is not None  # kept — nothing live to yield to


async def test_poll_attributes_hosted_session_not_external(runner_config, monkeypatch):
    # A Clauster hosted (claustrum) session runs no bridge, so the cross-check would
    # see its live `claude` pid and mislabel it EXTERNAL/unmanaged. With the hosted
    # provider wired, it's claimed by agent_pid → HOSTED, never surfaced as external (#592).
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.RUNNING,
                claustrum_process_id="host-1",
                agent_pid=999,
            )
        ]
    )
    sess = WorkingSession(
        pid=999,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=999,
        local_uuid="u-hosted",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.HOSTED
    assert runner._sessions[0].parent_instance == "host-1"
    assert runner.external_sessions_by_project() == {}  # not double-listed as external


async def test_poll_attributes_hosted_session_by_cwd_when_no_pid(runner_config, monkeypatch):
    # Pre-CT-1 daemon: the hosted row has no agent_pid, so attribution falls back to
    # the workspace cwd — still HOSTED, still kept out of the external listing.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.RUNNING,
                claustrum_process_id="host-2",
                agent_pid=None,
            )
        ]
    )
    sess = WorkingSession(
        pid=1234,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=1234,
        local_uuid="u-hosted",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.HOSTED
    assert runner.external_sessions_by_project() == {}


async def test_poll_attributes_orphan_hosted_survivor_not_external(runner_config, monkeypatch):
    # CL-8: an orphan is a CRASHED row whose agent survived a daemon restart. Its live
    # pid must still be claimed (HOSTED), or the survivor reads as EXTERNAL/unmanaged.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.CRASHED,
                is_orphan=True,
                claustrum_process_id="host-3",
                agent_pid=4321,
            )
        ]
    )
    sess = WorkingSession(
        pid=4321,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=4321,
        local_uuid="u-orphan",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.HOSTED
    assert runner.external_sessions_by_project() == {}


async def test_poll_hosted_pid_keeps_colocated_external(runner_config, monkeypatch):
    # A CT-1 hosted session (pid known) must claim ONLY its own pid, not its project
    # cwd — otherwise a genuine EXTERNAL bridge co-located at the same project path
    # would be reclassified HOSTED, hiding it from adoption and the phantom-prune.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.RUNNING,
                claustrum_process_id="host-5",
                agent_pid=900,
            )
        ]
    )
    hosted_sess = WorkingSession(
        pid=900,  # the hosted agent
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=900,
        local_uuid="u-hosted",
    )
    external_sess = WorkingSession(
        pid=901,  # an unrelated bridge in the same project dir
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=901,
        local_uuid="u-external",
    )
    monkeypatch.setattr(
        inspector, "list_working_sessions", lambda *a, **k: [hosted_sess, external_sess]
    )
    await runner.poll_once()
    by_uuid = {s.local_uuid: s for s in runner._sessions}
    assert by_uuid["u-hosted"].attribution is Attribution.HOSTED
    assert by_uuid["u-external"].attribution is Attribution.EXTERNAL
    assert "alpha" in runner.external_sessions_by_project()  # external still surfaced


async def test_poll_does_not_claim_stopped_hosted_row(runner_config, monkeypatch):
    # A STOPPED hosted row has no live process; its old pid could be reused, so it must
    # NOT be claimed — a live session at that pid is then a genuine EXTERNAL one.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.STOPPED,
                claustrum_process_id="host-4",
                agent_pid=555,
            )
        ]
    )
    sess = WorkingSession(
        pid=555,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=555,
        local_uuid="u-reused",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.EXTERNAL


async def test_poll_skips_hosted_row_without_process_id(runner_config, monkeypatch):
    # A hosted row with no claustrum_process_id can't be a claim key — it's skipped, and
    # a live session at the same cwd is left to attribute normally (EXTERNAL here).
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="alpha",
                label="hosted:alpha",
                channel="hosted",
                status=InstanceStatus.RUNNING,
                claustrum_process_id=None,
                agent_pid=42,
            )
        ]
    )
    sess = WorkingSession(
        pid=42,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=42,
        local_uuid="u",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.EXTERNAL


async def test_poll_hosted_cwd_fallback_skips_undiscovered_project(runner_config, monkeypatch):
    # Pre-CT-1 hosted row whose project isn't discovered: the cwd fallback finds no path,
    # so nothing is claimed and an unrelated live session stays EXTERNAL (no crash).
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner.set_hosted_provider(
        lambda: [
            RemoteControlInstance(
                project="ghost-project",  # not under projects_root
                label="hosted:ghost",
                channel="hosted",
                status=InstanceStatus.RUNNING,
                claustrum_process_id="host-6",
                agent_pid=None,
            )
        ]
    )
    sess = WorkingSession(
        pid=77,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=77,
        local_uuid="u",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert runner._sessions[0].attribution is Attribution.EXTERNAL


async def test_poll_keeps_live_bridge_managed_despite_nonrunning_status(
    runner_config, monkeypatch
):
    # Ownership of a cwd is keyed on the bridge PROCESS being alive, not on the
    # instance's status. A fresh pty bridge that connected but never printed a
    # scrapeable connect URL is left pre-ready (here: ERROR) yet is still OUR live
    # process — its session must be attributed managed (TRACKED), never flagged
    # external and phantom-deleted. Regression for the "external session active"
    # misclassification of a clauster-launched pty bridge.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    # A live process whose cmdline reads as a bridge (is_live_bridge checks both the
    # PID/start-time AND a `claude … remote-control` cmdline); the extra argv tokens are
    # ignored by the sleeping stand-in.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
    )
    try:
        fake = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.ERROR,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )
        runner._instances[fake.instance_id] = fake
        sess = WorkingSession(
            pid=999,
            cwd=config.projects_root / "alpha",
            kind="interactive",
            started_at=999,
            local_uuid="u",
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
        # The session's worker pid descends from the (live) bridge process — the #820
        # ownership signal. Stub the psutil walk so pid 999 reads as bridge-owned;
        # assert the runner roots ownership at the bridge's live pid.
        monkeypatch.setattr(
            procutil, "owned_pids", lambda roots: {999} if proc.pid in set(roots) else set()
        )
        await runner.poll_once()
        # Live bridge: NOT phantom-deleted.
        assert runner.get_instance_for_project("alpha") is not None
        # the session at its cwd is managed (TRACKED), so it is not surfaced as external
        assert "alpha" not in runner.external_sessions_by_project()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_poll_external_session_at_live_bridge_cwd_stays_external(runner_config, monkeypatch):
    # #820: an external SSH/terminal `claude` the operator ran by hand IN a live
    # bridge's project dir shares that cwd. It must stay EXTERNAL (attribution keys on
    # process ownership, not cwd) while the bridge's genuine child at the same cwd is
    # TRACKED — the mis-attribution that folded a hand-run session under the bridge.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
    )
    try:
        fake = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.RUNNING,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )
        runner._instances[fake.instance_id] = fake
        cwd = config.projects_root / "alpha"
        owned = WorkingSession(
            pid=1001, cwd=cwd, kind="interactive", started_at=1, local_uuid="u-owned"
        )
        external = WorkingSession(
            pid=2002, cwd=cwd, kind="interactive", started_at=2, local_uuid="u-ext"
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [owned, external])
        # The bridge owns only its real child (1001); the hand-run SSH session (2002)
        # descends from sshd/a shell, not the bridge.
        monkeypatch.setattr(
            procutil, "owned_pids", lambda roots: {1001} if proc.pid in set(roots) else set()
        )
        await runner.poll_once()
        tracked = runner.tracked_sessions_by_instance()
        # Keyed by instance_id, not project (#1020 A3).
        assert [s.local_uuid for s in tracked.get(fake.instance_id, [])] == ["u-owned"]
        assert "alpha" in runner.external_sessions_by_project()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_poll_inprocess_pty_session_owned_via_bridge_pid(runner_config, monkeypatch):
    # #820 review (HIGH): a single-session flag-form pty (`claude --remote-control`) can
    # report its `agents --json` pid as the BRIDGE process itself (in-process, no child
    # worker), and a reattached pty with a rotated/missing keeper sidecar contributes
    # only bridge_pid (keeper_pid None). owned_pids includes the roots themselves, so
    # such a session (pid == bridge_pid) stays TRACKED, not flipped to EXTERNAL — even
    # though the child walk is empty.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
    )
    try:
        fake = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.RUNNING,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )  # keeper_pid stays None (rotated/missing sidecar)
        runner._instances[fake.instance_id] = fake
        sess = WorkingSession(
            pid=proc.pid,  # the session IS the bridge process (in-process pty)
            cwd=config.projects_root / "alpha",
            kind="interactive",
            started_at=1,
            local_uuid="u-inproc",
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
        # In-process: the bridge has no child worker (empty child walk), so ownership
        # comes from the root pid itself — owned_pids returns exactly the roots.
        monkeypatch.setattr(procutil, "owned_pids", lambda roots: set(roots))
        await runner.poll_once()
        tracked = runner.tracked_sessions_by_instance()
        # Keyed by instance_id, not project (#1020 A3).
        assert [s.local_uuid for s in tracked.get(fake.instance_id, [])] == ["u-inproc"]
        assert "alpha" not in runner.external_sessions_by_project()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_poll_pidless_stopped_row_never_absorbs_an_external_session(
    runner_config, monkeypatch
):
    # #820 guard for the per-instance rewrite (#1020 A3). A project can hold a pid-less
    # STOPPED/ERROR row alongside a LIVE bridge (#778 multi-bridge), and the liveness test
    # that builds the candidate list is project-level, so that dead row would otherwise be
    # a candidate. Being absent from the ownership map, it is the one `_select_owner` falls
    # back to — so an operator's hand-run `claude` in the project dir would be re-labelled
    # TRACKED under a bridge that isn't even running, and would vanish from the external
    # list (and with it the Adopt affordance) instead of surfacing.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
    )
    try:
        live = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.RUNNING,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )
        dead = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.STOPPED,
        )  # no bridge_pid / keeper_pid — owns nothing
        runner._instances[live.instance_id] = live
        runner._instances[dead.instance_id] = dead
        cwd = config.projects_root / "alpha"
        hand_run = WorkingSession(
            pid=999, cwd=cwd, kind="interactive", started_at=1, local_uuid="u-handrun"
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [hand_run])
        # The live bridge owns its own child only; 999 descends from sshd/a shell.
        monkeypatch.setattr(procutil, "owned_pids", lambda roots: {200})
        await runner.poll_once()
        tracked = runner.tracked_sessions_by_instance()
        assert tracked.get(dead.instance_id, []) == []
        assert tracked.get(live.instance_id, []) == []
        ext = runner.external_sessions_by_project().get("alpha", [])
        assert [s.local_uuid for s in ext] == ["u-handrun"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_poll_ownership_separates_colocated_bridges_at_one_cwd(runner_config, monkeypatch):
    # #820 + #1020 A3: a standard and a pty bridge (or N pty) may be co-located at one
    # project root — each owns distinct worker pids. Both children stay TRACKED (neither
    # bridge flips the other's genuine children to EXTERNAL) and a session owned by
    # neither is EXTERNAL — that is the #820 half, unchanged.
    #
    # What CHANGED for #1020 A3: each child now attributes to the bridge that actually
    # owns it, instead of both landing in one project-keyed bucket. That bucket is what
    # made the dashboard's standard-bridge row list independent interactive sessions as
    # if it owned them.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
        )
        for _ in range(2)
    ]
    try:
        ids = []
        for mode, p in zip(("pty", "pty"), procs, strict=True):
            inst = RemoteControlInstance(
                project="alpha",
                label="alpha",
                status=InstanceStatus.RUNNING,
                resume_mode=mode,
                bridge_pid=p.pid,
                bridge_proc_start=procutil.proc_create_time(p.pid),
            )
            runner._instances[inst.instance_id] = inst
            ids.append(inst.instance_id)
        cwd = config.projects_root / "alpha"
        sessions = [
            WorkingSession(pid=1001, cwd=cwd, kind="interactive", started_at=1, local_uuid="a"),
            WorkingSession(pid=2002, cwd=cwd, kind="interactive", started_at=2, local_uuid="b"),
            WorkingSession(pid=9999, cwd=cwd, kind="interactive", started_at=3, local_uuid="ext"),
        ]
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: sessions)
        # Each bridge owns one distinct child; the runner must union both roots for the
        # shared cwd before the walk sees them.
        owned_of = {procs[0].pid: 1001, procs[1].pid: 2002}
        monkeypatch.setattr(
            procutil,
            "owned_pids",
            lambda roots: {owned_of[r] for r in roots if r in owned_of},
        )
        await runner.poll_once()
        tracked = runner.tracked_sessions_by_instance()
        # Separated, one session per owning bridge — NOT one shared bucket keyed "alpha".
        assert [s.local_uuid for s in tracked.get(ids[0], [])] == ["a"]
        assert [s.local_uuid for s in tracked.get(ids[1], [])] == ["b"]
        assert "alpha" not in tracked
        ext = runner.external_sessions_by_project().get("alpha", [])
        assert [s.local_uuid for s in ext] == ["ext"]
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=5)


async def test_poll_indeterminate_ownership_fails_closed_external(runner_config, monkeypatch):
    # #820 review (Greptile P1): on a host where the process tree can't be READ
    # (AccessDenied — hidepid/hardened /proc/restricted container), owned_pids contributes
    # only the root pid, not the children. A keyed cwd still gates, so a child session the
    # walk can't prove is owned reads EXTERNAL — fail closed, never silently re-enabling
    # the cwd-only join #820 removed. (A pid-less STARTING pty is still cwd-only, above.)
    config = runner_config[0]
    runner = _make_runner(runner_config)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "claude", "remote-control"]
    )
    try:
        fake = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.RUNNING,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )
        runner._instances[fake.instance_id] = fake
        sess = WorkingSession(
            pid=54321,  # a child whose ancestry the walk can't read → unprovable
            cwd=config.projects_root / "alpha",
            kind="interactive",
            started_at=1,
            local_uuid="u-unverifiable",
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
        # AccessDenied on the tree → only the root pid is owned, not the child.
        monkeypatch.setattr(procutil, "owned_pids", lambda roots: set(roots))
        await runner.poll_once()
        assert runner.tracked_sessions_by_instance().get("alpha", []) == []
        ext = runner.external_sessions_by_project().get("alpha", [])
        assert [s.local_uuid for s in ext] == ["u-unverifiable"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_rediscover_overlays_persisted_state(runner_config, monkeypatch):
    config, claude_json = runner_config
    # alpha was intentionally stopped with a custom label; zeta is stale/persisted.
    _IID_ZETA = "cccccccc-0000-0000-0000-000000000001"
    _db_save(
        config.state_dir,
        {
            _IID_ALPHA: {
                "project_name": "alpha",
                "label": "my-alpha",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
            _IID_ZETA: {
                "project_name": "zeta",
                "label": "zeta",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
        },
    )
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "1000", "env_x", "session_x"

    # Only alpha has a live bridge; beta/gamma/zeta resolve to no pointer.
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    monkeypatch.setattr("clauster.procutil.jiffies_to_epoch", lambda j: 12345.0)

    await runner.rediscover()
    insts = {i.project: i for i in runner.list_instances()}

    assert set(insts) == {"alpha"}  # no phantom from a persisted-but-dead entry
    assert insts["alpha"].label == "my-alpha"  # persisted label overlaid
    assert insts["alpha"].intentional_stop is False  # a live bridge is not "stopped"


async def test_rediscover_standard_rebinds_newest_debug_log(runner_config, monkeypatch):
    """A rediscovered standard survivor re-binds its live tail to the newest log it wrote.

    Without this the tail source is None after a restart and `/ws/bridge-log` 1008s every
    connect — the bridge is alive but the operator is blind to its logs (#584). The live
    survivor is the most recent spawn, so the highest `<ms>-<seq>` the filename encodes (not
    its `.raw/.stderr/.keeper` siblings) is the source.
    """
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    old = log_dir / "alpha-1700000000000-0.log"
    new = log_dir / "alpha-1700000000001-1.log"  # higher <ms>-<seq> → the live survivor
    for p in (old, new):
        p.write_text("hello\n")
    # Sibling spawn-set files of the newest spawn must never be chosen as the tail source.
    for sib in (
        "alpha-1700000000001-1.raw.log",
        "alpha-1700000000001-1.stderr.log",
        "alpha-1700000000001-1.keeper.json",
    ):
        (log_dir / sib).write_text("x")
    # A SIBLING PROJECT whose name shares alpha's prefix (PROJECT_NAME_RE allows `-`): its log
    # matches the bare `alpha-*.log` glob, and binding to it would leak another project's tail.
    # Give it the highest <ms>-<seq> so a prefix-only match would wrongly win (#584 review).
    (log_dir / "alpha-2-1700000000009-1.log").write_text("NOT alpha's log\n")

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    monkeypatch.setattr("clauster.procutil.jiffies_to_epoch", lambda j: 12345.0)

    await runner.rediscover()
    inst = runner.get_instance_for_project("alpha")
    assert inst is not None
    assert inst.status is InstanceStatus.RUNNING
    assert inst.bridge_debug_log_path == new  # highest <ms>-<seq>, not a sibling
    assert inst.bridge_raw_log_path == new  # redaction off → raw == debug


def test_latest_debug_log_for_tolerates_oserror(runner_config, monkeypatch):
    """A filesystem error listing the log dir degrades to None, never raises — so a transient
    FS fault can't crash startup rediscover; the tail simply stays unbound instead (#584)."""
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    def boom(self, pattern):
        raise OSError("log dir unreadable")

    monkeypatch.setattr(Path, "glob", boom)
    assert runner._latest_debug_log_for("alpha") is None


def test_latest_debug_log_for_returns_none_when_no_match(runner_config):
    """No anchored `<name>-<ms>-<seq>.log` for the project → None (so the tail stays unbound
    and `/ws/bridge-log` 1008s, rather than binding a sibling's log). Covers a dir holding
    only a prefix-sharing sibling project and the project's own non-tail spawn-set kin."""
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    log_dir = config.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-2-1700000000000-0.log").write_text("sibling project\n")
    (log_dir / "alpha-1700000000000-0.raw.log").write_text("not the public tail\n")
    assert runner._latest_debug_log_for("alpha") is None


async def test_adopt_leaves_log_path_unset(runner_config, monkeypatch):
    """Adopting an EXTERNAL standard bridge leaves the tail source None — Clauster never
    spawned it, so there is no Clauster-written log to bind (unlike a rediscovered
    survivor). Guards against the #584 fix wrongly grafting a stale log onto an adoptee."""
    runner = _make_runner(runner_config)
    log_dir = runner_config[0].state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "alpha-1700000000000-0.log").write_text("not mine\n")  # an unrelated bridge's log
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (runner_config[0].projects_root / "alpha").resolve(),
    )

    inst = await runner.adopt("alpha")
    assert inst.bridge_debug_log_path is None
    assert inst.bridge_raw_log_path is None


async def test_rediscover_tolerates_invalid_persisted_mode(runner_config, monkeypatch):
    config, claude_json = runner_config
    _db_save(
        config.state_dir,
        {
            _IID_ALPHA: {
                "project_name": "alpha",
                "label": "alpha",
                "intentional_stop": True,
                "spawn_mode": "BOGUS",
                "permission_mode": "NOPE",
            },
        },
    )
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "1000", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)
    monkeypatch.setattr("clauster.procutil.jiffies_to_epoch", lambda j: 12345.0)

    await runner.rediscover()
    inst = runner.get_instance_for_project("alpha")
    assert inst is not None  # didn't crash on the bad persisted modes
    assert inst.spawn_mode == "same-dir" and inst.permission_mode == "default"  # fell back


async def test_rediscover_tolerates_unparseable_proc_start(runner_config, monkeypatch):
    # A garbled/future-format pointer procStart must not crash startup with a bare
    # int() ValueError; it degrades to bridge_proc_start=None (cmdline-only
    # liveness), mirroring procutil.is_live_bridge so the two paths can't disagree.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    class FakePtr:
        pid, proc_start, environment_id, session_id = 4242, "not-a-number", "env_x", "session_x"

    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.pointers.is_live", lambda ptr: True)

    await runner.rediscover()  # must not raise
    inst = runner.get_instance_for_project("alpha")
    assert inst is not None  # rediscovered despite the unparseable procStart
    assert inst.bridge_proc_start is None  # degraded, not crashed


def test_reconcile_status_transitions():
    def inst(status, intentional=False):
        return RemoteControlInstance(
            project="x", label="x", status=status, intentional_stop=intentional
        )

    i = inst(InstanceStatus.RUNNING, intentional=True)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.STOPPED

    i = inst(InstanceStatus.RUNNING, intentional=False)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.CRASHED

    i = inst(InstanceStatus.STARTING)
    SessionRunner._reconcile_status(i, alive=True)
    # Liveness alone must NOT promote: a bridge can be alive yet never have
    # registered an environment (then it is unusable). Promotion is the
    # startup-watch's job, gated on a real environment registration.
    assert i.status is InstanceStatus.STARTING

    i = inst(InstanceStatus.STARTING, intentional=False)
    SessionRunner._reconcile_status(i, alive=False)
    assert i.status is InstanceStatus.CRASHED  # died during startup

    i = inst(InstanceStatus.RUNNING)
    SessionRunner._reconcile_status(i, alive=True)
    assert i.status is InstanceStatus.RUNNING  # unchanged


def _argv_of(instance) -> list[str]:
    """The argv the fake bridge recorded for its most recent spawn."""
    from pathlib import Path

    return json.loads(Path(str(instance.bridge_debug_log_path) + ".argv.json").read_text())


async def test_resume_reuses_modes_and_backfills_session(runner_config, monkeypatch):
    # A reconnecting bridge re-logs the environment + poll loop but NOT
    # "Created initial session", so the session id must be recovered from the
    # bridge-pointer — otherwise session_url (the primary deep link) breaks.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    first = await runner.spawn("alpha", permission_mode="acceptEdits")
    assert first.status is InstanceStatus.RUNNING
    assert first.error_detail is None  # a clean start records no failure reason
    await runner.stop(first.instance_id)

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "resume")

    class FakePtr:
        pid, proc_start = 1, "1000"
        environment_id = "env_01TESTENVAAAAAAAAAAAAAAAA"
        session_id = "session_01RESUMEDBBBBBBBBBBB"

    # accept the extra claude_projects_dir arg the #867 L2 pre-spawn health-check passes
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda *a, **k: FakePtr())

    resumed = await runner.resume(first.instance_id)
    assert resumed.status is InstanceStatus.RUNNING
    # session id backfilled from the pointer (the resume log omitted it)…
    assert resumed.starter_session_id == "session_01RESUMEDBBBBBBBBBBB"
    assert resumed.session_url and "session_01RESUMEDBBBBBBBBBBB" in resumed.session_url
    # …and resume reused the stored permission mode rather than the config default.
    argv = _argv_of(resumed)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    await runner.stop(first.instance_id)


async def test_resume_keeps_recorded_mode_when_config_flips(runner_config, monkeypatch):
    # Regression: a bridge launched in "standard" must stay standard on resume
    # even if clauster.yml is later flipped to "pty" (e.g. a config edit + restart).
    # The mode is recorded on the instance at first launch; the global config only
    # seeds brand-new bridges. Without this, stop() (reads instance.resume_mode)
    # and resume() (used to re-derive from config) disagree about the same bridge.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    first = await runner.spawn("alpha")
    assert first.resume_mode == "standard"
    assert first.status is InstanceStatus.RUNNING
    await runner.stop(first.instance_id)

    # Simulate editing clauster.yml -> launch_mode: pty underneath the stopped bridge.
    runner._config.claude.launch_mode = "pty"

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "resume")

    class FakePtr:
        pid, proc_start = 1, "1000"
        environment_id = "env_01TESTENVAAAAAAAAAAAAAAAA"
        session_id = "session_01RESUMEDBBBBBBBBBBB"

    # accept the extra claude_projects_dir arg the #867 L2 pre-spawn health-check passes
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda *a, **k: FakePtr())

    resumed = await runner.resume(first.instance_id)
    # Honored the recorded mode: stayed standard, did not cross to the pty
    # flag form / keeper despite the config now saying pty.
    assert resumed.resume_mode == "standard"
    assert resumed.keeper_pid is None
    argv = _argv_of(resumed)
    assert "remote-control" in argv and "--remote-control" not in argv
    await runner.stop(first.instance_id)


async def test_resume_unknown_instance_rejected(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        # Never spawned -> nothing to resume.
        await runner.resume("00000000-0000-0000-0000-000000000000")


async def test_spawn_captures_stderr_detail_on_failure(runner_config, monkeypatch):
    # A startup failure whose reason goes only to stderr (not --debug-file) must
    # still surface: clauster routes stdout+stderr to a file and captures the tail.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "stderr_error")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.ERROR
    assert inst.error_detail and "HTTP 401" in inst.error_detail


def test_capture_error_detail_no_log_is_noop():
    inst = RemoteControlInstance(project="x", label="x", bridge_debug_log_path=None)
    SessionRunner._capture_error_detail(inst)  # must not raise
    assert inst.error_detail is None


def test_capture_error_detail_unreadable_is_noop(tmp_path):
    # stderr sibling is a directory -> read_text raises OSError -> swallowed.
    log = tmp_path / "b.log"
    (tmp_path / "b.stderr.log").mkdir()
    inst = RemoteControlInstance(project="x", label="x", bridge_debug_log_path=log)
    SessionRunner._capture_error_detail(inst)
    assert inst.error_detail is None


def test_capture_error_detail_redacts_session_tokens(tmp_path):
    # error_detail is the bridge's captured stdout+stderr tail, now surfaced inline in the UI
    # (#313). The startup banner prints env_/session_/cse_ bearer-credential ids; a crash after
    # the banner would otherwise paint a LIVE token onto the project card. Capture must redact
    # (same posture as the at-rest log mirror) and strip ANSI first so an escape-split id can't
    # slip through.
    log = tmp_path / "b.log"
    (tmp_path / "b.stderr.log").write_text(
        "Created initial session session_01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "\x1b[31menv_01BX5ZZKBKACTAV9WEVGEMMVRZ\x1b[0m failed to start\n",
        encoding="utf-8",
    )
    inst = RemoteControlInstance(project="x", label="x", bridge_debug_log_path=log)
    SessionRunner._capture_error_detail(inst)
    assert inst.error_detail is not None
    assert "session_01ARZ3NDEKTSV4RRFFQ69G5FAV" not in inst.error_detail
    assert "env_01BX5ZZKBKACTAV9WEVGEMMVRZ" not in inst.error_detail
    assert "<redacted>" in inst.error_detail  # ids masked, not dropped
    assert "\x1b[" not in inst.error_detail  # ANSI stripped
    # ...but the failure reason must SURVIVE redaction — masking secrets must not wipe the
    # diagnostic context the card exists to show (CodeRabbit).
    assert "Created initial session" in inst.error_detail
    assert "failed to start" in inst.error_detail


def test_read_markers_tolerates_non_utf8_bytes(tmp_path):
    # The debug log is raw bridge output; a stray non-UTF-8 byte must NOT raise
    # UnicodeDecodeError (a ValueError, which the read's OSError guard would not
    # catch) and lose every marker — markers around the garbage still parse.
    log = tmp_path / "bridge.log"
    log.write_bytes(
        b"[bridge:work] Starting poll loop spawnMode=same-dir environmentId=env_ABC123\n"
        b"garbage: \xff\xfe\x80 not utf-8\n"
    )
    markers = SessionRunner._read_markers(log)
    assert markers.poll_loop_started is True
    assert markers.environment_id == "env_ABC123"


def test_read_sidecar_non_utf8_returns_none(tmp_path):
    # A non-UTF-8 sidecar raises UnicodeDecodeError (a ValueError) on read; the
    # invalid -> None contract must hold so readiness polling isn't broken.
    sidecar = tmp_path / "x.keeper.json"
    sidecar.write_bytes(b"\xff\xfe\x00not utf-8")
    assert SessionRunner._read_sidecar(sidecar) is None


async def test_poll_forever_continues_after_unexpected_error(runner_config, monkeypatch, caplog):
    # An unexpected error from poll_once is caught by the loop and never propagated, so
    # crash-detection/reconciliation survives a one-off failure; the loop reaches its
    # sleep (which we make exit the test). Mirrors the metrics loop's regression test.
    runner = _make_runner(runner_config)

    async def _boom():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(runner, "poll_once", _boom)
    monkeypatch.setattr("clauster.runner.asyncio.sleep", _raise_cancelled)
    with caplog.at_level(logging.ERROR, logger="clauster.runner"):
        with pytest.raises(asyncio.CancelledError):  # only the sleep's cancel escapes
            await runner._poll_forever()
    # The swallow path must stay observable — a refactor dropping the log is caught here.
    assert any("poll_once failed; continuing" in r.message for r in caplog.records)


async def test_poll_forever_propagates_cancel_from_poll(runner_config, monkeypatch):
    # A CancelledError from poll_once itself is re-raised (not swallowed by the loop),
    # so task cancellation stops the poll loop promptly. Mirrors the metrics loop test.
    runner = _make_runner(runner_config)

    async def _cancel():
        raise asyncio.CancelledError

    monkeypatch.setattr(runner, "poll_once", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await runner._poll_forever()


# ----- get_instance_for_project mirrors the project-keyed display (#778) -----


def _seed(runner, project, *, mode, status):
    inst = RemoteControlInstance(project=project, label=project, resume_mode=mode, status=status)
    runner._instances[inst.instance_id] = inst
    return inst


def test_get_instance_for_project_mirrors_displayed_instance(runner_config):
    """The name fallback targets the instance the project-keyed client displays.

    The pre-#779 dashboard folds ``GET /api/instances`` as ``map[project] = row`` —
    the LAST-registered row wins the project card — and sends the project name on
    Stop/Resume/Forget/QR. The resolver must pick exactly that displayed instance,
    in ANY status: a "smarter" pick (a live bridge, the oldest row) can act on a
    bridge the operator cannot see (Greptile P1 on #797) — e.g. Resume of the
    displayed stopped pty card bouncing off a hidden live standard bridge.
    """
    runner = _make_runner(runner_config)
    _seed(runner, "alpha", mode="standard", status=InstanceStatus.RUNNING)
    stopped_pty = _seed(runner, "alpha", mode="pty", status=InstanceStatus.STOPPED)
    # The stopped pty registered LAST → it owns the project card (a resumable card
    # in the UI); name-identity actions must target it, not the live standard.
    assert runner.get_instance_for_project("alpha") is stopped_pty
    # A newer registration takes over the card — and with it, name-identity actions.
    newest = _seed(runner, "alpha", mode="pty", status=InstanceStatus.RUNNING)
    assert runner.get_instance_for_project("alpha") is newest
    assert runner.get_instance_for_project("ghost") is None


def test_has_running_instance_sees_running_pty_behind_stale_display(runner_config):
    """The liveness flag must not miss a RUNNING pty hidden behind the displayed card (#778).

    ``get_instance_for_project`` mirrors the client's last-registered display pick —
    which can be a stopped/starting row while another instance for the project is
    RUNNING — so ``_bridge_running`` routes through this any-RUNNING scan instead.
    """
    runner = _make_runner(runner_config)
    _seed(runner, "alpha", mode="standard", status=InstanceStatus.STARTING)
    assert runner.has_running_instance("alpha") is False  # nothing RUNNING yet
    running_pty = _seed(runner, "alpha", mode="pty", status=InstanceStatus.RUNNING)
    stopped = _seed(runner, "alpha", mode="standard", status=InstanceStatus.STOPPED)
    # The displayed (last-registered) card is the stopped standard — the RUNNING
    # pty behind it must still flip the liveness flag.
    assert runner.get_instance_for_project("alpha") is stopped
    assert runner.get_instance_for_project("alpha") is not running_pty
    assert runner.has_running_instance("alpha") is True
    assert runner.has_running_instance("beta") is False


# ----- audited coverage gaps (2026-07 audit) ----------------------------


async def test_spawn_ensure_helpers_unchanged_skips_info_logs(runner_config, monkeypatch):
    # runner.py 832->845 + 850->862: when both pre-spawn ensure helpers report "no
    # change" (flags already set, hook already installed), spawn proceeds without
    # the changed-state info logs and still latches both once-per-runner gates.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.claude.resume_recap = True
    monkeypatch.setattr("clauster.runner.ensure_remote_control_enabled", lambda p: False)
    monkeypatch.setattr("clauster.runner.ensure_recap_hook_installed", lambda p: False)
    runner = SessionRunner(config, claude_json=claude_json)

    inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert runner._rc_setting_ensured and runner._recap_hook_ensured
    await runner.stop(inst.instance_id)


async def test_spawn_survives_ensure_helper_write_failures(runner_config, monkeypatch, caplog):
    # runner.py 838-844 + 856-861: both pre-spawn writes are best-effort — an OSError
    # writing either ~/.claude.json flag or the recap hook WARNS (never silent) and
    # the spawn continues; the startup watch stays the honest failure gate.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.claude.resume_recap = True

    def _readonly(path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("clauster.runner.ensure_remote_control_enabled", _readonly)
    monkeypatch.setattr("clauster.runner.ensure_recap_hook_installed", _readonly)
    runner = SessionRunner(config, claude_json=claude_json)

    with caplog.at_level("WARNING", logger="clauster.runner"):
        inst = await runner.spawn("alpha")
    assert inst.status is InstanceStatus.RUNNING  # the spawn was not failed over it
    assert any("could not pre-enable remote control" in r.message for r in caplog.records)
    assert any("could not install resume-recap hook" in r.message for r in caplog.records)
    await runner.stop(inst.instance_id)


def test_read_markers_vanished_log_returns_empty(tmp_path):
    # runner.py 1686-1687: a bridge log that vanished (rotation/cleanup race) parses
    # to empty markers instead of raising — readiness polling must survive it.
    markers = SessionRunner._read_markers(tmp_path / "gone.log")
    assert markers == bridge_log.BridgeMarkers()


async def test_start_startup_watch_replaces_prior_watch(runner_config, monkeypatch):
    # runner.py 1795-1796: re-arming the watch for the same instance (respawn during
    # a still-running watch) cancels the old task — never two watches racing to
    # reconcile the same instance.
    runner = _make_runner(runner_config)

    async def _idle(_instance_id: str) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "_watch_startup", _idle)
    runner._start_startup_watch("iid-1")
    first = runner._startup_watches["iid-1"]
    runner._start_startup_watch("iid-1")
    second = runner._startup_watches["iid-1"]
    assert second is not first
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert first.cancelled()
    second.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await second


async def test_watch_startup_returns_when_instance_vanishes(runner_config, monkeypatch):
    # runner.py 1826-1827: the watch wakes to find the instance gone (stopped or
    # forgotten mid-startup) and exits instead of watching a ghost forever.
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.01)
    runner = _make_runner(runner_config)
    await asyncio.wait_for(runner._watch_startup("ghost"), timeout=2.0)


async def test_watch_startup_returns_when_log_path_missing(runner_config, monkeypatch):
    # runner.py 1832-1834: a STARTING instance with a live proc but no debug-log path
    # has nothing to read markers from — the watch defers to the poll loop (returns)
    # rather than spinning or inventing a status.
    monkeypatch.setattr("clauster.runner._STARTUP_WATCH_INTERVAL", 0.01)
    runner = _make_runner(runner_config)
    inst = RemoteControlInstance(project="alpha", label="alpha", status=InstanceStatus.STARTING)
    runner._instances[inst.instance_id] = inst

    class _Alive:
        def poll(self):
            return None

    runner._procs[inst.instance_id] = cast(subprocess.Popen, _Alive())
    await asyncio.wait_for(runner._watch_startup(inst.instance_id), timeout=2.0)
    assert inst.status is InstanceStatus.STARTING  # untouched; the poll loop owns it now


async def test_poll_once_reaps_keeper_pid(runner_config, monkeypatch):
    # runner.py 2399-2400: a pty keeper is Clauster's direct child — poll_once must
    # reap its pid when set, or an organically-exited keeper lingers as a zombie.
    runner = _make_runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        resume_mode="pty",
        keeper_pid=54321,
        bridge_pid=None,
    )
    runner._instances[inst.instance_id] = inst
    reaped: list[int] = []
    monkeypatch.setattr(procutil, "reap_if_exited", reaped.append)
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()
    assert reaped == [54321]  # keeper reaped; bridge_pid None short-circuits the rest


async def test_poll_once_crosscheck_failure_warns_and_returns(runner_config, monkeypatch, caplog):
    # runner.py 2435-2436: a failing `claude agents --json` probe must not kill the
    # poll loop NOR pass silently — it warns and returns, leaving liveness intact.
    runner = _make_runner(runner_config)

    def _boom(*a, **k):
        raise OSError("agents probe wedged")

    monkeypatch.setattr(inspector, "list_working_sessions", _boom)
    with caplog.at_level("WARNING", logger="clauster.runner"):
        await runner.poll_once()
    assert any(
        "cross-check failed" in r.message and "agents probe wedged" in r.message
        for r in caplog.records
    )


async def test_shutdown_cancels_pending_startup_watches(runner_config, monkeypatch):
    # runner.py 2854-2856: shutdown cancels in-flight startup watches (bridges stay
    # running, detached) so uvicorn's graceful stop never waits on a watch task.
    runner = _make_runner(runner_config)

    async def _idle(_instance_id: str) -> None:
        await asyncio.Event().wait()

    async def _done(_instance_id: str) -> None:
        return

    monkeypatch.setattr(runner, "_watch_startup", _idle)
    runner._start_startup_watch("iid-1")
    task = runner._startup_watches["iid-1"]
    # An already-finished watch sits beside the pending one: shutdown must skip
    # cancelling it (no-op) while still cancelling the live watch and clearing both.
    done_task = asyncio.create_task(_done("iid-2"))
    await done_task
    runner._startup_watches["iid-2"] = done_task
    await runner.shutdown()
    assert runner._startup_watches == {}
    assert not done_task.cancelled()  # the finished watch was left alone
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


def test_backfill_starter_session_without_log_path_is_noop(runner_config):
    # runner.py 1760->exit: no pointer and no log path at all -> nothing to recover
    # the session id from; the backfill leaves it None instead of raising.
    inst = RemoteControlInstance(project="alpha", label="alpha")
    SessionRunner._backfill_starter_session(inst, runner_config[0].projects_root / "alpha")
    assert inst.starter_session_id is None


def test_backfill_starter_session_log_without_marker_is_noop(runner_config, tmp_path):
    # runner.py 1762->exit: a log that exists but carries no Unarchive/session marker
    # yields an empty sid — the backfill must not invent one.
    logf = tmp_path / "b.log"
    logf.write_text("no session markers here\n")
    inst = RemoteControlInstance(project="alpha", label="alpha", bridge_debug_log_path=logf)
    SessionRunner._backfill_starter_session(inst, runner_config[0].projects_root / "alpha")
    assert inst.starter_session_id is None


def test_popen_win32_detaches_with_new_process_group(runner_config, monkeypatch, tmp_path):
    # runner.py 1335-1344: the Windows spawn variant — CREATE_NEW_PROCESS_GROUP makes
    # the bridge addressable by CTRL_BREAK for graceful stop, and stdin is detached
    # so a wrapping cmd.exe can never block on an interactive prompt.
    captured: dict = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    runner = _make_runner(runner_config)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("clauster.runner.resolve_binary", lambda b: b)
    monkeypatch.setattr(sys, "platform", "win32")
    runner._popen(
        runner_config[0].projects_root / "alpha",
        tmp_path / "b.log",
        "alpha",
        "same-dir",
        "default",
    )
    assert captured["creationflags"] == 0x00000200
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.STDOUT


def test_conpty_keeper_available_false_on_posix():
    from clauster import runner as runner_mod

    assert runner_mod._conpty_keeper_available() is False


def test_conpty_keeper_available_true_when_winpty_imports(monkeypatch):
    # Force the win32 branch on this POSIX host and inject an importable ``winpty``:
    # the real function reaches its ``import winpty`` -> ``return True`` path.
    import types

    from clauster import runner as runner_mod

    monkeypatch.setattr(runner_mod.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winpty", types.ModuleType("winpty"))
    assert runner_mod._conpty_keeper_available() is True


def test_conpty_keeper_available_false_when_winpty_import_fails(monkeypatch):
    # win32 branch, but ``import winpty`` raises (mapped to None in sys.modules): the
    # real ``except`` arm fails closed to False (no keeper -> Server Mode fallback).
    from clauster import runner as runner_mod

    monkeypatch.setattr(runner_mod.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winpty", None)
    assert runner_mod._conpty_keeper_available() is False


def test_popen_keeper_win32_detaches_process(runner_config, monkeypatch, tmp_path):
    # The Windows keeper detaches with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP so it
    # survives a Clauster restart (start_new_session is a POSIX no-op on Windows).
    captured: dict = {}

    class _FakeProc:
        pid = 4321

    def _fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    runner = _make_runner(runner_config)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(sys, "platform", "win32")
    runner._popen_keeper(
        runner_config[0].projects_root / "alpha",
        tmp_path / "b.keeper.json",
        ["claude", "--remote-control"],
    )
    assert captured["creationflags"] == (0x00000008 | 0x00000200)
    assert "start_new_session" not in captured
    assert captured["stdin"] is subprocess.DEVNULL


def test_is_pty_mode_win32_falls_back_without_pywinpty(runner_config, monkeypatch):
    from clauster import runner as runner_mod

    runner = _make_runner(runner_config)
    monkeypatch.setattr(runner_mod.sys, "platform", "win32")
    monkeypatch.setattr(runner_mod, "_conpty_keeper_available", lambda: False)
    assert runner._is_pty_mode(requested="pty") is False  # Server Mode fallback


def test_is_pty_mode_win32_honors_pty_with_pywinpty(runner_config, monkeypatch):
    from clauster import runner as runner_mod

    runner = _make_runner(runner_config)
    monkeypatch.setattr(runner_mod.sys, "platform", "win32")
    monkeypatch.setattr(runner_mod, "_conpty_keeper_available", lambda: True)
    assert runner._is_pty_mode(requested="pty") is True  # ConPTY keeper honored
    assert runner._is_pty_mode(requested="standard") is False


# ----- #867 L4: stale-pointer prune -----------------------------------------------


def _age(pointer: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(pointer, (old, old))


def _prune_cutoff() -> float:
    return time.time() - _STALE_POINTER_TTL_SECONDS


def test_prune_clears_aged_nonlive_pointer(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _age(pointer, 20)  # older than the 14-day TTL
    runner._prune_one_pointer(config.projects_root / "alpha", _prune_cutoff())
    assert not pointer.exists()


def test_prune_keeps_recent_pointer(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")  # fresh mtime
    runner._prune_one_pointer(config.projects_root / "alpha", _prune_cutoff())
    assert pointer.exists()  # a recent pointer may still back a resume


def test_prune_keeps_live_pointer(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _age(pointer, 20)
    monkeypatch.setattr(pointers, "is_live", lambda ptr: True)  # a live owner
    runner._prune_one_pointer(config.projects_root / "alpha", _prune_cutoff())
    assert pointer.exists()  # never prune a live bridge's pointer


def test_prune_skips_project_symlinked_outside_root(runner_config, tmp_path_factory):
    # #871 review: a symlink under projects_root that resolves OUTSIDE it must not have its
    # (out-of-tree) pointer pruned — that pointer isn't clauster's to GC.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    outside = tmp_path_factory.mktemp("outside_proj")  # a sibling of projects_root, not under it
    (config.projects_root / "linked").symlink_to(outside)
    pdir = runner._claude_projects_dir / pointers.sanitize_cwd(outside.resolve())
    pdir.mkdir(parents=True, exist_ok=True)
    pointer = pdir / "bridge-pointer.json"
    pointer.write_text(
        json.dumps(
            {
                "sessionId": "session_x",
                "environmentId": "env_x",
                "source": "standalone",
                "pid": 81750,
                "procStart": "2590192",
            }
        )
    )
    _age(pointer, 30)
    runner._prune_one_pointer(config.projects_root / "linked", _prune_cutoff())
    assert pointer.exists()  # escaped projects_root -> left intact


def test_prune_noop_without_pointer(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._prune_one_pointer(
        config.projects_root / "alpha", _prune_cutoff()
    )  # no pointer -> no raise


def test_prune_tolerates_clear_oserror(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _age(pointer, 20)

    def _boom(*_a, **_k):
        raise OSError("io error")

    monkeypatch.setattr("clauster.pointers.clear_pointer", _boom)
    runner._prune_one_pointer(
        config.projects_root / "alpha", _prune_cutoff()
    )  # logged, not raised


def test_prune_tolerates_stat_oserror(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    _write_nonlive_pointer(runner, "alpha")
    real_stat = Path.stat

    def _boom(self, **kw):  # only the pointer stat errors; everything else is real
        if self.name == "bridge-pointer.json":
            raise OSError("stat failed")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", _boom)
    runner._prune_one_pointer(
        config.projects_root / "alpha", _prune_cutoff()
    )  # logged, not raised


def test_prune_logs_tolerates_stat_oserror(runner_config, monkeypatch):
    # A per-file stat() OSError inside _prune_logs' _stat closure (a TOCTOU race) is
    # skipped, not raised: the aged set still dates off its surviving sibling and is
    # pruned, and the fresh set survives. Default retention keeps age-pruning at 30d.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._log_dir.mkdir(parents=True, exist_ok=True)
    aged = runner._log_dir / "alpha-1000-0.log"
    aged_raw = runner._log_dir / "alpha-1000-0.raw.log"  # same spawn set as `aged`
    fresh = runner._log_dir / "beta-2000-0.log"
    for p in (aged, aged_raw, fresh):
        p.write_text("x")
    old = time.time() - 40 * 86400  # older than the 30-day default
    os.utime(aged, (old, old))
    os.utime(aged_raw, (old, old))

    real_stat = Path.stat

    def _boom(self, *a, **k):
        # aged_raw's stat fails wherever it's called. To pin the failure to the
        # _stat() closure (not the is_file() listing filter -- 3.12's is_file() stats
        # bare, 3.13's doesn't), is_file() is stubbed below so the listing never stats.
        if self.name == "alpha-1000-0.raw.log":
            raise OSError("stat failed")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "is_file", lambda self, **k: True)  # temp dir holds only test files
    monkeypatch.setattr(Path, "stat", _boom)
    runner._prune_logs(protected=set())  # tolerated, not raised
    monkeypatch.undo()  # restore real stat/is_file so the existence asserts below are honest

    assert not aged.exists()  # aged set still dated off `aged` and pruned
    assert not aged_raw.exists()  # its sibling went with the set despite the stat error
    assert fresh.exists()  # the fresh set survived


def test_prune_clear_returns_false_is_noop(runner_config, monkeypatch):
    # Race guard: the pointer vanished between the stat and the clear -> clear returns False.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _age(pointer, 20)
    monkeypatch.setattr("clauster.pointers.clear_pointer", lambda *a, **k: False)
    runner._prune_one_pointer(config.projects_root / "alpha", _prune_cutoff())  # no raise, no log


async def test_prune_stale_pointers_scans_projects(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    pointer = _write_nonlive_pointer(runner, "alpha")
    _age(pointer, 30)
    await runner._prune_stale_pointers()
    assert not pointer.exists()  # the startup GC found + pruned it


async def test_prune_stale_pointers_tolerates_discover_error(runner_config, monkeypatch):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    def _boom(*_a, **_k):
        raise OSError("cannot list")

    monkeypatch.setattr("clauster.runner.discover_projects_cached", _boom)
    await runner._prune_stale_pointers()  # logged, not raised


async def test_prune_stale_pointers_tolerates_per_project_error(runner_config, monkeypatch):
    # An unexpected per-project error (e.g. a resolve() symlink loop) must not abort the GC.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    def _boom(*_a, **_k):
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(runner, "_prune_one_pointer", _boom)
    await runner._prune_stale_pointers()  # logged per project, never raised


# --------------------------------------------------------------------------- #
# poll_once(side_effects=False): observation without announcement (#1104)
# --------------------------------------------------------------------------- #
def _crashing_runner(runner_config, monkeypatch):
    """A runner holding one RUNNING instance whose process is dead."""
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.inspector.list_working_sessions", lambda *a, **k: [])
    runner._instances["iid-a"] = RemoteControlInstance(
        instance_id="iid-a",
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=4242,
    )
    return runner


async def test_poll_once_observing_reports_a_crash_without_announcing_it(
    runner_config, monkeypatch
):
    # #1104. The MCP read path needs poll_once's `agents --json` cross-check, but a
    # one-shot reader has no standing to announce a death: the live service is already
    # tracking that bridge and would fire its own event, so both firing double-notifies
    # the operator for one crash — from a tool documented as read-only.
    runner = _crashing_runner(runner_config, monkeypatch)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    emitted: list[str] = []
    monkeypatch.setattr(
        SessionRunner, "_emit_lifecycle", lambda self, event, inst: emitted.append(event)
    )

    await runner.poll_once(side_effects=False)

    inst = runner.get_instance("iid-a")
    assert inst is not None
    assert inst.status is InstanceStatus.CRASHED, "an observing poll must still SEE the crash"
    assert emitted == [], "an observing poll must not fire a lifecycle event"


async def test_poll_once_default_still_announces_a_crash(runner_config, monkeypatch):
    # Differential control for the test above: same setup, default flag. Without this,
    # `side_effects=False` could be the only behaviour and nothing would notice.
    runner = _crashing_runner(runner_config, monkeypatch)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    emitted: list[str] = []
    monkeypatch.setattr(
        SessionRunner, "_emit_lifecycle", lambda self, event, inst: emitted.append(event)
    )

    await runner.poll_once()

    assert emitted == ["crash"], "the poll loop must still announce a crash"


async def test_poll_once_observing_does_not_write_the_redacted_mirror(runner_config, monkeypatch):
    # The other write on this path: a live bridge's public log mirror. The live service
    # flushes it on its own loop; a read must not write into the instance's log set.
    runner = _crashing_runner(runner_config, monkeypatch)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    flushed: list[str] = []
    monkeypatch.setattr(
        SessionRunner,
        "_flush_redacted_mirror",
        lambda self, inst: flushed.append(inst.instance_id),
    )

    await runner.poll_once(side_effects=False)
    assert flushed == [], "an observing poll wrote the redacted log mirror"

    await runner.poll_once()
    assert flushed == ["iid-a"], "the poll loop must still flush the mirror"
