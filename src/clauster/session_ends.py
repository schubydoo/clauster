"""Find the sessions a standard bridge has ended, from its own debug log (TASK-557).

The bridge pointer keeps naming a bridge's first session after that session ends, so neither
the pointer nor the row can say whether the "Open in Claude" deep link still works. The
bridge log can: a completed session is archived with a request that names it.

The log grows for the life of the bridge, so each file is read once and then only from where
the last read stopped. Standard bridges only: a pty bridge has one session and ends with it.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import bridge_log

_log = logging.getLogger("clauster.session_ends")

_READ_SIZE = 1024 * 1024


@dataclass
class _LogState:
    """What one log has said so far, and the byte offset after its last complete line."""

    offset: int = 0
    archived: set[str] = field(default_factory=set)
    environments: set[str] = field(default_factory=set)


class SessionEndTracker:
    """Remember, per bridge log, which sessions were archived and which environments ran."""

    def __init__(self) -> None:
        """Start with no logs read."""
        self._logs: dict[Path, _LogState] = {}
        self._lock = threading.Lock()

    def has_ended(
        self, log_path: Path | None, session_id: str, environment_id: str | None
    ) -> bool:
        """Return whether a deep link to ``session_id`` can no longer be vouched for.

        ``True`` when the log archived the session, and also when no log can speak for this
        bridge: there is no path, the file is gone, or it never names the bridge's
        environment (the newest log of a project can belong to another bridge there). The
        environment link is always safe, so the doubt goes that way. A transient read error
        returns ``False`` and the next poll tries again.
        """
        if log_path is None:
            return True
        with self._lock:
            state = self._logs.setdefault(log_path, _LogState())
            try:
                self._read_new_lines(log_path, state)
            except FileNotFoundError:
                return True
            except OSError as exc:
                _log.debug("could not read bridge log %s: %s", log_path, exc)
                return False
            if environment_id is not None and environment_id not in state.environments:
                return True
            return bridge_log.session_key(session_id) in state.archived

    def keep_only(self, log_paths: Collection[Path]) -> None:
        """Forget every log not in ``log_paths``, so the map cannot grow without bound."""
        with self._lock:
            for path in self._logs.keys() - set(log_paths):
                del self._logs[path]

    @staticmethod
    def _read_new_lines(log_path: Path, state: _LogState) -> None:
        """Fold the complete lines after ``state.offset`` into ``state``."""
        with log_path.open("rb") as fh:
            if os.fstat(fh.fileno()).st_size < state.offset:
                state.offset = 0  # truncated or replaced: what it already said stays true
            fh.seek(state.offset)
            pending = b""
            while chunk := fh.read(_READ_SIZE):
                data = pending + chunk
                end = data.rfind(b"\n") + 1
                pending = data[end:]  # still being written: read it whole on the next pass
                if not end:
                    continue
                state.offset += end
                complete = data[:end]
                for line in _lines_naming(complete, b"/archive"):
                    key = bridge_log.archived_session_key(line)
                    if key is not None:
                        state.archived.add(key)
                for line in _lines_naming(complete, b"nvironment"):
                    state.environments |= bridge_log.environment_ids_in(line)


def _lines_naming(data: bytes, needle: bytes) -> Iterator[str]:
    """Yield each line of newline-terminated ``data`` that holds ``needle``.

    Searches the bytes first, because almost no line of a debug log needs a decode.
    """
    at = data.find(needle)
    while at >= 0:
        start = data.rfind(b"\n", 0, at) + 1
        stop = data.index(b"\n", at)
        yield data[start:stop].decode("utf-8", "replace")
        at = data.find(needle, stop + 1)
