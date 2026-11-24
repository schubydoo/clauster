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
from enum import Enum, auto
from pathlib import Path

from . import bridge_log

_log = logging.getLogger("clauster.session_ends")

_READ_SIZE = 1024 * 1024


class LinkEvidence(Enum):
    """What a bridge log says about a deep link to one session."""

    LIVE = auto()  # the log is this bridge's and has not archived the session
    ARCHIVED = auto()  # proven: the log holds the archive request for the session
    NO_EVIDENCE = auto()  # no log can speak for this bridge, so the link cannot be vouched for
    READ_ERROR = auto()  # a transient failure: keep what is known and try again next poll


@dataclass
class _LogState:
    """What one log has said so far, and the byte offset after its last complete line."""

    offset: int = 0
    environments: set[str] = field(default_factory=set)


class SessionEndTracker:
    """Remember, per bridge log, which sessions were archived and which environments ran."""

    def __init__(self) -> None:
        """Start with no logs read."""
        self._logs: dict[Path, _LogState] = {}
        # Proven archives, by session key. Kept for the life of the process, past the log and
        # the row that showed them: a later Start or Resume can seed the same id from the
        # pointer into a row whose new log says nothing about it. One short key per session.
        self._archived: set[str] = set()
        self._lock = threading.Lock()

    def evidence(
        self, log_path: Path | None, session_id: str, environment_id: str | None
    ) -> LinkEvidence:
        """Return what the log says about a deep link to ``session_id``.

        :attr:`~LinkEvidence.NO_EVIDENCE` covers a missing path, a missing file, and a log
        that never names the bridge's environment (the newest log of a project can belong
        to another bridge there). The environment link is always safe, so doubt goes that
        way. An archive request for this exact session is proof whichever log held it.
        """
        key = bridge_log.session_key(session_id)
        with self._lock:
            state: _LogState | None = None
            read_error = False
            if log_path is not None:
                try:
                    state = self._logs.setdefault(log_path, _LogState())
                    self._read_new_lines(log_path, state)
                except FileNotFoundError:
                    state = None
                except OSError as exc:
                    _log.debug("could not read bridge log %s: %s", log_path, exc)
                    read_error = True
            if key in self._archived:
                return LinkEvidence.ARCHIVED
            if read_error:
                return LinkEvidence.READ_ERROR
            if state is None or (
                environment_id is not None and environment_id not in state.environments
            ):
                return LinkEvidence.NO_EVIDENCE
            return LinkEvidence.LIVE

    def keep_only(self, log_paths: Collection[Path]) -> None:
        """Forget the read state of every log not in ``log_paths``; proven archives stay."""
        with self._lock:
            for path in self._logs.keys() - set(log_paths):
                del self._logs[path]

    def _read_new_lines(self, log_path: Path, state: _LogState) -> None:
        """Fold the complete lines after ``state.offset`` into ``state`` and the proofs."""
        with log_path.open("rb") as fh:
            if os.fstat(fh.fileno()).st_size < state.offset:
                state.offset = 0  # truncated or replaced: what it already said stays true
            fh.seek(state.offset)
            # Pieces of the line in progress. A list, and a search of only the new chunk, keep
            # one very long line linear. A tail with no newline is read again next pass.
            partial: list[bytes] = []
            while chunk := fh.read(_READ_SIZE):
                end = chunk.rfind(b"\n") + 1
                if not end:
                    partial.append(chunk)
                    continue
                complete = b"".join([*partial, chunk[:end]])
                partial = [chunk[end:]]
                state.offset += len(complete)
                for line in _lines_naming(complete, b"/archive"):
                    key = bridge_log.archived_session_key(line)
                    if key is not None:
                        self._archived.add(key)
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
