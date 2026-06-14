"""Incremental bridge-log reader for the WebSocket tail (feature 6).

Byte-offset based so it survives partial writes, and resets when the file is
rotated/truncated (size shrinks below the last offset). Pure/synchronous — the
app layer drives it off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_TAIL_BYTES = 64 * 1024


def _incomplete_tail_len(data: bytes) -> int:
    """Return how many trailing bytes form a truncated (incomplete) UTF-8 sequence.

    A multibyte character can be split across a read boundary when the writer flushes
    mid-character. Decoding the partial bytes with ``errors="replace"`` would corrupt
    them into ``�`` on both sides, so the caller withholds these bytes and re-reads them
    once the rest arrives. Returns 0 when ``data`` ends on a complete code-point boundary,
    or when the trailing bytes are *invalid* rather than merely incomplete (those decode
    to the replacement char so the offset advances instead of stalling forever). At most 3
    bytes are ever withheld — the longest possible truncation of a 4-byte sequence.
    """
    n = len(data)
    # Walk back over up to 3 continuation bytes (10xxxxxx) to reach the lead byte.
    i = n - 1
    while i >= 0 and (n - 1 - i) < 3 and (data[i] & 0xC0) == 0x80:
        i -= 1
    if i < 0:
        return 0  # empty, or only continuation bytes in reach → nothing valid to withhold
    lead = data[i]
    if lead < 0x80:
        expected = 1
    elif 0xC2 <= lead <= 0xDF:  # 0xC0/0xC1 are overlong → invalid leads, never incomplete
        expected = 2
    elif 0xE0 <= lead <= 0xEF:
        expected = 3
    elif 0xF0 <= lead <= 0xF4:  # 0xF5–0xF7 encode > U+10FFFF → invalid leads
        expected = 4
    else:
        return 0  # stray continuation / invalid lead → not incomplete, just bad
    have = n - i
    if have >= 2:
        # Certain leads have a constrained second byte; an out-of-range one makes the prefix
        # already-invalid (overlong / surrogate / > U+10FFFF), never merely incomplete — so it
        # must be replaced + the offset advanced, not withheld (which would wedge at EOF).
        second = data[i + 1]
        if lead == 0xE0 and not (0xA0 <= second <= 0xBF):
            return 0  # overlong 3-byte
        if lead == 0xED and not (0x80 <= second <= 0x9F):
            return 0  # UTF-16 surrogate (U+D800–DFFF)
        if lead == 0xF0 and not (0x90 <= second <= 0xBF):
            return 0  # overlong 4-byte
        if lead == 0xF4 and not (0x80 <= second <= 0x8F):
            return 0  # > U+10FFFF
    return have if have < expected else 0


def initial_offset(path: Path, tail_bytes: int = DEFAULT_TAIL_BYTES) -> int:
    """Start near the end on a line boundary, so the first emitted line is whole.

    Tailing from ``size - tail_bytes`` lands mid-line, so the first thing the reader
    would emit is the trailing fragment of the line that straddled that offset — and a
    fragment can split a secret/id apart from its redaction context. Advance to just
    after the next newline in the window so the first emitted line is whole. A window
    with no newline at all (a single line longer than ``tail_bytes`` — pathological for
    a debug log) has no whole line to show, so tail from the very end.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    start = max(0, size - tail_bytes)
    if start == 0:
        return 0  # the whole file fits in the window; its first line is already whole
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            window = fh.read(tail_bytes)
    except OSError:
        return start
    newline = window.find(b"\n")
    if newline == -1:
        # No whole line in the window — tail from the very end. This deliberately drops
        # the in-flight oversized line (incl. any secret on it), so the first thing later
        # emitted is only the post-EOF remainder, never the giant fragment seen at connect.
        return size
    return start + newline + 1


def read_new(path: Path, offset: int) -> tuple[int, str]:
    """Return ``(new_offset, text)`` for bytes appended since ``offset``.

    Resets to 0 if the file shrank (rotation/truncation). ``text`` is "" when
    there is nothing new or the file is gone.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return offset, ""
    if size < offset:  # rotated/truncated -> start over
        offset = 0
    if size <= offset:
        return offset, ""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, ""
    # Withhold a trailing byte sequence truncated mid-character; advance the offset only
    # by the bytes actually decoded so the tail is re-read and completed next call.
    hold = _incomplete_tail_len(data)
    if hold:
        data = data[: len(data) - hold]
    return offset + len(data), data.decode("utf-8", "replace")
