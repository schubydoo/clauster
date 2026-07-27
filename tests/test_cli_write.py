"""Tests for the headless write CLI commands (#775, Slice B).

Each command is driven through ``clauster.__main__.main`` (so the argparse wiring
and dispatch arms are covered) with a fake engine swapped into ``cli_write`` — the
facade itself is tested in ``test_engine.py``, so here we assert the command layer:
argument plumbing (mode/spawn-mode/permission-mode/name/sandbox/trust), the status
line vs ``--json`` output, the created-vs-already-running branch, warnings, and the
error → exit-code mapping (unknown/untrusted/bad-option → 2, capacity/failure → 1).
"""

from __future__ import annotations

import json

import pytest

from clauster import cli_write
from clauster.__main__ import main
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.runner import (
    CapacityExceeded,
    InvalidSpawnOption,
    NotTrusted,
    PermissionModeNotAllowed,
    SpawnError,
    SpawnOutcome,
    UnknownProject,
)


class _FakeEngine:
    """Stand-in for ClausterEngine — canned outcome/behaviour set per test."""

    def bridge_id_candidates(self, identity: str) -> list[str]:
        """Ambiguous-prefix candidates (#1099); empty unless a test sets ``candidates``."""
        return list(self.candidates)

    candidates: list[str] = []

    outcome: SpawnOutcome | None = None
    stop_result: RemoteControlInstance | None = None
    raise_on_start: Exception | None = None
    raise_on_stop: Exception | None = None

    def __init__(self, config, *, runner=None) -> None:
        type(self).hydrated = 0
        type(self).start_kw = {}

    def __enter__(self) -> _FakeEngine:
        return self

    def __exit__(self, *exc) -> None:
        return None

    async def hydrate(self) -> None:
        type(self).hydrated += 1

    async def start(self, project, **kw):
        type(self).start_project = project
        type(self).start_kw = kw
        if type(self).raise_on_start is not None:
            raise type(self).raise_on_start
        return type(self).outcome

    async def stop(self, identity):
        type(self).stop_identity = identity
        if type(self).raise_on_stop is not None:
            raise type(self).raise_on_stop
        return type(self).stop_result


@pytest.fixture
def cfg(write_config) -> str:
    return str(write_config(""))


@pytest.fixture(autouse=True)
def _fake_engine(monkeypatch):
    _FakeEngine.outcome = None
    _FakeEngine.stop_result = None
    _FakeEngine.raise_on_start = None
    _FakeEngine.raise_on_stop = None
    monkeypatch.setattr(cli_write, "ClausterEngine", _FakeEngine)


def _inst(iid: str = "abc1234567", **kw) -> RemoteControlInstance:
    return RemoteControlInstance(
        instance_id=iid, project=kw.pop("project", "alpha"), label="lbl", **kw
    )


# -- start ---------------------------------------------------------------------


def test_start_forwards_all_options_and_hydrates_first(cfg, capsys):
    inst = _inst(status=InstanceStatus.RUNNING, resume_mode="pty")
    _FakeEngine.outcome = SpawnOutcome(instance=inst, created=True)
    rc = main(
        [
            "start",
            "-c",
            cfg,
            "alpha",
            "--mode",
            "pty",
            "--spawn-mode",
            "worktree",
            "--permission-mode",
            "plan",
            "--name",
            "mybridge",
            "--sandbox",
            "on",
            "--trust",
        ]
    )
    assert rc == 0
    assert _FakeEngine.hydrated == 1  # read-only reattach ran before the spawn
    assert _FakeEngine.start_project == "alpha"
    assert _FakeEngine.start_kw == {
        "spawn_mode": "worktree",
        "permission_mode": "plan",
        "resume_mode": "pty",
        "custom_name": "mybridge",
        "sandbox": "on",
        "trust": True,
    }
    out = capsys.readouterr().out
    assert "started" in out and "abc1234" in out and "mode=pty" in out and "running" in out


def test_start_defaults_pass_none_through(cfg):
    _FakeEngine.outcome = SpawnOutcome(instance=_inst(), created=True)
    assert main(["start", "-c", cfg, "alpha"]) == 0
    assert _FakeEngine.start_kw == {
        "spawn_mode": None,
        "permission_mode": None,
        "resume_mode": None,
        "custom_name": None,
        "sandbox": None,
        "trust": False,
    }


