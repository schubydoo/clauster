"""Tests for the headless read CLI commands (#775, Slice A).

Each command is driven through ``clauster.__main__.main`` (so the argparse wiring
and dispatch arms are covered) with a fake engine swapped into ``cli_read`` — the
facade itself is tested in ``test_engine.py``, so here we assert the command layer:
argument plumbing, table vs ``--json`` output, empty states, log tail/follow, the
connect-URL path, and the unknown-target exit code.
"""

from __future__ import annotations

import json

import pytest

from clauster import cli_read
from clauster.__main__ import main
from clauster.models import (
    InstanceStatus,
    Project,
    RemoteControlInstance,
    TrustState,
    WorkingSession,
)


class _FakeEngine:
    """Stand-in for ClausterEngine — canned data set per test via class attributes."""

    projects: list[Project] = []
    instances: list[RemoteControlInstance] = []
    sessions: list[WorkingSession] = []
    log_path = None
    log_reads: list[tuple[int, list[str]]] = []
    url: str | None = None

    def __init__(self, config, *, runner=None) -> None:
        self._read = 0

    def __enter__(self) -> _FakeEngine:
        return self

    def __exit__(self, *exc) -> None:
        return None

    async def hydrate(self) -> None:
        return None

    def list_projects(self):
        return type(self).projects

    def list_instances(self):
        return type(self).instances

    def working_sessions(self):
        return type(self).sessions

    def bridge_log_path(self, identity):
        return type(self).log_path

    def connect_url(self, identity):
        return type(self).url

    def initial_log_offset(self, path):
        return 0

    def read_log_lines(self, path, offset):
        reads = type(self).log_reads
        result = reads[self._read] if self._read < len(reads) else (offset, [])
        self._read += 1
        return result


@pytest.fixture
def cfg(write_config) -> str:
    return str(write_config(""))


@pytest.fixture(autouse=True)
def _fake_engine(monkeypatch):
    # Reset canned data + swap the facade for the fake in the command module.
    _FakeEngine.projects = []
    _FakeEngine.instances = []
    _FakeEngine.sessions = []
    _FakeEngine.log_path = None
    _FakeEngine.log_reads = []
    _FakeEngine.url = None
    monkeypatch.setattr(cli_read, "ClausterEngine", _FakeEngine)


def _proj(name: str, **kw) -> Project:
    return Project(name=name, path=f"/p/{name}", **kw)


def _inst(iid: str, **kw) -> RemoteControlInstance:
    return RemoteControlInstance(
        instance_id=iid, project=kw.pop("project", "alpha"), label="lbl", **kw
    )


def _sess(uuid: str, pid: int) -> WorkingSession:
    return WorkingSession(
        pid=pid, cwd="/p/alpha", kind="interactive", started_at=0, local_uuid=uuid
    )


# -- projects ------------------------------------------------------------------


def test_projects_table(cfg, capsys):
    _FakeEngine.projects = [
        _proj(
            "alpha",
            is_git_repo=True,
            trust_state=TrustState.TRUSTED,
            allow_bypass_permissions=True,
        ),
        _proj("beta"),
    ]
    assert main(["projects", "-c", cfg]) == 0
    out = capsys.readouterr().out
    assert "PROJECT" in out and "alpha" in out and "beta" in out
    assert "trusted" in out and "yes" in out  # alpha's trust + bypass stamped


