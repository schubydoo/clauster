from __future__ import annotations

import json
from pathlib import Path

from clauster import inspector
from clauster.models import Attribution


def _agent(pid, cwd, sid, kind="interactive", started=1716998400000, state=None):
    item = {
        "pid": pid,
        "cwd": cwd,
        "kind": kind,
        "startedAt": started,
        "sessionId": sid,
    }
    if state is not None:
        item["state"] = state
    return item


def test_parse_agents_json_empty():
    assert inspector.parse_agents_json("") == []
    assert inspector.parse_agents_json("[]") == []


def test_parse_agents_json_skips_malformed():
    payload = json.dumps([_agent(1, "/a", "uuid-1"), {"pid": 2}])  # second lacks fields
    sessions = inspector.parse_agents_json(payload)
    assert len(sessions) == 1
    assert sessions[0].pid == 1
    assert sessions[0].local_uuid == "uuid-1"


def test_parse_agents_json_tolerates_unexpected_shapes():
    # Valid JSON but an unexpected shape must return [] rather than crashing: a
    # top-level scalar/None/bool would raise AttributeError on ``.get``, and a dict
    # whose agents/sessions value isn't a list would raise TypeError on iteration.
    # (fail-closed liveness holds: malformed JSON still raises at json.loads.)
    assert inspector.parse_agents_json("5") == []
    assert inspector.parse_agents_json('"a string"') == []
    assert inspector.parse_agents_json("true") == []
    assert inspector.parse_agents_json("null") == []
    # dict with a non-list agents/sessions value (CodeRabbit catch)
    assert inspector.parse_agents_json('{"agents": null}') == []
    assert inspector.parse_agents_json('{"agents": 5}') == []
    assert inspector.parse_agents_json('{"sessions": "nope"}') == []
    assert inspector.parse_agents_json("{}") == []  # neither key present


def test_parse_agents_json_deep_nesting_converts_to_jsondecode():
    # Deeply-nested JSON overflows CPython's recursive scanner; parse_agents_json
    # converts that RecursionError to JSONDecodeError so callers that already handle
    # the strict-parse failure (e.g. the runner cross-check) degrade uniformly rather
    # than on a stray RecursionError.
    try:
        inspector.parse_agents_json("[" * 100_000)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("expected JSONDecodeError on deeply-nested JSON")


def test_reconcile_attributes_by_resolved_cwd(tmp_path: Path):
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(10, str(proj), "u-tracked"), _agent(11, "/somewhere/else", "u-ext")])
    )
    result = inspector.reconcile(sessions, {proj: "alpha"})
    by_uuid = {s.local_uuid: s for s in result}
    assert by_uuid["u-tracked"].attribution is Attribution.TRACKED
    assert by_uuid["u-tracked"].parent_instance == "alpha"
    assert by_uuid["u-ext"].attribution is Attribution.EXTERNAL


def test_reconcile_normalizes_trailing_slash(tmp_path: Path):
    proj = tmp_path / "beta"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(12, str(proj) + "/", "u")]))
    result = inspector.reconcile(sessions, {proj: "beta"})
    assert result[0].attribution is Attribution.TRACKED


def test_parse_agents_json_drops_terminal_states():
    # Agent view (2.1.139+) can list finished sessions; done/failed/stopped are
    # not working sessions and must not count as live anywhere.
    payload = json.dumps(
        [
            _agent(1, "/a", "u-working", state="working"),
            _agent(2, "/a", "u-blocked", state="blocked"),
            _agent(3, "/a", "u-legacy"),  # pre-agent-view item: no state field
            _agent(4, "/a", "u-done", state="done"),
            _agent(5, "/a", "u-failed", state="failed"),
            _agent(6, "/a", "u-stopped", state="stopped"),
        ]
    )
    sessions = inspector.parse_agents_json(payload)
    assert [s.local_uuid for s in sessions] == ["u-working", "u-blocked", "u-legacy"]
    assert sessions[0].state == "working"
    assert sessions[2].state == ""


def test_reconcile_background_kind_never_tracked(tmp_path: Path):
    # A `claude --bg` session in a managed project's cwd is NOT the bridge's
    # session — attributing it TRACKED would be a false liveness signal.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(10, str(proj), "u-bg", kind="background", state="working")])
    )
    result = inspector.reconcile(sessions, {proj: "alpha"})
    assert result[0].attribution is Attribution.UNTRACKED
    assert result[0].parent_instance is None


def test_reconcile_background_kind_never_external():
    # EXTERNAL would phantom-delete a stopped managed record and surface
    # "external session active" for what is not a bridge.
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(11, "/somewhere/else", "u-bg", kind="background")])
    )
    result = inspector.reconcile(sessions, {})
    assert result[0].attribution is Attribution.UNTRACKED


def test_reconcile_unknown_kind_stays_untracked(tmp_path: Path):
    # Allowlist, not blocklist: a future kind doesn't join either (fail-closed).
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(12, str(proj), "u-new", kind="subagent")])
    )
    result = inspector.reconcile(sessions, {proj: "alpha"})
    assert result[0].attribution is Attribution.UNTRACKED


def test_reconcile_missing_kind_still_joins(tmp_path: Path):
    # Pre-agent-view CLI compat: an item without `kind` still attributes by cwd.
    proj = tmp_path / "alpha"
    proj.mkdir()
    item = _agent(13, str(proj), "u-old")
    del item["kind"]
    result = inspector.reconcile(inspector.parse_agents_json(json.dumps([item])), {proj: "alpha"})
    assert result[0].attribution is Attribution.TRACKED
