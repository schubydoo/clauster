"""Exact-value redaction (#1621): a value Clauster knows is masked on every output path.

The oracle models what a reader can SEE, not the tokens of the returned string. It removes
every character a value cannot hold, so a wrap, a line break, padding, a box border or an
escape cannot hide a value that the reader reassembles. It then looks for any run of
``KNOWN_FRAGMENT_MIN`` characters of the value's random part, which is the guarantee
``docs/security.md`` states.

Every test also runs the oracle on its INPUT and requires a leak there (the positive
control), so a test cannot pass because the oracle is blind. ``test_shape_masks_alone_leak``
shows the store is what closes each documented residue: with the store empty, the same
inputs leak.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from clauster import pty_screen, redact, supervisor
from clauster.app import create_app
from clauster.hosted import _redact_obj
from clauster.models import InstanceStatus, RemoteControlInstance
from clauster.pty_screen import PtyScreen
from clauster.runner import SessionRunner
from clauster.usage import _line_to_turn

# Shaped like real ids: the prefix, then `01` and 22 base62 characters.
ENV = "env_01Xk3PqR8sTvW2yZ4bNc6dFg"
SESSION = "session_01Hm7JtL9wQe5RuY3iOp2aSd"
CSE = "cse_01Hm7JtL9wQe5RuY3iOp2aSd"  # derived from SESSION (code_sessions.code_session_id_for)
BRIDGE = "3f2b9c1e-7a4d-4e8b-9c2f-1d6a5b4e3c21"
VALUES = (ENV, SESSION, CSE, BRIDGE)

_NON_ID = re.compile(r"[^A-Za-z0-9_-]+")


def _random_part(value: str) -> str:
    return re.sub(r"\A(?:env|session|cse)_", "", value)


def _close(a: str, b: str) -> bool:
    """Report whether ``a`` and ``b`` differ in at most one character."""
    return sum(x != y for x, y in zip(a, b, strict=True)) <= 1


def _leaks(seen: str, value: str) -> bool:
    """Report whether a reader of ``seen`` can read a long enough run of ``value``."""
    squeezed = _NON_ID.sub("", seen)
    body = _random_part(value)
    n = redact.KNOWN_FRAGMENT_MIN
    return any(body[i : i + n] in squeezed for i in range(len(body) - n + 1))


@pytest.fixture
def known() -> None:
    """Store the test values as one instance's ids (``cse_`` comes from ``session_``)."""
    redact.remember_known_values("iid-1", ENV, SESSION, BRIDGE)


def _line_forms(value: str) -> dict[str, str]:
    """One line holding ``value`` in each shape the guarantee covers."""
    k = len(value) // 2
    other = ENV if value != ENV else SESSION
    return {
        "standalone": f"x {value} y",
        "welded-before": f"xyz{value}",  # residue of the shape masks (no boundary, no cut)
        "welded-after": f"{value}xyz",
        "underscore-chain": f"x {other}_{value} y",  # the log-path residue reported in PR 1620
        "escape-split": f"x {value[:k]}\x1b[0m{value[k:]} y",
        "osc-weld": f"agent\x1b]0;title\x07{value}",
        "invisible-split": f"x {value[:k]}​{value[k:]} y",
        "bel-split": f"x {value[:k]}\x07{value[k:]} y",
        "space-split": " ".join(value[i : i + 5] for i in range(0, len(value), 5)),
        "truncated": f"x {value[:-3]}...",
        "tail": f"...{value[-12:]} y",
        "bearer": f"Authorization: Bearer {value}",
        "url": f"https://claude.ai/code?environment={value}",
    }


_FORM_NAMES = sorted(_line_forms(ENV))


