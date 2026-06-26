"""Pure server-side terminal emulation for the read-only live pty-screen view (#534)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from clauster import pty_screen
from clauster.pty_screen import PtyScreen, PyteUnavailableError


def test_renders_plaintext_cells_and_cursor():
    scr = PtyScreen(cols=20, rows=3)
    scr.feed(b"hello \x1b[31mworld\x1b[0m")  # SGR color escapes are consumed by pyte
    frame = scr.frame()
    assert frame["cols"] == 20 and frame["rows_count"] == 3
    assert len(frame["rows"]) == 3
    assert frame["rows"][0].startswith("hello world")
    assert "\x1b" not in frame["rows"][0]  # cells only, never raw ANSI
    assert frame["cursor"] == {"x": 11, "y": 0}


def test_frame_redacts_secrets_and_ids():
    scr = PtyScreen(cols=60, rows=2)
    scr.feed(b"export TOK=sk-abcdef0123456789 session_01ABCDEFGHIJKLMNOP")
    joined = "".join(scr.frame()["rows"])
    assert "sk-abcdef0123456789" not in joined
    assert "session_01ABCDEFGHIJKLMNOP" not in joined
    assert "<redacted>" in joined


def test_title_is_never_serialized():
    # An OSC title sequence must not surface in any frame field — OSC 0/1/2 are a
    # data-exfiltration channel, so the title is rendered by pyte but never emitted.
    scr = PtyScreen(cols=20, rows=2)
    scr.feed(b"\x1b]0;SECRET-TITLE\x07visible")
    frame = scr.frame()
    assert "title" not in frame and "icon_name" not in frame
    assert "SECRET-TITLE" not in json.dumps(frame)
    assert frame["rows"][0].startswith("visible")


def test_feed_is_incremental():
    scr = PtyScreen(cols=10, rows=1)
    scr.feed(b"ab")
    scr.feed(b"cd")
    assert scr.frame()["rows"][0].startswith("abcd")


def test_default_geometry_is_120x40():
    frame = PtyScreen().frame()
    assert frame["cols"] == pty_screen.SCREEN_COLS == 120
    assert frame["rows_count"] == pty_screen.SCREEN_ROWS == 40
    assert len(frame["rows"]) == 40


def test_every_frame_row_is_exactly_cols_wide_after_redaction():
    # Redaction can SHORTEN a row (long secret -> 10-char `<redacted>`) or LENGTHEN it
    # (short id -> `env_<redacted>`); frame() re-fits each row to exactly `cols` so the
    # client's fixed grid never wraps. Truncation only trims the right edge (no span
    # exposed). Both directions are exercised here.
    scr = PtyScreen(cols=24, rows=3)
    scr.feed(b"sk-abcdef0123456789 tail")  # long secret -> row shrinks
    scr.feed(b"\r\nenv_ABCDEF")  # short id -> `env_<redacted>` lengthens past width
    frame = scr.frame()
    assert all(len(row) == 24 for row in frame["rows"])  # exact width, every row
    joined = "".join(frame["rows"])
    assert "sk-abcdef0123456789" not in joined and "env_ABCDEF" not in joined
    assert "<redacted>" in joined


def test_no_control_bytes_ever_reach_rows():
    # Lock the cells-only invariant against a future pyte change: a corpus of adversarial
    # control/escape sequences (7-bit + 8-bit C1 OSC, DCS, APC, CSI, lone ESC, NUL/BEL/DEL)
    # must never land a C0/C1 control byte in a rendered row. pyte owns escape parsing; this
    # pins the property at OUR egress so a pyte upgrade can't silently start leaking raw bytes.
    scr = PtyScreen(cols=30, rows=4)
    corpus = [
        b"\x1b]0;title\x07",  # 7-bit OSC title (BEL-terminated)
        b"\x1b]2;title\x1b\\",  # 7-bit OSC title (ST-terminated)
        b"\x1b]8;;https://evil/\x07",  # OSC 8 hyperlink
        b"\x1b]52;c;c2VjcmV0\x07",  # OSC 52 clipboard
        b"\x9d0;title\x9c",  # 8-bit C1 OSC + ST
        b"\x1bP1;2;3qpayload\x1b\\",  # DCS
        b"\x1b_application\x1b\\",  # APC
        b"\x1b",  # lone ESC
        b"\x00\x07\x7f\x08\x0c",  # NUL BEL DEL BS FF
        b"text\x9bafter",  # 8-bit CSI introducer
    ]
    for chunk in corpus:
        scr.feed(chunk)
    for row in scr.frame()["rows"]:
        for ch in row:
            o = ord(ch)
            assert o >= 0x20 and o != 0x7F and not (0x80 <= o <= 0x9F), (
                f"control byte {o:#x} leaked into a rendered row"
            )


def test_screen_sidecar_path_is_stem_dot_screen_json():
    # The shared naming helper (used by both the keeper-spawn writer and the /ws reader)
    # swaps the log's suffix for `.screen.json`, keyed off the stem so it sits beside its set.
    assert pty_screen.screen_sidecar_path(Path("/logs/alpha-1700000000000-1.log")) == Path(
        "/logs/alpha-1700000000000-1.screen.json"
    )


def test_read_screen_sidecar_roundtrips_a_frame(tmp_path: Path):
    p = tmp_path / "x.screen.json"
    frame = {"seq": 3, "state": "live", "error": None, "screen": {"rows": ["hi"]}}
    p.write_text(json.dumps(frame), encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) == frame


def test_read_screen_sidecar_missing_file_returns_none(tmp_path: Path):
    # The keeper may not have written the sidecar yet — a missing file is a wait, not a fail.
    assert pty_screen.read_screen_sidecar(tmp_path / "nope.screen.json") is None


def test_read_screen_sidecar_malformed_json_returns_none(tmp_path: Path):
    p = tmp_path / "x.screen.json"
    p.write_text("{not json", encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) is None


def test_read_screen_sidecar_non_object_returns_none(tmp_path: Path):
    # A valid-JSON but non-object payload (e.g. a bare list) is rejected like malformed input.
    p = tmp_path / "x.screen.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert pty_screen.read_screen_sidecar(p) is None


def test_missing_pyte_raises_clear_error(monkeypatch):
    # Without the optional `pty` extra, the lazy import fails -> a clear
    # PyteUnavailableError naming the extra, never a bare ImportError. Simulate the
    # absent dependency by poisoning sys.modules so `import pyte` re-raises ImportError.
    monkeypatch.setitem(sys.modules, "pyte", None)
    with pytest.raises(PyteUnavailableError, match=r"clauster\[pty\]"):
        PtyScreen()
