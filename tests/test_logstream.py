from __future__ import annotations

from pathlib import Path

from clauster import logstream


def test_read_new_appends(tmp_path: Path):
    p = tmp_path / "l.log"
    # write_bytes (not write_text): logstream reads raw bytes, so the fixture must
    # not let text-mode newline translation turn "\n" into "\r\n" on Windows.
    p.write_bytes(b"a\nb\n")
    off, text = logstream.read_new(p, 0)
    assert text == "a\nb\n" and off == 4
    p.write_bytes(b"a\nb\nc\n")
    off2, text2 = logstream.read_new(p, off)
    assert text2 == "c\n" and off2 == 6


def test_read_new_nothing_new(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("a\n")
    off, _ = logstream.read_new(p, 0)
    assert logstream.read_new(p, off) == (off, "")


def test_read_new_resets_on_truncation(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_bytes(b"longcontent\n")  # bytes: avoid Windows "\n"->"\r\n" translation
    off, _ = logstream.read_new(p, 0)
    p.write_bytes(b"x\n")  # rotated -> smaller than last offset
    off2, text = logstream.read_new(p, off)
    assert text == "x\n" and off2 == 2


def test_read_new_missing_file(tmp_path: Path):
    assert logstream.read_new(tmp_path / "nope.log", 0) == (0, "")


def test_initial_offset_small_file_is_zero(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("hi\n")
    assert logstream.initial_offset(p) == 0


def test_initial_offset_aligns_to_next_newline(tmp_path: Path):
    # The tail window starts mid-line; the offset advances to the next newline so the
    # first emitted line is whole (never the trailing fragment of a straddled line).
    p = tmp_path / "l.log"
    p.write_bytes(b"a" * 980 + b"\n" + b"second line\n")  # size 993; newline at 980
    off = logstream.initial_offset(p, tail_bytes=20)  # start=973 (mid first line)
    assert off == 981  # just past the newline at 980 — start of "second line"
    assert logstream.read_new(p, off) == (993, "second line\n")  # whole lines only


def test_initial_offset_no_newline_in_window_tails_from_end(tmp_path: Path):
    # A window with no newline (a single line longer than tail_bytes — pathological for a
    # debug log) has no whole line to show, so tail from the very end rather than emit a
    # giant partial fragment.
    p = tmp_path / "l.log"
    p.write_bytes(b"x" * 100_000)
    assert logstream.initial_offset(p, tail_bytes=1000) == 100_000


def test_initial_offset_missing_file_is_zero(tmp_path: Path):
    assert logstream.initial_offset(tmp_path / "nope.log") == 0


def test_initial_offset_open_error_falls_back_to_window_start(tmp_path: Path, monkeypatch):
    # stat() succeeds but open() fails (perm/race) -> fall back to the raw window start;
    # the call site's carry buffer re-aligns whole lines anyway.
    p = tmp_path / "l.log"
    p.write_bytes(b"a" * 980 + b"\nsecond\n")  # size 988, larger than the tail window
    real_open = open

    def boom(path, *a, **k):
        if str(path) == str(p):
            raise OSError("open failed")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", boom)
    assert logstream.initial_offset(p, tail_bytes=20) == 968  # start = 988 - 20


def test_read_new_open_error_returns_empty(tmp_path: Path, monkeypatch):
    # stat() succeeds but open() fails (file vanishes / perm) -> graceful ("", offset).
    p = tmp_path / "l.log"
    p.write_text("hello world\n")
    real_open = open

    def boom(path, *a, **k):
        if str(path) == str(p):
            raise OSError("vanished")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", boom)
    assert logstream.read_new(p, 0) == (0, "")


def test_read_new_reassembles_split_4byte_char(tmp_path: Path):
    # A 4-byte char (🚀 = f0 9f 9a 80) flushed mid-character must not corrupt: the
    # incomplete tail is withheld (offset does not advance past it) and completes on the
    # next read. Without the hold-back, each side decodes to � and the char is lost.
    p = tmp_path / "l.log"
    rocket = "🚀".encode()
    p.write_bytes(rocket[:2])  # only the first 2 of 4 bytes flushed
    off, text = logstream.read_new(p, 0)
    assert text == ""  # nothing decodable yet
    assert off == 0  # did NOT advance past the truncated char
    p.write_bytes(rocket)  # the rest arrives
    off2, text2 = logstream.read_new(p, off)
    assert text2 == "🚀"  # reassembled intact
    assert off2 == len(rocket)


def test_read_new_reassembles_split_2byte_char(tmp_path: Path):
    p = tmp_path / "l.log"
    data = "wörld\n".encode()  # ö = c3 b6
    cut = data.index(b"\xc3") + 1  # split between the two bytes of ö
    p.write_bytes(data[:cut])
    off, text = logstream.read_new(p, 0)
    assert "�" not in text  # the half-written ö is withheld, not corrupted
    p.write_bytes(data)
    off2, text2 = logstream.read_new(p, off)
    assert text + text2 == "wörld\n"
    assert off2 == len(data)


def test_read_new_invalid_trailing_byte_does_not_wedge(tmp_path: Path):
    # An invalid byte (0xFF can never start a UTF-8 sequence) must be replaced and the
    # offset must advance — holding it back would stall the tail forever.
    p = tmp_path / "l.log"
    p.write_bytes(b"ok\xff")
    off, text = logstream.read_new(p, 0)
    assert off == 3  # advanced past all bytes, no infinite hold
    assert text == "ok�"  # invalid byte became the replacement char


def test_read_new_reassembles_split_3byte_char(tmp_path: Path):
    # 3-byte char (€ = e2 82 ac) split across reads — exercises the 3-byte lead path.
    p = tmp_path / "l.log"
    euro = "€".encode()
    p.write_bytes(euro[:1])
    off, text = logstream.read_new(p, 0)
    assert text == "" and off == 0
    p.write_bytes(euro)
    off2, text2 = logstream.read_new(p, off)
    assert text2 == "€" and off2 == len(euro)


def test_read_new_only_continuation_bytes_are_replaced(tmp_path: Path):
    # Bytes that are all UTF-8 continuations (10xxxxxx) with no lead can never complete;
    # they must be replaced and the offset advance, not be withheld (the i<0 branch).
    p = tmp_path / "l.log"
    p.write_bytes(b"\x80\x80\x80")
    off, text = logstream.read_new(p, 0)
    assert off == 3
    assert text == "���"


def test_read_new_invalid_lead_bytes_are_replaced_not_wedged(tmp_path: Path):
    # 0xC0/0xC1 (overlong) and 0xF5–0xF7 (> U+10FFFF) are INVALID UTF-8 leads — they can
    # never complete, so they must be replaced and the offset advance, never withheld.
    for bad in (b"\xc0", b"\xc1", b"\xf5", b"\xf6", b"\xf7"):
        p = tmp_path / "l.log"
        p.write_bytes(b"x" + bad)
        off, text = logstream.read_new(p, 0)
        assert off == 2, f"offset wedged on {bad!r}"
        assert text == "x�"


def test_read_new_invalid_2byte_prefixes_do_not_wedge(tmp_path: Path):
    # Leads with constrained second bytes: an out-of-range second makes the prefix ALREADY
    # invalid (E0/F0 overlong, ED surrogate, F4 > U+10FFFF), so it must be replaced and the
    # offset advance, never withheld (which would wedge the tail at EOF).
    for prefix in (b"\xe0\x80", b"\xed\xa0", b"\xf0\x80", b"\xf4\x90"):
        p = tmp_path / "l.log"
        p.write_bytes(b"x" + prefix)
        off, text = logstream.read_new(p, 0)
        assert off == 3, f"wedged on {prefix!r}"
        assert text.startswith("x") and "�" in text


def test_read_new_valid_2byte_prefix_is_withheld(tmp_path: Path):
    # A VALID truncated 3-byte prefix (E0 A0, second in range) is still withheld for completion.
    p = tmp_path / "l.log"
    p.write_bytes(b"x\xe0\xa0")
    off, text = logstream.read_new(p, 0)
    assert text == "x"  # only the complete leading "x" is emitted; the 3-byte char waits
    assert off == 1
