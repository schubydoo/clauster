"""Application logging setup honoring ``log_format`` (#361) and ``log_level`` (#993).

``text`` (the default) keeps the human-readable single-line format; ``json`` emits
one structured JSON object per record. Both modes **redact** the final output —
``env_``/``session_``/``cse_`` ids, bare UUIDs, and listed token shapes are masked in
either format — by running the same :mod:`clauster.redact` passes the WebSocket log
stream uses; that module's shape allow-list bounds what "token" covers. Configures the
root logger so application *and* propagated server (uvicorn) records share the chosen
format, at the severity :data:`LogLevel` names (``info`` unless ``log_level`` says
otherwise).
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from . import redact


def _redact(text: str) -> str:
    r"""Strip secrets/ids from a log string (the same passes the live stream uses).

    Runs against an ANSI-stripped view first — as ``redact.sanitize_line`` does — so a
    colorised library log can't smuggle a secret past the ``\b``-anchored id/secret
    regexes by splitting an identifier with an escape sequence. Delegates rather than
    repeating those passes inline: stripping can also SPLICE two word characters together
    and destroy the boundary the masks need, and :func:`redact.redact_stripped` is where
    that second view is handled for every egress path at once.
    """
    return redact.redact_stripped(text)


class RedactingTextFormatter(logging.Formatter):
    """Human-readable formatter that redacts the fully-rendered line (message + trace)."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, then redact the whole line so nothing secret survives."""
        return _redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """One redacted JSON object per record: time, level, logger, message (+ exc/stack)."""

    def format(self, record: logging.LogRecord) -> str:
        """Build the structured record, redacting the message and any traceback text."""
        payload: dict[str, object] = {
            "time": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            # Logger names are static module paths today, but redact for symmetry with the
            # text formatter (which redacts the whole line) and to defend a future dynamic
            # logger name.
            "logger": _redact(record.name),
            "message": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = _redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = _redact(self.formatStack(record.stack_info))
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: The configurable severities (``log_level``). Deliberately a subset of the stdlib's:
#: ``critical`` would hide startup/lifecycle errors, and ``notset`` is not a level.
#: The names double as uvicorn's own ``log_level`` values, so one config key sets both.
LogLevel = Literal["debug", "info", "warning", "error"]

LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup_logging(
    log_format: Literal["text", "json"] = "text", *, level: LogLevel | int = logging.INFO
) -> None:
    """Configure the root logger with a single redacting handler for ``log_format``.

    Idempotent: replaces any existing root handlers so a re-run (e.g. tests) doesn't
    stack duplicates. ``json`` selects :class:`JsonFormatter`; anything else uses the
    human text format. Both formatters run the same redaction passes, so neither mode can
    leak an ``env_``/``session_``/``cse_`` id, a bare UUID, or a listed token shape (see
    :mod:`clauster.redact` for what the shape allow-list does *not* catch).

    ``level`` takes either a :data:`LogLevel` name (what ``log_level`` holds) or a raw
    stdlib level int. Redaction is applied by the formatter, so raising the level to
    ``debug`` widens *what* is logged without widening what may leak into a line.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter() if log_format == "json" else RedactingTextFormatter(_TEXT_FORMAT)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(LOG_LEVELS[level] if isinstance(level, str) else level)