def test_start_json(cfg, capsys):
    inst = _inst(status=InstanceStatus.RUNNING)
    _FakeEngine.outcome = SpawnOutcome(instance=inst, created=True, warnings=["w1"])
    assert main(["start", "-c", cfg, "alpha", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["instance_id"] == "abc1234567"
    assert data["created"] is True and data["warnings"] == ["w1"]


def test_start_already_running_reports_reason(cfg, capsys):
    inst = _inst(status=InstanceStatus.RUNNING)
    _FakeEngine.outcome = SpawnOutcome(
        instance=inst, created=False, reason="a standard bridge is already live"
    )
    assert main(["start", "-c", cfg, "alpha"]) == 0
    out = capsys.readouterr().out
    assert "already running" in out and "a standard bridge is already live" in out


def test_start_surfaces_warnings_on_stderr(cfg, capsys):
    inst = _inst(status=InstanceStatus.RUNNING)
    _FakeEngine.outcome = SpawnOutcome(
        instance=inst, created=True, warnings=["no worktree — risky"]
    )
    assert main(["start", "-c", cfg, "alpha"]) == 0
    assert "no worktree — risky" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("exc", "code", "needle"),
    [
        (NotTrusted("directory not trusted: /p/alpha."), 2, "--trust"),
        (UnknownProject("no such project: alpha"), 2, "no such project"),
        (InvalidSpawnOption("invalid spawn_mode 'x'"), 2, "invalid spawn_mode"),
        (PermissionModeNotAllowed("bypass not allowed"), 2, "bypass not allowed"),
        (CapacityExceeded("too many bridges"), 1, "could not start"),
        (SpawnError("bridge died on launch"), 1, "could not start"),
    ],
)
def test_start_error_exit_codes(cfg, capsys, exc, code, needle):
    _FakeEngine.raise_on_start = exc
    assert main(["start", "-c", cfg, "alpha"]) == code
    assert needle in capsys.readouterr().err


# -- stop ----------------------------------------------------------------------


def test_stop_reports_stopped_instance(cfg, capsys):
    _FakeEngine.stop_result = _inst(status=InstanceStatus.STOPPED)
    assert main(["stop", "-c", cfg, "abc1234567"]) == 0
    assert _FakeEngine.hydrated == 1 and _FakeEngine.stop_identity == "abc1234567"
    assert "stopped abc1234" in capsys.readouterr().out


def test_stop_json(cfg, capsys):
    _FakeEngine.stop_result = _inst(status=InstanceStatus.STOPPED)
    assert main(["stop", "-c", cfg, "abc1234567", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["instance_id"] == "abc1234567"


def test_stop_names_the_candidates_for_an_ambiguous_prefix(cfg, capsys):
    # Without this the operator reads "no managed instance" for an id that names several
    # REAL bridges, with no hint to type more characters (#1099).
    _FakeEngine.stop_result = None
    _FakeEngine.candidates = ["f2c456fd-aaaa", "f2c456fd-bbbb"]
    try:
        assert main(["stop", "-c", cfg, "f2c456fd"]) == 2
        err = capsys.readouterr().err
        assert "ambiguous" in err
        assert "f2c456fd-aaaa" in err and "f2c456fd-bbbb" in err
        assert "no managed instance" not in err, "the misleading not-found wording remained"
    finally:
        _FakeEngine.candidates = []


def test_stop_unknown_identity_exit_2(cfg, capsys):
    _FakeEngine.stop_result = None
    assert main(["stop", "-c", cfg, "nope"]) == 2
    assert "no managed instance" in capsys.readouterr().err


def test_stop_vanished_project_exit_2(cfg, capsys):
    _FakeEngine.raise_on_stop = UnknownProject("project alpha gone")
    assert main(["stop", "-c", cfg, "abc1234567"]) == 2
    assert "project alpha gone" in capsys.readouterr().err


def test_stop_signalling_oserror_exit_1_not_traceback(cfg, capsys):
    # runner.stop() can raise OSError (signalling a dead/reused pid) — the CLI must
    # surface it and exit 1, not leak a traceback past the documented failure path.
    _FakeEngine.raise_on_stop = OSError("no such process")
    assert main(["stop", "-c", cfg, "abc1234567"]) == 1
    assert "could not stop" in capsys.readouterr().err