def _transcript_line(text: str) -> str:
    return json.dumps(
        {
            "timestamp": "t",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


#: Each line-shaped output path, as ``text -> what the reader sees``.
_LINE_PATHS = {
    "ws-bridge-log": redact.sanitize_line,
    "ws-bridge-log-colored": lambda t: redact._visible(
        redact.sanitize_line(t, strip_ansi_seq=False)
    ),
    "disk-mirror-and-api-errors": redact.redact_for_disk,
    "ws-hosted-frame": lambda t: json.dumps(_redact_obj({"content": [{"text": t}]})),
    "transcript-turn": lambda t: json.dumps(_line_to_turn(_transcript_line(t))),
}


@pytest.mark.usefixtures("known")
@pytest.mark.parametrize("path", sorted(_LINE_PATHS))
@pytest.mark.parametrize("form", _FORM_NAMES)
@pytest.mark.parametrize("value", VALUES)
def test_every_line_path_masks_every_form(value: str, form: str, path: str) -> None:
    text = _line_forms(value)[form]
    assert _leaks(redact._visible(text), value)  # positive control: the input leaks
    assert not _leaks(_LINE_PATHS[path](text), value)


@pytest.mark.usefixtures("known")
@pytest.mark.parametrize("value", VALUES)
def test_disk_mirror_masks_a_value_split_across_lines(value: str) -> None:
    for k in range(1, len(value)):
        text = f"first line {value[:k]}\n{value[k:]} second line\n"
        assert _leaks(text, value)
        out = redact.redact_for_disk(text)
        assert not _leaks(out, value), (k, out)
        assert out.count("\n") == 2  # the line structure survives


@pytest.mark.parametrize(
    "text",
    [
        f"xyz{ENV}",  # the welded literal residue of docs/security.md
        f"x {ENV}_{SESSION} y",  # the `_` chain residue reported in PR 1620
    ],
)
def test_shape_masks_alone_leak(text: str) -> None:
    """With the store empty, the documented residue reaches the reader: the store closes it."""
    assert _leaks(redact.sanitize_line(text), SESSION if "session_" in text else ENV)
    redact.remember_known_values("iid-1", ENV, SESSION)
    assert not _leaks(redact.sanitize_line(text), ENV)
    assert not _leaks(redact.sanitize_line(text), SESSION)


def test_an_unknown_value_keeps_the_shape_behavior() -> None:
    # The store adds masks; it changes nothing for a text that holds no stored value.
    redact.remember_known_values("iid-1", ENV)
    for text in ("x env_production y", "session_timeout_ms", "x env_01QQQQ y", "plain"):
        redact.forget_known_values("iid-1")
        before = redact.sanitize_line(text)
        redact.remember_known_values("iid-1", ENV)
        assert redact.sanitize_line(text) == before


@pytest.mark.usefixtures("known")
def test_a_short_fragment_is_not_masked() -> None:
    # Below KNOWN_FRAGMENT_MIN the run is not the value and is left alone; at the minimum it
    # is masked. This pins the boundary of the guarantee.
    n = redact.KNOWN_FRAGMENT_MIN
    body = _random_part(ENV)
    assert redact.sanitize_line(f"x {body[5 : 5 + n - 1]} y") == f"x {body[5 : 5 + n - 1]} y"
    assert redact.sanitize_line(f"x {body[5 : 5 + n]} y") == "x <redacted> y"


@pytest.mark.usefixtures("known")
def test_an_anchor_that_grows_too_little_is_not_masked() -> None:
    # The whole line is 6 characters of the random part, so the grid reads one anchor at 0,
    # which grows to 6: below the minimum, so it is left alone.
    body = _random_part(ENV)
    assert redact.sanitize_line(body[:6]) == body[:6]


@pytest.mark.usefixtures("known")
@pytest.mark.parametrize("value", VALUES)
def test_one_substituted_character_does_not_hide_the_rest(value: str) -> None:
    """A wrong id character stops one run; the rest of the value after it is grown again."""
    body_start = len(value) - len(_random_part(value))
    for k in range(body_start, len(value)):
        swapped = "Q" if value[k] != "Q" else "R"
        text = f"xyz{value[:k]}{swapped}{value[k + 1 :]} y"
        for out in (redact.sanitize_line(text), redact.redact_known_screen_rows([text])[0]):
            assert not _leaks(out, value), (k, out)


@pytest.mark.usefixtures("known")
def test_a_split_copy_before_a_whole_copy_is_masked() -> None:
    # `find` for the split copy's first run lands on the later whole copy; the count of kept
    # characters before it must reject that place.
    text = f"a xyz{ENV[:12]}\n{ENV[12:]} b {ENV} c\n"
    out = redact.redact_for_disk(text)
    assert not _leaks(out.split("b")[0], ENV) and not _leaks(out, ENV), out


@pytest.mark.usefixtures("known")
def test_two_split_values_in_one_text_are_both_masked() -> None:
    # Two split runs in one block: the second reuses the block index the first one built.
    text = f"a {ENV[:9]}\n{ENV[9:]} b {SESSION[:13]}\n  {SESSION[13:]} c\n"
    out = redact.redact_for_disk(text)
    assert not _leaks(out, ENV) and not _leaks(out, SESSION)
    assert out.count("\n") == 3


@settings(max_examples=300, deadline=None)
@given(
    value=st.sampled_from(VALUES),
    cuts=st.lists(st.integers(min_value=1, max_value=35), max_size=6, unique=True),
    seps=st.lists(
        st.sampled_from([" ", "\n", "  \t", "\x1b[0m", "\x1b]8;;x\x07", "​", "\x07", "|", "."]),
        min_size=7,
        max_size=7,
    ),
    swap=st.none() | st.integers(min_value=0, max_value=35),
    before=st.sampled_from(["", "x", "xyz", "agent ", "_", "env_", "<redacted>"]),
    after=st.sampled_from(["", "x", "_backup", " y", "-1"]),
)
def test_property_any_split_by_non_id_characters_is_masked(
    value, cuts, seps, before, after, swap
) -> None:
    """Cut the value anywhere, join the pieces with characters a value cannot hold.

    ``swap`` also replaces one character of the value by another id character: what is left
    on either side of it must still be masked wherever it holds a long enough run.
    """
    redact.remember_known_values("iid-1", ENV, SESSION, BRIDGE)
    if swap is not None and swap < len(value):
        value = value[:swap] + ("Q" if value[swap] != "Q" else "R") + value[swap + 1 :]
    points = sorted(c for c in cuts if c < len(value))
    pieces = [value[i:j] for i, j in zip([0, *points], [*points, len(value)], strict=True)]
    text = before + "".join(p + s for p, s in zip(pieces, seps, strict=False)) + after
    original = next(v for v in VALUES if len(v) == len(value) and _close(v, value))
    assert _leaks(redact._visible(text), original)
    assert not _leaks(redact.redact_for_disk(text), original)
    assert not _leaks(redact.sanitize_line(text), original)


# ----- the live pty screen ---------------------------------------------------------------


def _screen_text(rows: list[str]) -> str:
    return "".join(rows)


def _frame_rows(feed: bytes) -> tuple[list[str], list[str]]:
    screen = PtyScreen()
    screen.feed(feed)
    raw = list(screen._screen.display)
    frame = screen.frame()
    assert len(frame["rows"]) == pty_screen.SCREEN_ROWS
    assert all(len(row) == pty_screen.SCREEN_COLS for row in frame["rows"])
    return raw, frame["rows"]


@pytest.mark.usefixtures("known")
@pytest.mark.parametrize("value", VALUES)
def test_screen_masks_a_hard_wrap_at_every_split(value: str) -> None:
    for k in range(1, len(value)):
        # A real id shape always masks at the edge; the welded `zz` in front makes the shape
        # masks' job harder, the exact match does not care.
        feed = ("zz" * 60)[: pty_screen.SCREEN_COLS - k].encode() + value.encode()
        raw, rows = _frame_rows(feed)
        assert _leaks(_screen_text(raw), value)
        assert not _leaks(_screen_text(rows), value), (k, rows[:2])


@pytest.mark.usefixtures("known")
@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("k", [1, 3, 6, 10, 15, 20])
def test_screen_masks_a_soft_wrap_and_a_box_border(value: str, k: int) -> None:
    soft = f"text {value[:k]}\r\n      {value[k:]} more".encode()
    boxed = f"│ text {value[:k]} │\r\n│    {value[k:]}  │".encode()
    for feed in (soft, boxed):
        raw, rows = _frame_rows(feed)
        assert _leaks(_screen_text(raw), value)
        assert not _leaks(_screen_text(rows), value), rows[:2]


@pytest.mark.usefixtures("known")
def test_screen_masks_the_visible_tail_of_a_scrolled_value() -> None:
    raw, rows = _frame_rows(f"{ENV[-12:]} rest of the line".encode())
    assert _leaks(_screen_text(raw), ENV)
    assert not _leaks(_screen_text(rows), ENV)


def test_screen_rows_keep_their_length_and_untouched_rows_stay_identical() -> None:
    redact.remember_known_values("iid-1", ENV)
    rows = ["a" * 10 + ENV[:6], ENV[6:] + " " * 2, "unrelated row"]
    out = redact.redact_known_screen_rows(rows)
    assert [len(r) for r in out] == [len(r) for r in rows]
    assert out[2] is rows[2]
    assert not _leaks(_screen_text(out), ENV)
    # A short masked run keeps its cells with `*`; a long one becomes the token.
    assert out[0] == "a" * 10 + "******" and out[1].startswith("<redacted>")


def test_screen_pass_returns_a_copy_when_nothing_is_stored() -> None:
    rows = [ENV]
    out = redact.redact_known_screen_rows(rows)
    assert out == rows and out is not rows


def test_keeper_stores_the_session_id_it_scrapes(tmp_path: Path) -> None:
    """The keeper process renders the frames, so it must know the id it learned."""
    from clauster import pty_keeper

    sidecar, screen_sidecar = tmp_path / "k.json", tmp_path / "k.screen.json"
    base: dict[str, object] = {"state": "starting", "note": None}
    drain = pty_keeper._KeeperDrain(base, sidecar, PtyScreen(), screen_sidecar)
    drain.feed(f"connect: https://claude.ai/code/{SESSION}\r\n".encode())
    assert base["session_id"] == SESSION
    assert {SESSION, CSE} <= redact._known_owners["pty-keeper"]
    # Clear the connect URL, then draw the id split across a box border: the shape masks
    # leave that split visible (see the soft-wrap test), the exact match does not.
    drain.feed(b"\x1b[2J\x1b[H")
    drain.feed(f"│ door {CSE[:12]} │\r\n│    {CSE[12:]}  │".encode())
    drain.tick()
    frame = json.loads(screen_sidecar.read_text(encoding="utf-8"))
    assert _leaks(_screen_text(list(drain._screen._screen.display)), CSE)
    assert not _leaks(_screen_text(frame["screen"]["rows"]), CSE)


# ----- the screen relay (the Clauster process re-masks with every value it knows) -------


def _write_sidecar(path: Path, **payload) -> None:
    path.write_text(json.dumps({"seq": 1, "state": "live", "error": None, **payload}), "utf-8")


@pytest.mark.usefixtures("known")
def test_relay_masks_a_value_the_keeper_did_not_know(tmp_path: Path) -> None:
    sidecar = tmp_path / "s.json"
    rows = ["x" * 100 + ENV[:20], ENV[20:] + " " * 112]
    assert _leaks(_screen_text(rows), ENV)
    _write_sidecar(sidecar, screen={"rows": rows, "cols": 120}, error=f"screen feed: {SESSION}")
    frame = pty_screen.read_screen_sidecar(sidecar)
    assert frame is not None
    assert not _leaks(_screen_text(frame["screen"]["rows"]), ENV)
    assert [len(r) for r in frame["screen"]["rows"]] == [len(r) for r in rows]
    assert frame["screen"]["cols"] == 120  # the rest of the payload is kept
    assert not _leaks(frame["error"], SESSION)


@pytest.mark.parametrize(
    "screen", [{"rows": "not a list"}, {"rows": [1, 2]}, ["rows"], {"no": "rows"}]
)
def test_relay_fails_closed_on_a_screen_it_cannot_check(tmp_path: Path, screen) -> None:
    sidecar = tmp_path / "s.json"
    _write_sidecar(sidecar, screen=screen)
    frame = pty_screen.read_screen_sidecar(sidecar)
    assert frame is not None and frame["screen"] is None


def test_relay_keeps_a_status_frame_without_a_screen(tmp_path: Path) -> None:
    sidecar = tmp_path / "s.json"
    _write_sidecar(sidecar, screen=None, error=None)
    assert pty_screen.read_screen_sidecar(sidecar) == {
        "seq": 1,
        "state": "live",
        "error": None,
        "screen": None,
    }


# ----- end to end, at the WebSocket -------------------------------------------------------


def _client(runner_config, instance: RemoteControlInstance) -> TestClient:
    config, claude_json = runner_config
    config.claude.pty_screen_enabled = True
    runner = SessionRunner(config, claude_json=claude_json)
    runner._instances[instance.instance_id] = instance
    return TestClient(create_app(config, runner=runner))


def _append(log: Path, text: str) -> None:
    # Byte-exact `\n` on every OS: the socket splits on `\n` alone.
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _bare_instance(log: Path) -> RemoteControlInstance:
    return RemoteControlInstance(
        project="alpha", label="alpha", status=InstanceStatus.RUNNING, bridge_debug_log_path=log
    )


def test_ws_bridge_log_masks_a_learned_value_in_every_form(runner_config, tmp_path: Path) -> None:
    log = tmp_path / "alpha.log"
    _append(log, "")
    instance = _bare_instance(log)
    instance.environment_id = ENV  # learned: stored by the model, no call site involved
    forms = _line_forms(ENV)
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            _append(log, "".join(f"{forms[name]}\n" for name in _FORM_NAMES))
            for name in _FORM_NAMES:
                assert not _leaks(ws.receive_text(), ENV), name


def test_ws_bridge_log_learns_an_id_before_the_runner_does(runner_config, tmp_path: Path) -> None:
    """The socket reads the marker line itself, so the next line is masked exactly at once."""
    log = tmp_path / "alpha.log"
    _append(log, "")
    instance = _bare_instance(log)
    marker = f"[bridge:api] POST /v1/environments/bridge -> 200 environment_id={ENV}"
    residue = f"xyz{ENV}"  # the shape masks leave this whole (test_shape_masks_alone_leak)
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            # Receive a first line, so the head read at connect is over before the marker is
            # written: only the per-line learning can see it.
            _append(log, "sentinel\n")
            assert ws.receive_text() == "sentinel"
            _append(log, f"{marker}\n{residue}\n")
            assert not _leaks(ws.receive_text(), ENV)
            assert not _leaks(ws.receive_text(), ENV)
    assert instance.environment_id is None  # the runner never parsed it: the socket did
    assert ENV in redact._known_owners[instance.instance_id]


def test_ws_bridge_log_reads_the_ids_above_its_tail(runner_config, tmp_path: Path) -> None:
    """After a restart the record may hold no id; the marker line is above the tail start."""
    log = tmp_path / "alpha.log"
    marker = f"[bridge:api] POST /v1/environments/bridge -> 200 environment_id={ENV}"
    _append(log, marker + "\n" + "old line\n" * 20_000)
    instance = _bare_instance(log)
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            _append(log, f"xyz{ENV}\n")
            line = ws.receive_text()
            while line == "old line":  # the tail may begin inside the old lines
                line = ws.receive_text()
            assert "xyz" in line and not _leaks(line, ENV)
    assert instance.environment_id is None


def test_ws_bridge_log_stores_one_id_of_each_kind(runner_config, tmp_path: Path) -> None:
    """Log text that names many ids cannot grow the store: the first of each kind wins."""
    log = tmp_path / "alpha.log"
    _append(log, "")
    instance = _bare_instance(log)
    echoed = [f"tool said environment_id=env_01{i:04d}Zq9Wx7Vc5Bn3Mk1Lj" for i in range(50)]
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            # The echoed ids come while the session id is still unknown, so only the per-kind
            # first-wins rule keeps them out of the store.
            _append(log, f"environment_id={ENV}\n")
            _append(log, "".join(f"{line}\n" for line in echoed))
            _append(log, f"Created initial session {SESSION}\n")
            for _ in range(2 + len(echoed)):
                ws.receive_text()
    assert redact._known_owners[instance.instance_id] == {ENV, SESSION, CSE}


def test_ws_bridge_log_takes_a_second_id_only_from_a_bridge_line(
    runner_config, tmp_path: Path
) -> None:
    """A bridge that registers again is masked exactly; echoed text cannot add a second id."""
    env2 = "env_01Tq4Wr6Ey8Uz2Ix9Oc3Pv5Ab7"
    env3 = "env_01Mn5Bv7Cx9Za2Sd4Fg6Hj8Kl1"
    log = tmp_path / "alpha.log"
    _append(log, "")
    instance = _bare_instance(log)
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            _append(log, f"t [DEBUG] [bridge:api] POST -> 200 environment_id={ENV}\n")
            _append(log, f"tool said environment_id={env3}\n")  # echoed: not the bridge
            _append(log, f"t [DEBUG] [bridge:api] POST -> 200 environment_id={env2}\n")
            _append(log, f"weld{env2}weld\n")
            _append(log, f"t [bridge:init] bridgeId={BRIDGE} dir=x\n")
            out = [ws.receive_text() for _ in range(5)]
    assert not _leaks(out[3], env2)
    assert redact._known_owners[instance.instance_id] == {ENV, env2, BRIDGE}


def test_a_fake_marker_first_cannot_shut_out_the_real_id() -> None:
    """Echoed text names an env id before the bridge's own line (a rotated log, say)."""
    from clauster.routes import websockets as ws_module

    learned: dict[str, set[str]] = {}
    fake = "env_01Mn5Bv7Cx9Za2Sd4Fg6Hj8Kl1"
    ws_module._learn_stream_ids("iid-1", f"tool said environment_id={fake}", learned)
    ws_module._learn_stream_ids("iid-1", f"t [bridge:api] environment_id={ENV}", learned)
    assert ENV in redact._known_owners["iid-1"]
    assert not _leaks(redact.sanitize_line(f"xyz{ENV}"), ENV)


def test_a_replace_that_changes_no_value_keeps_the_matcher() -> None:
    # The supervisor's per-call owner: remember, replace the shared owner, forget. Each of
    # these leaves the union of values as it was, so none may recompile the matcher.
    redact.remember_known_values("background-jobs", ENV)
    before = redact._known_matcher
    redact.remember_known_values("background-jobs:call", ENV)
    redact.replace_known_values("background-jobs", [ENV])
    redact.replace_known_values("background-jobs", [])  # the call owner still holds ENV
    assert redact._known_matcher is before
    redact.forget_known_values("background-jobs:call")
    assert redact._known_matcher is None


def test_stream_learns_at_most_a_few_ids_of_a_kind() -> None:
    from clauster.routes import websockets as ws_module

    learned: dict[str, set[str]] = {}
    lines = "".join(
        f"[bridge:api] environment_id=env_01{i:04d}Zq9Wx7Vc5Bn3Mk1Lj\n" for i in range(20)
    )
    ws_module._learn_stream_ids("iid-1", lines, learned)
    assert len(learned["env"]) == ws_module._STREAM_IDS_PER_KIND
    assert len(redact._known_owners["iid-1"]) == ws_module._STREAM_IDS_PER_KIND


def test_a_forget_that_changes_no_value_keeps_the_matcher() -> None:
    redact.remember_known_values("a", ENV)
    redact.remember_known_values("b", ENV)
    before = redact._known_matcher
    redact.forget_known_values("b")
    assert redact._known_matcher is before  # no rebuild: "a" still holds every value


def test_stream_id_pattern_is_linear_on_crafted_lines() -> None:
    from clauster.routes import websockets as ws_module

    learned: dict[str, set[str]] = {}
    for line in ("[bridge:init] " * 20_000, "Unarchive " * 25_000, "environment_id=" * 15_000):
        start = time.perf_counter()
        ws_module._learn_stream_ids("iid-1", line, learned)
        assert time.perf_counter() - start < 1.0
    assert learned == {}
    # Dense untagged hits after a first id: each one used to rescan the line for a tag.
    dense = f"environment_id={ENV} " + "".join(
        f"[b] environment_id=env_{i} " for i in range(11_000)
    )
    start = time.perf_counter()
    ws_module._learn_stream_ids("iid-1", dense, learned)
    assert time.perf_counter() - start < 1.0
    assert learned["env"] == {ENV}


def test_stream_ignores_a_tag_after_the_id() -> None:
    from clauster.routes import websockets as ws_module

    env2 = "env_01Tq4Wr6Ey8Uz2Ix9Oc3Pv5Ab7"
    learned: dict[str, set[str]] = {}
    ws_module._learn_stream_ids("iid-1", f"[bridge:api] environment_id={ENV}", learned)
    ws_module._learn_stream_ids("iid-1", f"echo environment_id={env2} [bridge:api]", learned)
    assert learned["env"] == {ENV}


def test_stream_learns_a_resumed_session_from_a_remote_bridge_line() -> None:
    from clauster.routes import websockets as ws_module

    resumed = "session_01Pz8Ox6Ny4Mw2Lv9Ku7Jt5Is3"
    learned: dict[str, set[str]] = {}
    ws_module._learn_stream_ids(
        "iid-1", f"[bridge:init] Created initial session {SESSION}", learned
    )
    ws_module._learn_stream_ids("iid-1", f"t [remote-bridge] Unarchive {resumed}", learned)
    assert learned["session"] == {SESSION, resumed}


def test_log_head_cut_at_the_limit_drops_the_partial_line(tmp_path: Path, monkeypatch) -> None:
    from clauster.routes import websockets as ws_module

    monkeypatch.setattr(ws_module, "_LOG_HEAD_BYTES", 32)
    log = tmp_path / "x.log"
    log.write_bytes(b"first line\nenvironment_id=env_01Xk3PqR8sTvW2yZ4bNc6dFg\n")
    assert ws_module._read_log_head(log) == "first line\n"


def test_ws_bridge_log_sends_every_line_in_order_across_slices(
    runner_config, tmp_path: Path, monkeypatch
) -> None:
    from clauster.routes import websockets as ws_module

    monkeypatch.setattr(ws_module, "_SANITIZE_SLICE", 2)
    log = tmp_path / "alpha.log"
    _append(log, "")
    instance = _bare_instance(log)
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/bridge-log/alpha") as ws:
            # Seven lines (an odd count), the last one a terminator: a slice that drops a line
            # ends the read early, rather than leaving the test waiting for a line forever.
            _append(log, "".join(f"line {i}\n" for i in range(6)) + "end\n")
            got: list[str] = []
            while (line := ws.receive_text()) != "end":
                got.append(line)
            assert got == [f"line {i}" for i in range(6)]


def test_log_head_of_an_unreadable_file_is_empty(tmp_path: Path) -> None:
    from clauster.routes import websockets as ws_module

    assert ws_module._read_log_head(tmp_path / "missing.log") == ""


def test_ws_pty_screen_masks_a_learned_value(runner_config, tmp_path: Path) -> None:
    log = tmp_path / "alpha-1-1.log"
    instance = RemoteControlInstance(
        project="alpha",
        label="alpha",
        status=InstanceStatus.RUNNING,
        bridge_debug_log_path=log,
        resume_mode="pty",
    )
    instance.environment_id = ENV  # learned after construction, as the poll loop does
    rows = ["x" * 110 + ENV[:10], ENV[10:].ljust(120)]
    _write_sidecar(pty_screen.screen_sidecar_path(log), screen={"rows": rows})
    with _client(runner_config, instance) as client:
        with client.websocket_connect("/ws/pty-screen/alpha") as ws:
            frame = ws.receive_json()
    assert not _leaks(_screen_text(frame["screen"]["rows"]), ENV)


# ----- the store --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        42,
        "env_01short",  # too short a random part to be a real id
        "env_01Xk3PqR8sTvW2yZ 4bNc6dFg",  # a character no id holds
        "env_01TESTENVAAAAAAAAAAAAAAAA",  # repeats a 7-character run: not random
        "<redacted>xxxxxxxxxxxxxxxx",
    ],
)
def test_store_skips_what_is_not_a_value(value) -> None:
    redact.remember_known_values("iid-1", value)
    assert "iid-1" not in redact._known_owners
    assert redact._known_matcher is None


