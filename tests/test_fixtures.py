"""Validate domain models against the real captured artifacts in fixtures/.

Provenance:
  - pointers/*.bridge-pointer.json   lifted verbatim from ~/.claude/projects/<cwd>/
  - transcripts/test1-session.jsonl  lifted verbatim from the same capture
  - bridge-logs/test1-bridge-debug.log  faithful reproduction of the captured
    sample preserved in conductor_spec_v3.md (the raw .log was not retained)
"""

from __future__ import annotations

import json

import pytest

from clauster.models import BridgePointer

POINTER_FILES = ["test1", "dockerize2", "test2"]


@pytest.mark.parametrize("name", POINTER_FILES)
def test_bridge_pointer_parses_real_capture(fixtures_dir, name):
    raw = (fixtures_dir / "pointers" / f"{name}.bridge-pointer.json").read_text()
    pointer = BridgePointer.model_validate_json(raw)
    assert pointer.session_id.startswith("session_")
    assert pointer.environment_id.startswith("env_")
    assert pointer.pid > 0
    assert pointer.proc_start.isdigit()  # Linux jiffies, string form


def test_bridge_log_contains_documented_markers(fixtures_dir):
    log = (fixtures_dir / "bridge-logs" / "test1-bridge-debug.log").read_text()
    assert "[bridge:api] POST /v1/environments/bridge -> 200 environment_id=env_" in log
    assert "[bridge:init] Created initial session session_" in log
    assert "[bridge:shutdown] SIGINT received" in log


def test_transcript_is_jsonl(fixtures_dir):
    lines = (fixtures_dir / "transcripts" / "test1-session.jsonl").read_text().splitlines()
    assert lines, "transcript fixture is empty"
    for line in lines:
        json.loads(line)  # every line must be valid JSON
