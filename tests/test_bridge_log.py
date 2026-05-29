from __future__ import annotations

from pathlib import Path

from clauster.bridge_log import parse_bridge_markers


def test_parses_full_happy_log(fixtures_dir: Path):
    text = (fixtures_dir / "bridge-logs" / "test1-bridge-debug.log").read_text()
    m = parse_bridge_markers(text)
    assert m.bridge_id == "97051a45-aaaa-bbbb-cccc-ddddeeeeffff"
    assert m.environment_id == "env_01RHE7cHW3DawXjGRp5Ae3va"
    assert m.starter_session_id == "session_01LG15p2JVjwBamscENjuBLi"
    assert m.spawn_mode == "same-dir"
    assert m.session_pids == [81805]
    assert m.poll_loop_started is True
    assert m.clean_shutdown is True
    assert m.is_ready is True


def test_partial_log_before_poll_loop_is_not_ready():
    text = (
        "[bridge:init] bridgeId=abc123de dir=/x\n"
        "[bridge:api] POST /v1/environments/bridge -> 200 environment_id=env_01PARTIAL\n"
    )
    m = parse_bridge_markers(text)
    assert m.environment_id == "env_01PARTIAL"
    assert m.poll_loop_started is False
    assert m.is_ready is False  # no poll loop yet


def test_trust_error_detected():
    m = parse_bridge_markers("[ERROR] Workspace not trusted\n")
    assert m.trust_error is True
    assert m.is_ready is False


def test_environment_id_alt_form():
    m = parse_bridge_markers("[bridge:work] Starting poll loop environmentId=env_01ALTFORM\n")
    assert m.environment_id == "env_01ALTFORM"
    assert m.poll_loop_started is True
    assert m.is_ready is True


def test_empty_log_is_blank():
    m = parse_bridge_markers("")
    assert m.bridge_id is None
    assert m.environment_id is None
    assert m.is_ready is False
