"""Incremental bridge-log reader for the WebSocket tail (feature 6).

Byte-offset based so it survives partial writes, and resets when the file is
rotated/truncated (size shrinks below the last offset). Pure/synchronous — the
app layer drives it off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_TAIL_BYTES = 64 * 1024


def initial_offset(path: Path, tail_bytes: int = DEFAULT_TAIL_BYTES) -> int:
    """Start near the end so we don't replay an entire large log on connect."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    return max(0, size - tail_bytes)


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
    return offset + len(data), data.decode("utf-8", "replace")
