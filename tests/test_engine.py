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
from clauster.redact import sanitize_line


class _FakePersistence:
    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


class _FakeRunner:
    candidates: list[str] = []
    candidates_kind: str | None = "prefix"
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
        # When set, resume_detailed returns this outcome instead of a created=True one —
        # lets a test drive the standard-singleton-cap decline (#1148).
        self.resume_detailed_outcome = None
        self.trusted: list[str] = []

    def list_instances(self) -> list[RemoteControlInstance]:
        return list(self._instances.values())

    def resolve_bridge_id(self, identity: str) -> str | None:
        # Exact-match only, on purpose: the engine is a passthrough, and the real
        # prefix/ambiguity logic is pinned against the real resolver in test_runner.py.
        return identity if identity in self._instances else None

    def bridge_id_candidates(self, identity: str) -> list[str]:
        return list(self.candidates)

    def bridge_id_ambiguity(self, identity: str) -> tuple[list[str], str | None]:
        cands = list(self.candidates)
        return cands, (self.candidates_kind if cands else None)

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
        return (await self.resume_detailed(instance_id)).instance

    async def resume_detailed(self, instance_id: str):
        from clauster.runner import SpawnOutcome

        self.resumed.append(instance_id)
        if self.resume_detailed_outcome is not None:
            return self.resume_detailed_outcome
        return SpawnOutcome(instance=self._instances[instance_id], created=True)

    async def trust_project(self, name: str) -> None:
        self.trusted.append(name)


def _engine(tmp_path: Path, projects_root: Path, runner: _FakeRunner) -> ClausterEngine:
    cfg = ClausterConfig.model_validate(
        {"projects_root": str(projects_root), "state_dir": str(tmp_path / "state")}
    )
    return ClausterEngine(cfg, runner=runner)  # type: ignore[arg-type]


def test_bridge_id_candidates_passes_through_to_the_runner(tmp_path, projects_root):
    # #1099: every engine resolve returns None for an ambiguous prefix exactly as it does
    # for an unknown one, so this is the only way a CLI/MCP caller can tell the two apart
    # and name the ids to retry with instead of printing a bare "not found".
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)
    assert engine.bridge_id_candidates("nope") == []
    runner.candidates = ["f2c456fd-aaaa", "f2c456fd-bbbb"]
    try:
        assert engine.bridge_id_candidates("f2c456fd") == ["f2c456fd-aaaa", "f2c456fd-bbbb"]
    finally:
        runner.candidates = []


def test_bridge_id_ambiguity_passes_through_to_the_runner(tmp_path, projects_root):
    # #1150: the facade carries the resolver's ambiguity KIND ("prefix" vs "project") out
    # to the CLI/HTTP callers so they word the retry hint from the verdict rather than
    # re-deriving it from the candidate strings (which misfires for a hex-ish project name).
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)
    assert engine.bridge_id_ambiguity("nope") == ([], None)
    runner.candidates = ["f2c456fd-aaaa", "a1b2c3d4"]
    runner.candidates_kind = "project"
    try:
        assert engine.bridge_id_ambiguity("myproj") == (["f2c456fd-aaaa", "a1b2c3d4"], "project")
    finally:
        runner.candidates = []
        runner.candidates_kind = "prefix"


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


def test_resume_detailed_returns_the_full_outcome(tmp_path, projects_root):
    # #1148: resume_detailed threads the standard-singleton cap's decline (created=False
    # with a reason) instead of collapsing it to the instance, so the MCP tool can report
    # `resumed: false` rather than a resume that never happened.
    from clauster.runner import SpawnOutcome

    inst = RemoteControlInstance(instance_id="i1", project="alpha", label="alpha")
    runner = _FakeRunner(claude_json=tmp_path / "claude.json", instances=[inst])
    runner.resume_detailed_outcome = SpawnOutcome(
        instance=inst, created=False, reason="a live standard bridge already exists"
    )
    engine = _engine(tmp_path, projects_root, runner)

    outcome = asyncio.run(engine.resume_detailed("i1"))
    assert outcome.created is False
    assert outcome.reason == "a live standard bridge already exists"
    assert outcome.instance is inst
    assert runner.resumed == ["i1"]


