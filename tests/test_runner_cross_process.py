"""Cross-process serialization of the bridge lifecycle (#949).

Two clauster processes (the live web app vs a headless CLI/MCP writer) share the
instance store and the per-project bridge pointers, but not an in-process lock or
registry. These tests simulate the second process with a SECOND ``SessionRunner``
(its own ``Persistence`` engine on the same SQLite file, its own empty registry)
or with direct store writes, and pin the two #949 guarantees:

* race 1 (duplicate-bridge TOCTOU): a spawn's idempotency check extends across
  processes via the on-disk bridge pointer, probed under a per-project flock —
  a live standard bridge another process launched is reattached, never doubled;
* race 2 (stale-snapshot resurrection): every full-replace persist merges onto
  the store's CURRENT state, so a row another process pruned stays pruned and a
  row another process added survives this process's saves.
"""

from __future__ import annotations

import contextlib
import os
import sys

import pytest

from clauster import atomicio
from clauster.db.persistence import Persistence
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner, UnknownProject

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="fcntl flock is POSIX-only; Windows keeps in-process locking"
)


@contextlib.contextmanager
def _other_process_store(config):
    """A state store on its OWN engine over the same DB file — the 'other process'."""
    persistence = Persistence(
        config.state_dir, backup_before_migrate=config.db.backup_before_migrate
    )
    try:
        yield persistence.state_store()
    finally:
        persistence.dispose()


