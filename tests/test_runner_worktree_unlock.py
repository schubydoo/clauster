"""#1089: clauster releases the git lock on a stopped pty session's worktree.

Claude Code creates a `spawn_mode="worktree"` interactive session's worktree via
`--worktree <name>` and locks it; it never releases the lock on the SIGINT stop, so
`git worktree remove` refuses it. `stop()` now unlocks it (the lock guards a live
session only), leaving the worktree + branch for a possible resume.
"""

from __future__ import annotations

import subprocess
import types

from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import SessionRunner


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


def _worktree_locked(repo, name: str) -> bool:
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True
    ).stdout
    return any(name in line and "locked" in line for line in out.splitlines())


def _runner(runner_config) -> SessionRunner:
    return SessionRunner(runner_config[0], claude_json=runner_config[1])


def test_unlock_pty_worktree_releases_lock(runner_config, tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "i")
    inst = RemoteControlInstance(
        project="proj", label="proj", spawn_mode="worktree", resume_mode="pty"
    )
    name = f"clauster-{inst.instance_id[:8]}"
    wt = repo / ".claude" / "worktrees" / name
    _git(repo, "worktree", "add", "--lock", "-b", f"wt-{name}", str(wt))
    assert _worktree_locked(repo, name)  # precondition: Claude Code would leave it locked

    runner = _runner(runner_config)
    monkeypatch.setattr(runner, "_resolve_project", lambda n: types.SimpleNamespace(path=repo))
    runner._unlock_pty_worktree(inst)
    assert not _worktree_locked(repo, name)  # lock released, so `git worktree remove` can run


def test_unlock_pty_worktree_noop_for_non_worktree(runner_config, monkeypatch):
    # A same-dir/session pty (or a standard bridge) has no worktree — the helper must not
    # even resolve a project or shell git.
    runner = _runner(runner_config)

    def _boom(_n):
        raise AssertionError("must not resolve a project for a non-worktree session")

    monkeypatch.setattr(runner, "_resolve_project", _boom)
    inst = RemoteControlInstance(project="proj", label="proj", spawn_mode="same-dir")
    runner._unlock_pty_worktree(inst)  # no raise, no git


def test_unlock_pty_worktree_best_effort_on_missing_worktree(runner_config, tmp_path, monkeypatch):
    # An already-removed / never-created worktree → git exits non-zero, swallowed (no raise).
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    runner = _runner(runner_config)
    monkeypatch.setattr(runner, "_resolve_project", lambda n: types.SimpleNamespace(path=repo))
    inst = RemoteControlInstance(
        project="proj", label="proj", spawn_mode="worktree", resume_mode="pty"
    )
    runner._unlock_pty_worktree(inst)


async def test_stop_unlocks_the_worktree(runner_config, monkeypatch):
    # Wiring: a clean stop() invokes _unlock_pty_worktree. Uses the pid-is-None branch
    # (bridge already gone) so no real bridge/keeper is needed.
    runner = _runner(runner_config)
    inst = RemoteControlInstance(
        project="alpha",
        label="alpha",
        spawn_mode="worktree",
        resume_mode="pty",
        status=InstanceStatus.RUNNING,
    )
    runner._instances[inst.instance_id] = inst
    calls: list[str] = []
    monkeypatch.setattr(runner, "_unlock_pty_worktree", lambda i: calls.append(i.instance_id))
    await runner.stop(inst.instance_id)
    assert calls == [inst.instance_id]
    assert inst.status is InstanceStatus.STOPPED