def test_resume_detailed_unknown_identity_returns_none_without_delegating(tmp_path, projects_root):
    runner = _FakeRunner(claude_json=tmp_path / "claude.json")
    engine = _engine(tmp_path, projects_root, runner)

    assert asyncio.run(engine.resume_detailed("nope")) is None
    assert runner.resumed == []


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


def test_read_log_lines_never_prints_a_secret_split_across_two_reads(tmp_path):
    # #1105, the security case. `sanitize_line` matches whole tokens, so a mid-line flush
    # landing INSIDE a secret used to print the fragment verbatim (it matches no pattern)
    # and the remainder on the next poll — reassembling the secret on the operator's
    # terminal from a stream `--help`, the docs and this method all document as redacted.
    log = tmp_path / "bridge.log"
    # Deliberately LOW-ENTROPY, and it must stay that way. `redact.py` matches token SHAPE
    # (`gh[pousr]_[A-Za-z0-9]{16,}` — no entropy gate), so a repetitive fixture exercises the
    # matcher exactly like a random one. gitleaks' `github-pat` rule additionally requires
    # entropy >= 3, so a realistic-looking fixture here fails the `secret scan` CI job. Do not
    # "improve" this into something that looks like a real token.
    secret = "ghp_" + "FAKE" * 9
    assert sanitize_line(f"token={secret}") == "token=<redacted>"  # whole token: caught
    assert sanitize_line(f"token={secret[:8]}") == f"token={secret[:8]}"  # fragment: not

    log.write_text(f"token={secret[:8]}", encoding="utf-8")  # bridge flushed half a line
    offset, lines = ClausterEngine.read_log_lines(log, 0)
    assert lines == [], "an unterminated line must be withheld, not printed as a fragment"
    assert offset == 0, "withheld bytes must not be consumed, or the tail is lost"

    with log.open("a", encoding="utf-8") as fh:  # the bridge completes the line
        fh.write(f"{secret[8:]}\n")
    offset, lines = ClausterEngine.read_log_lines(log, offset)

    assert lines == ["token=<redacted>"]
    assert offset == log.stat().st_size
    # The whole point: neither read put any part of the secret on stdout.
    assert secret[8:] not in "".join(lines)


def test_read_log_lines_emits_complete_lines_and_holds_only_the_partial(tmp_path):
    # The withholding must not cost the lines that ARE complete in the same read, and the
    # rewound offset must resume exactly at the partial rather than re-emitting a line.
    log = tmp_path / "bridge.log"
    # write_bytes, not write_text: this asserts a BYTE offset, and text mode translates
    # "\n"->"\r\n" on Windows, which would make the expected offset platform-dependent.
    log.write_bytes(b"first\nsecond\npart")

    offset, lines = ClausterEngine.read_log_lines(log, 0)
    assert lines == ["first", "second"]
    assert offset == len(b"first\nsecond\n")

    with log.open("ab") as fh:
        fh.write(b"ial\n")
    offset, lines = ClausterEngine.read_log_lines(log, offset)
    assert lines == ["partial"], "the held fragment must rejoin its remainder, not duplicate"
    assert offset == log.stat().st_size


def test_read_log_lines_holds_a_multibyte_partial_by_bytes_not_characters(tmp_path):
    # The offset is a BYTE offset; rewinding by len(str) would desync on any non-ASCII
    # log line and corrupt every subsequent read.
    log = tmp_path / "bridge.log"
    # write_bytes, not write_text: the assertion below is a BYTE count, and text mode
    # translates "\n"->"\r\n" on Windows, making it platform-dependent.
    log.write_bytes("done\nté".encode())  # 'é' is 2 bytes, 1 character

    offset, lines = ClausterEngine.read_log_lines(log, 0)
    assert lines == ["done"]
    assert offset == len(b"done\n")

    with log.open("ab") as fh:
        fh.write(b"st\n")
    offset, lines = ClausterEngine.read_log_lines(log, offset)
    assert lines == ["tést"]
    assert offset == log.stat().st_size


def test_read_log_lines_strips_the_cr_of_a_crlf_log(tmp_path):
    # Line completeness can only be judged on "\n", so this path splits on "\n" rather
    # than str.splitlines(). Strip the lone \r so a CRLF-written log reads unchanged.
    log = tmp_path / "bridge.log"
    log.write_bytes(b"alpha\r\nbeta\r\n")
    _, lines = ClausterEngine.read_log_lines(log, 0)
    assert lines == ["alpha", "beta"]


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
