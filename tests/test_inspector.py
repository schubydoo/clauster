from __future__ import annotations

import json
from pathlib import Path

from clauster import inspector
from clauster.models import Attribution


def _agent(pid, cwd, sid, kind="interactive", started=1716998400000):
    return {"pid": pid, "cwd": cwd, "kind": kind, "startedAt": started, "sessionId": sid}


def test_parse_agents_json_empty():
    assert inspector.parse_agents_json("") == []
    assert inspector.parse_agents_json("[]") == []


def test_parse_agents_json_skips_malformed():
    payload = json.dumps([_agent(1, "/a", "uuid-1"), {"pid": 2}])  # second lacks fields
    sessions = inspector.parse_agents_json(payload)
    assert len(sessions) == 1
    assert sessions[0].pid == 1
    assert sessions[0].local_uuid == "uuid-1"


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
