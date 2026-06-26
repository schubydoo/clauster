"""Server-side terminal emulation for the read-only live pty-screen view (#534).

The pty keeper feeds the SAME raw byte chunks it already reads off the PTY master into a
:class:`PtyScreen`, which renders them through a ``pyte`` Screen and emits *redacted,
cells-only* frames (plaintext rows + cursor) for the ``/ws/pty-screen`` WebSocket. The
browser reconstructs the screen from those cells via the xterm.js API — the raw ANSI is
never sent, so OSC title/hyperlink/clipboard sequences can't re-leak through the client.

Two constraints, locked for the v1 read-only view:

- **Cells, never raw ANSI.** :meth:`PtyScreen.frame` returns rendered text + cursor only.
- **The terminal title is never serialized.** A frame carries no ``title`` / ``icon_name``
  (OSC 0/1/2 are a data-exfiltration channel); pyte may track ``screen.title`` internally
  but it never leaves this module.

``pyte`` is an OPTIONAL dependency (the ``pty`` extra). It is LGPL-licensed, so it is kept
out of the default install and the Apache-licensed standalone binary; it is imported
lazily here so importing this module — or running the app without the extra — never fails.
"""

from __future__ import annotations

from typing import Any

from .redact import redact_screen_text

# Fixed v1 geometry. The keeper renders at this size and the client matches it; a
# resize/negotiation path is out of scope for the read-only first cut (#534).
SCREEN_COLS = 120
SCREEN_ROWS = 40


class PyteUnavailableError(RuntimeError):
    """Raised when the live pty-screen view is used without the optional ``pty`` extra.

    ``pyte`` (LGPL) is optional and not bundled in the default install/binary; install
    ``clauster[pty]`` to enable the live terminal view.
    """


def _import_pyte() -> Any:
    """Import ``pyte`` lazily, raising a clear error when the ``pty`` extra is absent."""
    try:
        import pyte
    except ImportError as exc:
        raise PyteUnavailableError(
            "the live pty-screen view needs the optional 'pyte' dependency; install clauster[pty]"
        ) from exc
    return pyte


class PtyScreen:
    """A pyte-backed terminal emulator that renders raw pty bytes into redacted cells.

    Pure (no I/O): :meth:`feed` consumes raw byte chunks and :meth:`frame` returns the
    current screen as a redacted, cells-only snapshot. Lazily imports ``pyte`` so the
    module is importable without the optional ``pty`` extra.
    """

    def __init__(self, cols: int = SCREEN_COLS, rows: int = SCREEN_ROWS) -> None:
        """Build the emulator at a fixed ``cols`` x ``rows`` geometry (raises if no pyte)."""
        pyte = _import_pyte()
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.ByteStream(self._screen)

    def feed(self, data: bytes) -> None:
        """Feed a chunk of raw pty bytes into the emulator (escape sequences consumed here)."""
        self._stream.feed(data)

    def frame(self) -> dict[str, Any]:
        """Return the current screen as a redacted, cells-only frame.

        Shape: ``{"rows": [<redacted line>, ...], "cursor": {"x": int, "y": int},
        "cols": int, "rows_count": int}``. No raw ANSI and no terminal title ever appear:
        the rows are pyte-rendered plaintext run through :func:`redact_screen_text`.
        """
        cursor = self._screen.cursor
        return {
            "rows": redact_screen_text(list(self._screen.display)),
            "cursor": {"x": cursor.x, "y": cursor.y},
            "cols": self.cols,
            "rows_count": self.rows,
        }