def test_store_pairs_a_session_id_with_its_code_session_id() -> None:
    redact.remember_known_values("a", CSE)
    assert redact._known_owners["a"] == {SESSION, CSE}


def test_store_replace_and_forget() -> None:
    redact.remember_known_values("a", ENV)
    redact.remember_known_values("b", SESSION)
    redact.replace_known_values("b", [BRIDGE, None])
    assert redact._known_owners["b"] == {BRIDGE}
    redact.replace_known_values("b", [BRIDGE])  # unchanged: no rebuild needed
    redact.replace_known_values("b", [])
    assert "b" not in redact._known_owners
    redact.forget_known_values("a")
    redact.forget_known_values("never-stored")
    assert redact._known_matcher is None
    assert redact.sanitize_line(f"xyz{ENV}") == f"xyz{ENV}"  # the shape residue is back


def test_model_stores_ids_on_build_and_on_assignment() -> None:
    inst = RemoteControlInstance(project="p", label="p", environment_id=ENV)
    assert redact._known_owners[inst.instance_id] == {ENV}
    inst.starter_session_id = SESSION
    inst.bridge_id = BRIDGE
    inst.label = "renamed"  # not an id field: no change
    assert redact._known_owners[inst.instance_id] == {ENV, SESSION, CSE, BRIDGE}


