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


def test_poison_reason_archived_detected():
    # #867 L3: the server's end_session control request marks a reattached-but-gone anchor.
    log = (
        "[bridge:work] Starting poll loop environmentId=env_01X\n"
        '[bridge:ws] sessionId=[REDACTED] <<< {"request":{"reason":"archived",'
        '"subtype":"end_session"},"type":"control_request"}\n'
    )
    m = parse_bridge_markers(log)
    assert m.poison_reason == "archived"


def test_poison_reason_deleted_detected():
    m = parse_bridge_markers('{"reason": "deleted", "subtype": "end_session"}')
    assert m.poison_reason == "deleted"


def test_no_poison_in_healthy_log():
    assert parse_bridge_markers("[bridge:work] Starting poll loop env_01X\n").poison_reason is None


def test_resume_session_from_unarchive_line():
    # A --continue resume reconnects to an existing session and never logs "Created
    # initial session"; it logs the session it resumed as "[remote-bridge] Unarchive
    # session_<id>". Recover it so the deep link works after a true-resume.
    text = "[DEBUG] [remote-bridge] Unarchive session_01WE4dP9b4JoxYfV5r4X6PFG status=409\n"
    assert parse_bridge_markers(text).starter_session_id == "session_01WE4dP9b4JoxYfV5r4X6PFG"


def test_created_initial_session_wins_over_unarchive():
    # A fresh start's explicit "Created initial session" takes precedence over a
    # later Unarchive line (first-wins, fresh source preferred).
    text = (
        "Created initial session session_FRESH01\n"
        "[remote-bridge] Unarchive session_OLD02 status=409\n"
    )
    assert parse_bridge_markers(text).starter_session_id == "session_FRESH01"
