"""Tests for the shared read-facade :class:`clauster.engine.ClausterEngine` (#775).

The facade is exercised with a lightweight fake runner for the delegations and its
own logic (bypass stamp, connect-URL fallback, log path + redaction, dispose
ownership, hydrate), plus the real ``discover_projects_cached`` path for
``list_projects``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from clauster.config import ClausterConfig
from clauster.engine import ClausterEngine
from clauster.models import RemoteControlInstance


class _FakePersistence:
    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


class _FakeRunner:
    """Minimal SessionRunner stand-in — no DB, just the methods the facade calls."""

    def __init__(
        self,
        *,
        claude_json: Path,
        instances: list[RemoteControlInstance] | None = None,
    ) -> None:
        self.claude_json = claude_json
        self._instances = {i.instance_id: i for i in (instances or [])}
        self.rediscovered = 0
        self.persist_arg: bool | None = None
        self.persistence = _FakePersistence()
        self.spawn_calls: list[tuple[str, dict]] = []
        self.stopped: list[str] = []
        self.resumed: list[str] = []
        self.trusted: list[str] = []

    def list_instances(self) -> list[RemoteControlInstance]:
        return list(self._instances.values())

    def resolve_bridge_id(self, identity: str) -> str | None:
        return identity if identity in self._instances else None

    def get_instance(self, instance_id: str) -> RemoteControlInstance | None:
        return self._instances.get(instance_id)

    async def rediscover(self, *, persist: bool = True) -> None:
        self.rediscovered += 1
        self.persist_arg = persist

    async def spawn_detailed(self, name: str, **kw: object):
        from clauster.runner import SpawnOutcome

        self.spawn_calls.append((name, kw))
        inst = next(iter(self._instances.values()))
        return SpawnOutcome(instance=inst, created=True)

    async def stop(self, instance_id: str) -> RemoteControlInstance:
        self.stopped.append(instance_id)
        return self._instances[instance_id]

    async def resume(self, instance_id: str) -> RemoteControlInstance:
        self.resumed.append(instance_id)
        return self._instances[instance_id]

    async def trust_project(self, name: str) -> None:
        self.trusted.append(name)


def _engine(tmp_path: Path, projects_root: Path, runner: _FakeRunner) -> ClausterEngine:
    cfg = ClausterConfig.model_validate(
        {"projects_root": str(projects_root), "state_dir": str(tmp_path / "state")}
    )
    return ClausterEngine(cfg, runner=runner)  # type: ignore[arg-type]


# -- list_projects: discovery + the bypass stamp -------------------------------


def test_list_projects_returns_discovered_and_stamps_bypass(tmp_path, projects_root, monkeypatch):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)
    # Only "alpha" is bypass-eligible → the stamp must reflect that per project, not a
    # blanket default (proves the facade owns the app-layer stamp discovery lacks).
    # Patch the class method (pydantic blocks setting a non-field attr on the instance).
    monkeypatch.setattr(ClausterConfig, "allows_bypass", lambda self, name: name == "alpha")

    projects = engine.list_projects()
    by_name = {p.name: p for p in projects}

    assert {"alpha", "beta", "gamma"} <= set(by_name)  # .hidden / "bad name!" excluded
    assert by_name["alpha"].allow_bypass_permissions is True
    assert by_name["beta"].allow_bypass_permissions is False


# -- delegations ---------------------------------------------------------------


def test_read_delegations_pass_through_to_runner(tmp_path, projects_root):
    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    engine = _engine(tmp_path, projects_root, runner)

    assert engine.list_instances() == [inst]
    assert engine.resolve_instance("i1") is inst
    assert engine.resolve_instance("nope") is None


def test_working_sessions_probes_inspector_read_only(tmp_path, projects_root, monkeypatch):
    from clauster import engine as engine_mod
    from clauster.models import WorkingSession

    sess = WorkingSession(pid=1, cwd="/p/alpha", kind="interactive", started_at=0, local_uuid="u1")
    seen = {}

    def fake_list(binary, **kw):
        seen["binary"] = binary
        return [sess]

    monkeypatch.setattr(engine_mod.inspector, "list_working_sessions", fake_list)
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)

    assert engine.working_sessions() == [sess]
    assert seen["binary"]  # the configured claude binary was passed through


# -- write: start / stop -------------------------------------------------------


def test_start_delegates_options_to_spawn_detailed(tmp_path, projects_root):
    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    engine = _engine(tmp_path, projects_root, runner)

    outcome = asyncio.run(
        engine.start(
            "alpha",
            spawn_mode="worktree",
            permission_mode="plan",
            resume_mode="pty",
            custom_name="mybridge",
            sandbox="on",
        )
    )

    assert outcome.instance is inst and outcome.created is True
    name, kw = runner.spawn_calls[0]
    assert name == "alpha"
    # Every option is forwarded unchanged — no hidden coercion of the bridge mode.
    assert kw == {
        "spawn_mode": "worktree",
        "permission_mode": "plan",
        "resume_mode": "pty",
        "custom_name": "mybridge",
        "sandbox": "on",
        "trust": False,
    }


def test_start_passes_trust_into_spawn_not_a_separate_write(tmp_path, projects_root):
    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    engine = _engine(tmp_path, projects_root, runner)

    asyncio.run(engine.start("alpha", trust=True))

    # --trust flows through spawn_detailed (applied after validation, atomically),
    # NOT a separate pre-spawn trust_project write that a later invalid option couldn't
    # roll back.
    _, kw = runner.spawn_calls[0]
    assert kw["trust"] is True
    assert runner.trusted == []  # engine no longer calls trust_project directly


def test_stop_resolves_then_delegates(tmp_path, projects_root):
    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    engine = _engine(tmp_path, projects_root, runner)

    assert asyncio.run(engine.stop("i1")) is inst
    assert runner.stopped == ["i1"]


def test_stop_unknown_identity_returns_none_without_delegating(tmp_path, projects_root):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)

    assert asyncio.run(engine.stop("nope")) is None
    assert runner.stopped == []  # never called runner.stop for an unresolved id


def test_resume_resolves_then_delegates(tmp_path, projects_root):
    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    engine = _engine(tmp_path, projects_root, runner)

    assert asyncio.run(engine.resume("i1")) is inst
    assert runner.resumed == ["i1"]


def test_resume_unknown_identity_returns_none_without_delegating(tmp_path, projects_root):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)

    assert asyncio.run(engine.resume("nope")) is None
    assert runner.resumed == []  # never called runner.resume for an unresolved id


# -- connect_url: session link, else composer link, else None ------------------


def test_connect_url_prefers_session_url_then_url_then_none(tmp_path, projects_root):
    live = RemoteControlInstance(
        instance_id="live", project="alpha", label="alpha", starter_session_id="s1"
    )
    composer = RemoteControlInstance(
        instance_id="composer", project="beta", label="beta", url="https://claude.ai/code?x"
    )
    bare = RemoteControlInstance(instance_id="bare", project="gamma", label="gamma")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[live, composer, bare])
    engine = _engine(tmp_path, projects_root, runner)

    assert engine.connect_url("live") == "https://claude.ai/code/s1?from=cli"
    assert engine.connect_url("composer") == "https://claude.ai/code?x"
    assert engine.connect_url("bare") is None
    assert engine.connect_url("unknown") is None


# -- bridge_log_path: raw preferred, else debug mirror, else None --------------


def test_bridge_log_path_prefers_raw_then_debug(tmp_path, projects_root):
    raw = RemoteControlInstance(
        instance_id="raw",
        project="alpha",
        label="alpha",
        bridge_raw_log_path=tmp_path / "raw.log",
        bridge_debug_log_path=tmp_path / "debug.log",
    )
    debug_only = RemoteControlInstance(
        instance_id="dbg", project="beta", label="beta", bridge_debug_log_path=tmp_path / "d.log"
    )
    none = RemoteControlInstance(instance_id="none", project="gamma", label="gamma")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[raw, debug_only, none])
    engine = _engine(tmp_path, projects_root, runner)

    assert engine.bridge_log_path("raw") == tmp_path / "raw.log"
    assert engine.bridge_log_path("dbg") == tmp_path / "d.log"
    assert engine.bridge_log_path("none") is None
    assert engine.bridge_log_path("unknown") is None


# -- read_log_lines: offset advance + redaction --------------------------------


def test_read_log_lines_advances_offset_and_sanitizes(tmp_path):
    log = tmp_path / "bridge.log"
    # An ANSI-coloured line — sanitize_line strips the escape sequences.
    log.write_text("plain line\n\x1b[31mred\x1b[0m line\n", encoding="utf-8")

    offset, lines = ClausterEngine.read_log_lines(log, 0)
    assert lines == ["plain line", "red line"]  # ANSI stripped by the redaction path
    assert offset == log.stat().st_size

    # Nothing new past the tail → no lines, offset unchanged.
    offset2, lines2 = ClausterEngine.read_log_lines(log, offset)
    assert lines2 == []
    assert offset2 == offset


def test_initial_log_offset_delegates(tmp_path):
    log = tmp_path / "small.log"
    log.write_text("one line\n", encoding="utf-8")
    # A file smaller than the tail window starts from the beginning (offset 0).
    assert ClausterEngine.initial_log_offset(log) == 0


# -- lifecycle: dispose ownership + hydrate ------------------------------------


def test_dispose_is_noop_for_injected_runner(tmp_path, projects_root):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)
    with engine:
        pass
    assert runner.persistence.disposed == 0  # the app owns an injected runner


def test_headless_engine_disposes_its_own_runner(tmp_path, projects_root):
    cfg = ClausterConfig.model_validate(
        {"projects_root": str(projects_root), "state_dir": str(tmp_path / "state")}
    )
    engine = ClausterEngine(cfg)  # no runner → builds + owns one
    disposed = {"n": 0}
    engine._runner.persistence.dispose = lambda: disposed.__setitem__("n", disposed["n"] + 1)  # type: ignore[method-assign]
    with engine:
        pass
    assert disposed["n"] == 1


def test_hydrate_reattaches_read_only_without_persisting(tmp_path, projects_root):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)
    asyncio.run(engine.hydrate())
    # Read-only reattach: rediscover once with persist=False, no poll_once emit path.
    assert runner.rediscovered == 1
    assert runner.persist_arg is False
