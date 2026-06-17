from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import cast

import pytest

from clauster import bridge_log, inspector, procutil
from clauster.models import (
    Attribution,
    InstanceStatus,
    RemoteControlInstance,
    WorkingSession,
)
from clauster.runner import (
    AdoptionUnavailable,
    InstanceStillLive,
    NotTrusted,
    SessionRunner,
    UnknownProject,
)
from clauster.state import StateStore


def _make_runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


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

    stopped = await runner.stop("alpha")
    assert stopped.status is InstanceStatus.STOPPED
    assert stopped.intentional_stop is True
    assert runner.running_count() == 0


async def test_forget_drops_stopped_bridge_from_memory_and_disk(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.spawn("alpha")
    await runner.stop("alpha")
    assert runner.get_instance("alpha") is not None  # a stopped, resumable card

    await runner.forget("alpha")
    assert runner.get_instance("alpha") is None
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
    await runner.spawn("alpha")
    await runner.stop("alpha")
    runner._instances.pop("alpha")  # keep only the persisted overlay
    assert "alpha" in runner._persisted

    await runner.forget("alpha")
    assert "alpha" not in runner._persisted


async def test_forget_refuses_running_bridge(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    await runner.spawn("alpha")
    with pytest.raises(InstanceStillLive):
        await runner.forget("alpha")
    assert runner.get_instance("alpha") is not None  # left intact, never killed
    await runner.stop("alpha")  # cleanup the fake process


async def test_forget_refuses_when_bridge_process_still_live_despite_status(
    runner_config, monkeypatch
):
    # Defense in depth: a STOPPED status with a still-live process must not be forgotten.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    await runner.spawn("alpha")
    inst = runner.get_instance("alpha")
    assert inst is not None
    inst.status = InstanceStatus.STOPPED  # lagging status (e.g. a missed poll)
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: True)
    with pytest.raises(InstanceStillLive):
        await runner.forget("alpha")
    monkeypatch.setattr("clauster.runner.procutil.is_live_bridge", lambda *a, **k: False)
    await runner.stop("alpha")  # cleanup


async def test_forget_refuses_when_keeper_process_still_live(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    await runner.spawn("alpha")
    inst = runner.get_instance("alpha")
    assert inst is not None
    original_bridge_pid = inst.bridge_pid
    inst.status = InstanceStatus.STOPPED
    inst.bridge_pid = None  # skip the bridge check, exercise the keeper branch
    inst.keeper_pid = 4242
    monkeypatch.setattr("clauster.runner.procutil.proc_create_time", lambda pid: 123.0)
    try:
        with pytest.raises(InstanceStillLive):
            await runner.forget("alpha")
    finally:
        inst.bridge_pid = original_bridge_pid
        inst.keeper_pid = None  # clear fake pid so stop() skips _cleanup_keeper(4242)
        await runner.stop("alpha")


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

    await runner.stop("alpha")


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
    await runner.stop("alpha")


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
    await runner.spawn("alpha")
    assert "alpha" in runner._procs
    await runner.stop("alpha")
    assert "alpha" not in runner._procs


async def test_stop_signals_graceful_shutdown(runner_config, monkeypatch):
    # The bridge must receive the graceful stop signal (SIGINT on POSIX,
    # CTRL_BREAK on Windows) and log its shutdown marker before exiting — proves
    # stop() is graceful cross-platform, not a hard kill.
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    runner = _make_runner(runner_config)
    inst = await runner.spawn("alpha")
    log_path = inst.bridge_debug_log_path
    assert log_path is not None

    await runner.stop("alpha")
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

    stopped = await runner.stop("alpha")
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
    await runner.stop("alpha")


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
    await runner.stop("alpha")


async def test_rediscover_resurrects_dead_bridge_and_retains_metadata(runner_config):
    # A discovered project whose bridge isn't alive at rediscover time (its process
    # died while Clauster was down — e.g. a host reboot) is resurrected as a STOPPED,
    # resumable card from its persisted record, AND keeps that record in state.json
    # (not wiped on the post-rediscover save, which would later resume it with
    # default modes — a silent downgrade).
    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "Custom Label",
                "permission_mode": "plan",
                "spawn_mode": "same-dir",
                "resume_mode": "standard",
                "intentional_stop": True,
            }
        }
    )
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.rediscover()  # bridge gone, but a persisted record exists

    inst = runner._instances["alpha"]
    assert inst.status is InstanceStatus.STOPPED  # surfaced as a resumable card
    assert inst.bridge_pid is None and inst.keeper_pid is None  # process is gone
    assert inst.permission_mode == "plan"  # persisted modes preserved
    assert inst.resume_mode == "standard"
    assert inst.label == "Custom Label"
    assert inst.intentional_stop is True  # carried through

    reloaded = StateStore(config.state_dir).load()
    assert reloaded["alpha"]["permission_mode"] == "plan"
    assert reloaded["alpha"]["spawn_mode"] == "same-dir"
    assert reloaded["alpha"]["resume_mode"] == "standard"
    assert reloaded["alpha"]["label"] == "Custom Label"