class _FakePtr:
    """Stand-in for a live Anthropic bridge-pointer.json (sessionId/env/pid/procStart)."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.proc_start = "1000"
        self.environment_id = "env_other"
        self.session_id = "session_other"


def _fake_instance(project: str) -> RemoteControlInstance:
    return RemoteControlInstance(project=project, label=project, status=InstanceStatus.RUNNING)


# -- race 1: cross-process spawn idempotency ----------------------------------


async def test_spawn_reattaches_live_bridge_of_another_process(runner_config, monkeypatch):
    # The registry can't see a standard bridge another process launched, but its
    # pointer on disk can: spawn must reattach it idempotently, never fork a second
    # bridge onto the same environment.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (config.projects_root / "alpha").resolve(),
    )

    def _no_launch(*_a, **_k):
        raise AssertionError("spawn must not launch a second bridge over a live pointer")

    monkeypatch.setattr(runner, "_popen", _no_launch)

    outcome = await runner.spawn_detailed("alpha")
    assert outcome.created is False
    assert "reattached" in (outcome.reason or "")
    assert outcome.instance.status is InstanceStatus.RUNNING
    assert outcome.instance.bridge_pid == 4242
    assert outcome.instance.environment_id == "env_other"
    assert runner.get_instance_for_project("alpha") is outcome.instance
    # Persisted, so a restart of THIS process keeps managing the reattached bridge.
    with _other_process_store(config) as store:
        assert any(v.get("project_name") == "alpha" for v in store.load().values())


async def test_reattach_probe_ignores_dead_or_pty_pointer(runner_config, monkeypatch):
    # The probe uses adopt()'s gate: a stale pointer or a pty/flag-form bridge fails
    # is_live_standard_bridge and the spawn proceeds to a normal launch.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    proj = runner._resolve_project("alpha")
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: False)
    assert await runner._reattach_external_standard(proj) is None
    assert runner.get_instance_for_project("alpha") is None


async def test_reattach_probe_refuses_sanitize_collided_foreign_bridge(runner_config, monkeypatch):
    # The pointer dir is keyed by the SANITIZED cwd, so a punctuation-differing OTHER
    # project's live bridge can sit behind alpha's pointer path. Its actual cwd is not
    # alpha's directory -> refuse the take-over (a reattach would hand alpha's Stop
    # button a foreign pid).
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    proj = runner._resolve_project("alpha")
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr(
        "clauster.runner.procutil.proc_cwd",
        lambda pid: (config.projects_root / "beta").resolve(),  # someone else's project
    )
    assert await runner._reattach_external_standard(proj) is None
    assert runner.get_instance_for_project("alpha") is None


async def test_reattach_probe_refuses_unattributable_cwd(runner_config, monkeypatch):
    # An unreadable cwd (gone/zombie/access denied) means the bridge can't be
    # positively attributed to this project: fail closed, never take over on a guess.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    proj = runner._resolve_project("alpha")
    monkeypatch.setattr("clauster.pointers.pointer_for_project", lambda path: _FakePtr())
    monkeypatch.setattr("clauster.runner.procutil.is_live_standard_bridge", lambda *a, **k: True)
    monkeypatch.setattr("clauster.runner.procutil.proc_cwd", lambda pid: None)
    assert await runner._reattach_external_standard(proj) is None
    assert runner.get_instance_for_project("alpha") is None


# -- race 2: stale-snapshot resurrection / foreign-row prune ------------------


async def test_persist_does_not_resurrect_row_another_process_forgot(runner_config):
    config, claude_json = runner_config
    with _other_process_store(config) as store:
        store.save({"iid-x": {"project_name": "beta", "label": "doomed"}})
    runner = SessionRunner(config, claude_json=claude_json)  # snapshot holds iid-x
    with _other_process_store(config) as store:
        store.save({})  # the other process forgets iid-x

    own = _fake_instance("alpha")
    runner._instances[own.instance_id] = own
    await runner._persist()

    with _other_process_store(config) as store:
        records = store.load()
    assert "iid-x" not in records  # stale base would have resurrected it
    assert own.instance_id in records


async def test_persist_does_not_prune_row_another_process_added(runner_config):
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)  # snapshot is empty
    with _other_process_store(config) as store:
        store.save({"iid-z": {"project_name": "beta", "label": "keep-me"}})

    own = _fake_instance("alpha")
    runner._instances[own.instance_id] = own
    await runner._persist()

    with _other_process_store(config) as store:
        records = store.load()
    assert records.get("iid-z", {}).get("label") == "keep-me"  # stale base would prune it
    assert own.instance_id in records


async def test_forget_of_row_another_process_already_forgot_raises(runner_config):
    # forget refreshes its merge base at entry, so a record that only survives in this
    # process's construction-time snapshot is honestly reported as unknown.
    config, claude_json = runner_config
    with _other_process_store(config) as store:
        store.save({"iid-x": {"project_name": "beta"}})
    runner = SessionRunner(config, claude_json=claude_json)
    with _other_process_store(config) as store:
        store.save({})
    with pytest.raises(UnknownProject):
        await runner.forget("iid-x")


async def test_rediscover_resurrects_row_persisted_after_construction(runner_config):
    # hydrate (#775) = rediscover(persist=False): its refresh must surface a STOPPED
    # card for a record the live service saved after this headless runner was built.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    with _other_process_store(config) as store:
        store.save({"iid-b": {"project_name": "beta", "label": "from-other-proc"}})

    await runner.rediscover(persist=False)
    inst = runner.get_instance_for_project("beta")
    assert inst is not None
    assert inst.status is InstanceStatus.STOPPED
    assert inst.label == "from-other-proc"


async def test_persist_aborts_on_failed_refresh_instead_of_pruning(runner_config, monkeypatch):
    # If the base can't be refreshed, writing the full-replace snapshot anyway could
    # prune a row another process added since our snapshot (Greptile #951 P1): the
    # persist attempt must abort — the store keeps the other process's row, and our
    # own change simply waits for the next (retried) persist.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)  # snapshot is empty
    with _other_process_store(config) as store:
        store.save({"iid-z": {"project_name": "beta", "label": "added-later"}})

    def _boom():
        raise OSError("state load failed: locked")

    monkeypatch.setattr(runner._state, "load_strict", _boom)
    own = _fake_instance("alpha")
    runner._instances[own.instance_id] = own
    await runner._persist()

    with _other_process_store(config) as store:
        records = store.load()
    assert records.get("iid-z", {}).get("label") == "added-later"  # never pruned
    assert own.instance_id not in records  # save was aborted, not partially applied


async def test_refresh_keeps_previous_base_on_db_read_error(runner_config, monkeypatch, caplog):
    # A transient DB read failure must keep the known-good base (a stale cursor),
    # never swap in {} — the next full-replace save would mass-prune the store.
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    runner._persisted = {"iid-keep": {"project_name": "beta"}}

    def _boom():
        raise OSError("state load failed: locked")

    monkeypatch.setattr(runner._state, "load_strict", _boom)
    with caplog.at_level("WARNING", logger="clauster.runner"):
        await runner._refresh_persisted()
    assert runner._persisted == {"iid-keep": {"project_name": "beta"}}
    assert "could not refresh persisted bridge state" in caplog.text


@_POSIX_ONLY
async def test_persist_holds_the_store_wide_flock_across_the_save(runner_config, monkeypatch):
    # StateStore.save is a full-table replace, so the refresh->save read-merge-write
    # must be exclusive STORE-WIDE (per-project flocks don't exclude other projects'
    # writers): probe the store lock from inside the save and expect it held.
    import fcntl

    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    real_save = runner._state.save
    observed = {}

    def _save_and_probe(records):
        lock_file = atomicio._cross_process_lock_file(
            (config.state_dir / "state-store").expanduser(),
            (config.state_dir / "locks").expanduser(),
        )
        fd = os.open(lock_file, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["held"] = False
            except BlockingIOError:
                observed["held"] = True
        finally:
            os.close(fd)
        real_save(records)

    monkeypatch.setattr(runner._state, "save", _save_and_probe)
    own = _fake_instance("alpha")
    runner._instances[own.instance_id] = own
    await runner._persist()
    assert observed["held"] is True


@_POSIX_ONLY
async def test_runner_flock_dir_survives_later_global_reconfigure(runner_config, tmp_path):
    # The runner pins its lock dir at construction: a LATER configure_lock_dir for a
    # different deployment in the same process (a second runner's init) must not
    # redirect this runner's lock files away from the ones external processes use.
    config, claude_json = runner_config
    a = SessionRunner(config, claude_json=claude_json)
    atomicio.configure_lock_dir(tmp_path / "other-deployment-locks")
    target = (config.projects_root / "alpha").expanduser()
    async with a._bridge_flock("alpha"):
        pinned = atomicio._cross_process_lock_file(
            target, (config.state_dir / "locks").expanduser()
        )
        assert pinned is not None
        assert pinned.exists()  # the flock landed in THIS runner's deployment dir
        drifted = atomicio._cross_process_lock_file(target)  # global = other deployment
        assert drifted is not None
        assert not drifted.exists()


# -- the flock itself ----------------------------------------------------------


@_POSIX_ONLY
async def test_bridge_flock_excludes_a_second_process(runner_config):
    # While one runner holds the per-project section, the flock file derived from the
    # same config is exclusively held — a second acquirer (any other process) blocks.
    import fcntl

    config, claude_json = runner_config
    a = SessionRunner(config, claude_json=claude_json)
    b = SessionRunner(config, claude_json=claude_json)
    target = (config.projects_root / "alpha").expanduser()
    async with a._bridge_flock("alpha"):
        lock_file = atomicio._cross_process_lock_file(target)
        assert lock_file is not None
        assert lock_file.exists()
        assert lock_file.is_relative_to(config.state_dir.expanduser())
        fd = os.open(lock_file, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    # Released on exit: a second runner over the same config can take it now.
    async with b._bridge_flock("alpha"):
        assert lock_file.exists()  # created O_CREAT, deliberately never unlinked


@_POSIX_ONLY
async def test_cancelled_contended_acquire_never_pins_the_lock(runner_config):
    # Cancel a caller stuck behind a held flock: its worker thread still completes the
    # acquisition later, and the done-callback must release it — the lock becomes
    # takeable again, and the cancelled section body never ran.
    import asyncio
    import fcntl

    from conftest import wait_until

    config, claude_json = runner_config
    a = SessionRunner(config, claude_json=claude_json)
    b = SessionRunner(config, claude_json=claude_json)
    entered_b = asyncio.Event()

    async def _b_section():
        async with b._bridge_flock("alpha"):
            entered_b.set()

    async with a._bridge_flock("alpha"):
        task = asyncio.create_task(_b_section())
        await asyncio.sleep(0.05)  # let B's worker thread block on the held flock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not entered_b.is_set()
    lock_file = atomicio._cross_process_lock_file((config.projects_root / "alpha").expanduser())

    def _acquirable():
        fd = os.open(lock_file, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    # B's landed-after-cancel acquisition is released by the callback (not GC).
    await wait_until(_acquirable)


async def test_cancelled_flock_acquire_is_released_when_the_thread_lands():
    # A caller cancelled mid-acquire must not pin the cross-process lock until GC:
    # the done-callback exits the manager as soon as the worker thread's acquisition
    # lands — and only for a SUCCESSFUL acquisition.
    import asyncio

    from clauster.runner import _release_flock_if_acquired

    class _Cm:
        exited = 0

        def __exit__(self, *exc):
            self.exited += 1

    cm = _Cm()
    ok = asyncio.get_event_loop().create_future()
    ok.set_result(None)
    _release_flock_if_acquired(cm)(ok)
    assert cm.exited == 1  # acquired after cancel -> released

    failed = asyncio.get_event_loop().create_future()
    failed.set_exception(FileNotFoundError("state dir gone"))
    _release_flock_if_acquired(cm)(failed)
    assert cm.exited == 1  # never entered -> never exited

    cancelled = asyncio.get_event_loop().create_future()
    cancelled.cancel()
    _release_flock_if_acquired(cm)(cancelled)
    assert cm.exited == 1  # cancelled before entering -> never exited


def test_proc_cwd_reads_own_process_and_fails_closed():
    import os as _os

    from clauster import procutil

    own = procutil.proc_cwd(_os.getpid())
    assert own is not None
    assert own.resolve() == type(own)(_os.getcwd()).resolve()
    # A PID that can't exist reads as "not attributable", never a raise.
    assert procutil.proc_cwd(2**22 + 12345) is None
