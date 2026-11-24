from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from clauster import bridge_log
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


def test_poison_reason_detected_regardless_of_key_order():
    # The CLI reordering the JSON keys must not hide the poison.
    assert parse_bridge_markers('{"subtype":"end_session","reason":"archived"}').poison_reason == (
        "archived"
    )
    # ...but an end_session with a benign reason (different object) is not poison.
    log = '{"subtype":"end_session","reason":"user_stop"} {"reason":"archived","foo":1}'
    assert parse_bridge_markers(log).poison_reason is None


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


# ----- TASK-471: the marker search is linear ------------------------------------------------

# The joined patterns as they were before TASK-471. They are the oracle: the split search must
# find the same match, with the same groups, for every text.
_JOINED = {
    "bridge_id": re.compile(r"\[bridge:init\][^\n]*\bbridgeId=([0-9a-fA-F-]{8,})"),
    "resume": re.compile(r"\[remote-bridge\][^\n]*\bUnarchive\s+(session_[A-Za-z0-9]+)"),
    "poll_loop": re.compile(r"\[bridge:work\][^\n]*Starting poll loop"),
    "shutdown": re.compile(r"\[bridge:shutdown\][^\n]*(?:SIGINT|SIGTERM|shutting down)"),
    "end_session": re.compile(
        r'"subtype":\s*"end_session"[^{}]*"reason":\s*"(archived|deleted)"'
        r'|"reason":\s*"(archived|deleted)"[^{}]*"subtype":\s*"end_session"'
    ),
}

_ATOMS = (
    "[bridge:init]", "[bridge:work]", "[bridge:shutdown]", "[remote-bridge]", " bridgeId=",
    "bridgeId=", "0123abcd", "-", "ef", "b", "\n", " ", "\t", "Unarchive", " session_",
    "session_AbC9", "Starting poll loop", "SIGINT", "SIGTERM", "shutting down", '"subtype":',
    ' "end_session"', '"end_session"', '"reason":', ' "archived"', '"deleted"', "{", "}", "x",
    "Created initial session ", "\r", "\n\n  ", "Unarchive\n session_Q", '"reason":\n"deleted"',
)  # fmt: skip


def _oracle(text: str) -> dict[str, object]:
    """The five fields the split patterns set, read with the joined patterns."""
    hit = _JOINED["bridge_id"].search(text)
    resume = _JOINED["resume"].search(text)
    gone = _JOINED["end_session"].search(text)
    return {
        "bridge_id": hit.group(1) if hit else None,
        "resume": resume.group(1) if resume else None,
        "poll_loop": _JOINED["poll_loop"].search(text) is not None,
        "shutdown": _JOINED["shutdown"].search(text) is not None,
        "poison": (gone.group(1) or gone.group(2)) if gone else None,
    }


def _split(text: str) -> dict[str, object]:
    m = parse_bridge_markers(text)
    resume = bridge_log._first_hit(text, bridge_log._RESUME_SESSION)
    return {
        "bridge_id": m.bridge_id,
        "resume": resume[1].group(1) if resume else None,
        "poll_loop": m.poll_loop_started,
        "shutdown": m.clean_shutdown,
        "poison": m.poison_reason,
    }


def test_split_markers_match_the_joined_patterns():
    rng = random.Random(471)  # noqa: S311 — a reproducible test stream, not crypto
    for _ in range(4000):
        text = "".join(rng.choice(_ATOMS) for _ in range(rng.randint(0, 40)))
        assert _split(text) == _oracle(text), repr(text)


@pytest.mark.parametrize(
    "text",
    [
        "[bridge:init] a bridgeId=12345678 bridgeId=abcdef01\n",  # the LAST id on the line
        "[bridge:init] bridgeId=1234567-bridgeId=abcdefab\n",  # an id inside a hex run
        "[bridge:init] x\n[bridge:init] bridgeId=deadbeef\n",  # the first line fails
        "[bridge:init] [bridge:init] bridgeId=deadbeef9\n",
        "[remote-bridge] Unarchive\n  session_NEXTLINE\n",  # `\s+` may cross the line end
        '{"subtype": "end_session", "reason": "archived"} {"reason": "deleted"}',
        '{"reason": "deleted", "x": {"subtype": "end_session"}}',  # another object: no hit
        '"reason": "deleted" "subtype": "end_session" "reason": "archived"',  # first one wins
        '"subtype":"end_session","reason":"archived","reason":"deleted"',  # the last reason
    ],
)
def test_split_markers_match_the_joined_patterns_on_edge_cases(text: str):
    assert _split(text) == _oracle(text)