async def test_rediscover_pty_orphan_resumable_and_skips_unpersisted(runner_config):
    # A "pty" bridge killed by a host reboot returns as a STOPPED card whose
    # resume_mode is preserved, so the UI offers true-resume (--continue restores the
    # conversation) — this is the dogfood bug. A discovered project with NO persisted
    # record is left absent: no phantom card offering to resume nothing.
    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "alpha",
                "spawn_mode": "same-dir",
                "resume_mode": "pty",
                "intentional_stop": False,
            }
        }
    )
    runner = SessionRunner(config, claude_json=claude_json)
    await runner.rediscover()

    alpha = runner._instances["alpha"]
    assert alpha.status is InstanceStatus.STOPPED
    assert alpha.resume_mode == "pty"  # true-resume affordance survives the reboot
    assert alpha.intentional_stop is False  # interrupted, not a deliberate stop
    assert "beta" not in runner._instances  # discovered but unpersisted -> no phantom


async def test_stop_instance_without_pid_marks_stopped(runner_config):
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_pid=None,
    )
    inst = await runner.stop("alpha")
    assert inst.status is InstanceStatus.STOPPED and inst.intentional_stop is True


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
    watch = runner._startup_watches["alpha"]

    await watch
    assert inst.status is InstanceStatus.ERROR  # honest: alive but never usable
    assert inst.url is None and inst.environment_id is None
    assert runner.running_count() == 0

    await runner.stop("alpha")  # clean up the still-idling fake bridge


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
    watch = runner._startup_watches["alpha"]

    await watch
    assert inst.status is InstanceStatus.RUNNING
    assert inst.environment_id == "env_01TESTENVAAAAAAAAAAAAAAAA"
    assert inst.url and inst.url.endswith("env_01TESTENVAAAAAAAAAAAAAAAA")

    await runner.stop("alpha")


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
    watch = runner._startup_watches["alpha"]

    runner._procs["alpha"].kill()  # die during startup (cross-platform hard kill)
    await watch
    assert inst.status is InstanceStatus.CRASHED


async def test_spawn_auto_enables_remote_control(runner_config, monkeypatch):
    """Before launching a bridge, the runner marks remote control acknowledged in
    ~/.claude.json (hasUsedRemoteControl/remoteDialogSeen) so the bridge skips the
    interactive enable prompt a detached-stdin bridge could never answer."""
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)

    assert "hasUsedRemoteControl" not in json.loads(claude_json.read_text())

    await runner.spawn("alpha")
    after = json.loads(claude_json.read_text())
    assert after["hasUsedRemoteControl"] is True
    assert after["remoteDialogSeen"] is True
    assert after["projects"]  # existing trust entries preserved

    await runner.stop("alpha")