def test_model_stores_an_id_before_the_record_shows_it(monkeypatch) -> None:
    inst = RemoteControlInstance(project="p", label="p")
    seen: list[object] = []
    original = redact.remember_known_values

    def _spy(owner, *values):
        seen.append(inst.environment_id)  # what a reader of the record sees at this moment
        original(owner, *values)

    monkeypatch.setattr(redact, "remember_known_values", _spy)
    inst.environment_id = ENV
    assert seen == [None]  # stored while the record did not show the id yet
    assert ENV in redact._known_owners[inst.instance_id]


def test_background_job_listing_survives_a_parallel_listing(tmp_path: Path, monkeypatch) -> None:
    """A parallel listing that read an older directory must not drop this listing's ids."""
    jobs = tmp_path / "jobs"
    (jobs / "aaaa1111").mkdir(parents=True)
    state = {"state": "working", "detail": f"door at xyz{CSE}", "bridgeSessionId": CSE}
    (jobs / "aaaa1111" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    original = supervisor._job_from_state

    def _interleaved(job_id, data, workers):
        redact.replace_known_values("background-jobs", [])  # the other listing saw no job
        return original(job_id, data, workers)

    monkeypatch.setattr(supervisor, "_job_from_state", _interleaved)
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "roster.json")
    assert not _leaks(job.detail, CSE)
    assert not any(owner.startswith("background-jobs:") for owner in redact._known_owners)