class _Counted:
    """A pattern that adds to ``ops`` the length of text one call may read (its budget)."""

    def __init__(self, rx: re.Pattern[str], edge: re.Pattern[str], ops: list[int]) -> None:
        self.rx, self.edge, self.ops = rx, edge, ops

    def search(self, text: str, pos: int) -> re.Match[str] | None:
        hit = self.rx.search(text, pos)
        self.ops[0] += (hit.end() if hit else len(text)) - pos
        return hit

    def match(self, text: str, pos: int) -> re.Match[str] | None:
        # A tail reads on only to the next edge: that is its budget.
        stop = self.edge.search(text, pos)
        self.ops[0] += (stop.start() if stop else len(text)) - pos
        return self.rx.match(text, pos)


def _count_ops(monkeypatch, text: str) -> int:
    """Parse ``text`` with every split pattern counted; return the characters they may read."""
    ops = [0]

    def counted(marker: bridge_log._Marker) -> bridge_log._Marker:
        return bridge_log._Marker(
            _Counted(marker.head, marker.edge, ops),  # type: ignore[arg-type]
            _Counted(marker.tail, marker.edge, ops),  # type: ignore[arg-type]
            _Counted(marker.edge, marker.edge, ops),  # type: ignore[arg-type]
        )

    with monkeypatch.context() as patch:
        for name in ("_BRIDGE_ID", "_RESUME_SESSION", "_POLL_LOOP", "_SHUTDOWN"):
            patch.setattr(bridge_log, name, counted(getattr(bridge_log, name)))
        patch.setattr(
            bridge_log, "_END_SESSION_GONE", tuple(map(counted, bridge_log._END_SESSION_GONE))
        )
        parse_bridge_markers(text)
    return ops[0]


def _naive_first_hit(text: str, marker: bridge_log._Marker):
    """What `search` with the joined pattern does: try the tail again after every head."""
    pos = 0
    while (head := marker.head.search(text, pos)) is not None:
        if (tail := marker.tail.match(text, head.end())) is not None:
            return head, tail
        pos = head.start() + 1
    return None


_CRAFTED = (
    "[bridge:init] ",
    "[remote-bridge] ",
    "[bridge:work] ",
    "[bridge:shutdown] ",
    '"subtype": "end_session" ',
    '"reason": "archived" ',
)


@pytest.mark.parametrize("unit", _CRAFTED)
def test_marker_search_reads_each_character_a_few_times(monkeypatch, unit: str):
    # A line of N heads: the joined patterns read about N * len(line) / 2 characters, which
    # took 0.42, 1.76 and 6.97 s at 2000, 4000 and 8000 heads. This counts what the split
    # search may read, not time: the bound is a multiple of the text length.
    for text in (unit * 2000, unit * 8000, (unit * 50 + "\n") * 200):
        assert _count_ops(monkeypatch, text) <= 16 * len(text)
    # The count grows with the text, not with its square.
    small, large = _count_ops(monkeypatch, unit * 2000), _count_ops(monkeypatch, unit * 8000)
    assert large <= 4.5 * small


@pytest.mark.parametrize("unit", _CRAFTED)
def test_marker_search_count_catches_a_quadratic_search(monkeypatch, unit: str):
    # Positive control: the same count on the search the joined patterns did fails both checks.
    monkeypatch.setattr(bridge_log, "_first_hit", _naive_first_hit)
    # Smaller texts than above, to keep the quadratic search cheap; it fails the checks anyway.
    text = unit * 800
    assert _count_ops(monkeypatch, text) > 16 * len(text)
    small, large = _count_ops(monkeypatch, unit * 200), _count_ops(monkeypatch, unit * 800)
    assert large > 4.5 * small