async def test_spawn_auto_enable_can_be_disabled(runner_config, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    config.claude.auto_enable_remote_control = False
    runner = SessionRunner(config, claude_json=claude_json)

    await runner.spawn("alpha")
    assert "hasUsedRemoteControl" not in json.loads(claude_json.read_text())

    await runner.stop("alpha")


async def test_stop_unknown_instance_raises(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.stop("alpha")  # never spawned


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

    inst = await runner.adopt("alpha")
    assert inst.status is InstanceStatus.RUNNING
    assert inst.bridge_pid == 4242
    assert inst.resume_mode == "standard"  # standard external bridge
    assert inst.keeper_pid is None  # no keeper for a standard bridge
    assert inst.environment_id == "env_x"
    assert "env_x" in (inst.url or "")
    assert runner.get_instance("alpha") is inst
    # Persisted so a clauster restart keeps managing it.
    fresh = SessionRunner(config, claude_json=claude_json)
    assert "alpha" in fresh._persisted


async def test_adopt_refuses_pty_or_dead_external(runner_config, monkeypatch):
    # is_live_standard_bridge is False for a pty (flag-form) bridge OR a pointer that
    # went stale between the poll and the click -> fail closed, never partially adopt.
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: False)
    with pytest.raises(AdoptionUnavailable):
        await runner.adopt("alpha")
    assert runner.get_instance("alpha") is None


async def test_adopt_refuses_when_no_pointer(runner_config, monkeypatch):
    runner = _make_runner(runner_config)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: None)
    with pytest.raises(AdoptionUnavailable):
        await runner.adopt("alpha")
    assert runner.get_instance("alpha") is None


async def test_adopt_refuses_already_managed(runner_config):
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING
    )
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
    StateStore(config.state_dir).save(
        {"alpha": {"label": "my-alpha", "resume_mode": "pty", "spawn_mode": "same-dir"}}
    )
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr(
        "clauster.pointers.pointer_for_project",
        lambda path: _FakePtr() if path.name == "alpha" else None,
    )
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)

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
    assert runner.adoptable_external_projects() == {"alpha"}


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
    await runner.adopt("alpha")

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
    inst = await runner.stop("alpha")
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

    def list_then_adopt(*a, **k):
        # adopt() completes mid-suspension: a RUNNING instance for alpha appears AFTER
        # live_projects (empty — no managed bridge existed at snapshot) was computed.
        runner._instances["alpha"] = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.RUNNING,
            resume_mode="standard",
            bridge_pid=4242,
        )
        return [
            WorkingSession(pid=999, cwd=cwd, kind="interactive", started_at=999, local_uuid="u")
        ]

    monkeypatch.setattr(inspector, "list_working_sessions", list_then_adopt)
    await runner.poll_once()
    assert "alpha" in runner._instances  # adopted RUNNING instance survived the prune


