from __future__ import annotations

from pathlib import Path

from clauster import logstream


def test_read_new_appends(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("a\nb\n")
    off, text = logstream.read_new(p, 0)
    assert text == "a\nb\n" and off == 4
    p.write_text("a\nb\nc\n")
    off2, text2 = logstream.read_new(p, off)
    assert text2 == "c\n" and off2 == 6


def test_read_new_nothing_new(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("a\n")
    off, _ = logstream.read_new(p, 0)
    assert logstream.read_new(p, off) == (off, "")


def test_read_new_resets_on_truncation(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("longcontent\n")
    off, _ = logstream.read_new(p, 0)
    p.write_text("x\n")  # rotated -> smaller than last offset
    off2, text = logstream.read_new(p, off)
    assert text == "x\n" and off2 == 2


def test_read_new_missing_file(tmp_path: Path):
    assert logstream.read_new(tmp_path / "nope.log", 0) == (0, "")


def test_initial_offset_small_file_is_zero(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_text("hi\n")
    assert logstream.initial_offset(p) == 0


def test_initial_offset_tails_large_file(tmp_path: Path):
    p = tmp_path / "l.log"
    p.write_bytes(b"x" * 100_000)
    assert logstream.initial_offset(p, tail_bytes=1000) == 99_000