async def test_forget_drops_the_instance_ids(runner_config, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ready")
    config, claude_json = runner_config
    runner = SessionRunner(config, claude_json=claude_json)
    inst = await runner.spawn("alpha")
    await runner.stop(inst.instance_id)
    inst.environment_id = ENV  # the fake bridge's own ids repeat a run, so are not stored
    assert inst.instance_id in redact._known_owners
    await runner.forget(inst.instance_id)
    assert inst.instance_id not in redact._known_owners


def test_background_job_listing_stores_the_ids_it_lists(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    (jobs / "aaaa1111").mkdir(parents=True)
    state = {"state": "working", "detail": f"door at xyz{CSE}", "bridgeSessionId": CSE}
    (jobs / "aaaa1111" / "state.json").write_text(json.dumps(state), encoding="utf-8")
    [job] = supervisor.list_background_jobs(jobs, tmp_path / "roster.json")
    assert not _leaks(job.detail, CSE)  # the job's own text was redacted AFTER the store
    assert redact._known_owners["background-jobs"] == {SESSION, CSE}
    (jobs / "aaaa1111" / "state.json").unlink()
    assert supervisor.list_background_jobs(jobs, tmp_path / "roster.json") == []
    assert "background-jobs" not in redact._known_owners  # a gone job takes its id with it


# ----- cost -------------------------------------------------------------------------------


def test_cost_is_linear_on_hostile_text() -> None:
    """Long runs of anchor hits and of the value itself must stay cheap (no quadratic walk)."""
    redact.remember_known_values("iid-1", ENV, SESSION, BRIDGE)
    blocks = "".join(_random_part(ENV)[i : i + 4] for i in range(0, 20, 4))
    body = _random_part(ENV)
    split_copies = ("env_" + body[:10] + " " + body[10:] + " x ") * 70_000  # 2.2 MB
    for text in (
        blocks * 40_000,
        (ENV + " ") * 20_000,
        ("A" * 1000 + "\n") * 500,
        split_copies,  # each copy's runs used to send `find` to the end of the text
    ):
        start = time.perf_counter()
        spans = redact._known_spans(text)
        assert time.perf_counter() - start < 5.0
        assert isinstance(spans, list)