async def test_poll_drops_phantom_stopped_shadowing_external(runner_config, monkeypatch):
    # A phantom STOPPED instance (e.g. `_stopped_from_persisted` from a stale pointer)
    # must not shadow a live EXTERNAL (flag-form/tmux) bridge at the same cwd: poll_once
    # drops it so the card shows "external session active" instead of Stopped/Resume.
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    sess = WorkingSession(
        pid=999,
        cwd=config.projects_root / "alpha",
        kind="interactive",
        started_at=999,
        local_uuid="u",
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
    await runner.poll_once()
    assert "alpha" not in runner._instances  # phantom dropped
    assert "alpha" in runner.external_sessions_by_project()  # now surfaced as external


async def test_poll_ignores_background_session_at_stopped_cwd(runner_config, monkeypatch):
    # A `claude --bg` background session (agent view, 2.1.139+) at a STOPPED
    # project's cwd is NOT an external bridge: the stopped record must survive
    # (Resume stays available) and nothing surfaces as "external session active".
    config = runner_config[0]
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
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
    assert "alpha" in runner._instances  # kept — a bg session is not a bridge
    assert runner.external_sessions_by_project() == {}


async def test_poll_keeps_stopped_instance_without_external_session(runner_config, monkeypatch):
    # The reboot-orphan path still works: a STOPPED-resumable instance with NO live
    # session at its cwd is preserved (so Resume stays available).
    runner = _make_runner(runner_config)
    runner._instances["alpha"] = RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.STOPPED, resume_mode="pty"
    )
    monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [])
    await runner.poll_once()
    assert "alpha" in runner._instances  # kept — nothing live to yield to


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
        runner._instances["alpha"] = RemoteControlInstance(
            project="alpha",
            label="alpha",
            status=InstanceStatus.ERROR,
            resume_mode="pty",
            bridge_pid=proc.pid,
            bridge_proc_start=procutil.proc_create_time(proc.pid),
        )
        sess = WorkingSession(
            pid=999,
            cwd=config.projects_root / "alpha",
            kind="interactive",
            started_at=999,
            local_uuid="u",
        )
        monkeypatch.setattr(inspector, "list_working_sessions", lambda *a, **k: [sess])
        await runner.poll_once()
        assert "alpha" in runner._instances  # live bridge: NOT phantom-deleted
        # the session at its cwd is managed (TRACKED), so it is not surfaced as external
        assert "alpha" not in runner.external_sessions_by_project()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


async def test_rediscover_overlays_persisted_state(runner_config, monkeypatch):
    config, claude_json = runner_config
    # alpha was intentionally stopped with a custom label; zeta is stale/persisted.
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "my-alpha",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
            "zeta": {
                "label": "zeta",
                "intentional_stop": True,
                "spawn_mode": "same-dir",
            },
        }
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


async def test_rediscover_tolerates_invalid_persisted_mode(runner_config, monkeypatch):
    config, claude_json = runner_config
    StateStore(config.state_dir).save(
        {
            "alpha": {
                "label": "alpha",
                "intentional_stop": True,
                "spawn_mode": "BOGUS",
                "permission_mode": "NOPE",
            },
        }
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
    inst = runner.get_instance("alpha")
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
    inst = runner.get_instance("alpha")
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
    await runner.stop("alpha")

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "resume")

    class FakePtr:
        pid, proc_start = 1, "1000"
        environment_id = "env_01TESTENVAAAAAAAAAAAAAAAA"
        session_id = "session_01RESUMEDBBBBBBBBBBB"

    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: FakePtr())

    resumed = await runner.resume("alpha")
    assert resumed.status is InstanceStatus.RUNNING
    # session id backfilled from the pointer (the resume log omitted it)…
    assert resumed.starter_session_id == "session_01RESUMEDBBBBBBBBBBB"
    assert resumed.session_url and "session_01RESUMEDBBBBBBBBBBB" in resumed.session_url
    # …and resume reused the stored permission mode rather than the config default.
    argv = _argv_of(resumed)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    await runner.stop("alpha")


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
    await runner.stop("alpha")

    # Simulate editing clauster.yml -> resume_mode: pty underneath the stopped bridge.
    runner._config.claude.resume_mode = "pty"

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "resume")

    class FakePtr:
        pid, proc_start = 1, "1000"
        environment_id = "env_01TESTENVAAAAAAAAAAAAAAAA"
        session_id = "session_01RESUMEDBBBBBBBBBBB"

    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: FakePtr())

    resumed = await runner.resume("alpha")
    # Honored the recorded mode: stayed standard, did not cross to the pty
    # flag form / keeper despite the config now saying pty.
    assert resumed.resume_mode == "standard"
    assert resumed.keeper_pid is None
    argv = _argv_of(resumed)
    assert "remote-control" in argv and "--remote-control" not in argv
    await runner.stop("alpha")


async def test_resume_unknown_instance_rejected(runner_config):
    runner = _make_runner(runner_config)
    with pytest.raises(UnknownProject):
        await runner.resume("alpha")  # never spawned -> nothing to resume


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