def test_projects_json(cfg, capsys):
    _FakeEngine.projects = [_proj("alpha", is_git_repo=True)]
    assert main(["projects", "-c", cfg, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["name"] == "alpha" and data[0]["is_git_repo"] is True


def test_projects_empty(cfg, capsys):
    assert main(["projects", "-c", cfg]) == 0
    assert "No projects found." in capsys.readouterr().err


# -- status --------------------------------------------------------------------


def test_status_table(cfg, capsys):
    _FakeEngine.instances = [_inst("abc1234567", status=InstanceStatus.RUNNING, resume_mode="pty")]
    assert main(["status", "-c", cfg]) == 0
    out = capsys.readouterr().out
    assert "INSTANCE" in out and "abc1234" in out and "running" in out and "pty" in out


def test_status_json(cfg, capsys):
    _FakeEngine.instances = [_inst("abc1234567")]
    assert main(["status", "-c", cfg, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["instance_id"] == "abc1234567"


def test_status_empty(cfg, capsys):
    assert main(["status", "-c", cfg]) == 0
    assert "No bridge instances." in capsys.readouterr().err


# -- sessions ------------------------------------------------------------------


def test_sessions_table(cfg, capsys):
    _FakeEngine.sessions = [_sess("uuid-one", 111), _sess("uuid-two", 222)]
    assert main(["sessions", "-c", cfg]) == 0
    out = capsys.readouterr().out
    assert "SESSION" in out and "KIND" in out
    assert "uuid-one" in out and "uuid-two" in out and "111" in out


def test_sessions_json(cfg, capsys):
    _FakeEngine.sessions = [_sess("u1", 1)]
    assert main(["sessions", "-c", cfg, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["local_uuid"] == "u1"


def test_sessions_empty(cfg, capsys):
    assert main(["sessions", "-c", cfg]) == 0
    assert "No working sessions." in capsys.readouterr().err


# -- logs ----------------------------------------------------------------------


def test_logs_unknown_instance_exits_2(cfg, capsys):
    assert main(["logs", "-c", cfg, "ghost"]) == 2
    assert "no bridge log" in capsys.readouterr().err


def test_logs_stale_missing_file_exits_2(cfg, tmp_path, capsys):
    # A resolved but nonexistent log path (rotated/deleted) must fail closed, not
    # exit 0 with no output / --follow hang.
    _FakeEngine.log_path = tmp_path / "gone.log"  # never created
    assert main(["logs", "-c", cfg, "i1"]) == 2
    assert "no longer on disk" in capsys.readouterr().err


def test_logs_one_shot_prints_tail(cfg, tmp_path, capsys):
    log = tmp_path / "b.log"
    log.touch()
    _FakeEngine.log_path = log
    _FakeEngine.log_reads = [(10, ["first line", "second line"])]
    assert main(["logs", "-c", cfg, "i1"]) == 0
    assert capsys.readouterr().out.splitlines() == ["first line", "second line"]


def test_logs_follow_streams_until_interrupt(cfg, tmp_path, capsys, monkeypatch):
    log = tmp_path / "b.log"
    log.touch()
    _FakeEngine.log_path = log
    _FakeEngine.log_reads = [(10, ["initial"]), (20, ["streamed"])]
    calls = {"n": 0}

    def fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_read.time, "sleep", fake_sleep)
    assert main(["logs", "-c", cfg, "i1", "--follow"]) == 0
    assert capsys.readouterr().out.splitlines() == ["initial", "streamed"]


def test_logs_follow_stops_if_file_vanishes(cfg, tmp_path, capsys, monkeypatch):
    # If the log is deleted after --follow starts, fail closed (exit 1 + diagnostic)
    # instead of sleeping forever showing nothing.
    log = tmp_path / "b.log"
    log.touch()
    _FakeEngine.log_path = log
    _FakeEngine.log_reads = [(10, ["initial"])]

    monkeypatch.setattr(cli_read.time, "sleep", lambda _s: log.unlink())  # vanish mid-follow
    assert main(["logs", "-c", cfg, "i1", "--follow"]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines() == ["initial"]  # the one-shot tail printed before the vanish
    assert "vanished" in out.err


# -- open ----------------------------------------------------------------------


def test_open_unknown_instance_exits_2(cfg, capsys):
    assert main(["open", "-c", cfg, "ghost"]) == 2
    assert "no connect URL" in capsys.readouterr().err


def test_open_prints_url(cfg, capsys):
    _FakeEngine.url = "https://claude.ai/code/s1?from=cli"
    assert main(["open", "-c", cfg, "i1"]) == 0
    assert capsys.readouterr().out.strip() == "https://claude.ai/code/s1?from=cli"


def test_open_launch_opens_browser(cfg, capsys, monkeypatch):
    _FakeEngine.url = "https://claude.ai/code/s1?from=cli"
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda url: opened.setdefault("url", url))
    assert main(["open", "-c", cfg, "i1", "--launch"]) == 0
    assert opened["url"] == "https://claude.ai/code/s1?from=cli"
