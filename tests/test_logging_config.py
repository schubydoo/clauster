"""Logging config honoring log_format, with redaction in both modes (#361)."""

from __future__ import annotations

import json
import logging
from typing import get_args

import pytest

from clauster.logging_config import (
    LOG_LEVELS,
    JsonFormatter,
    LogLevel,
    RedactingTextFormatter,
    setup_logging,
)

# A secret-shaped payload the redact passes must strip in either mode.
_SECRET = "session_01ABCDEFGHIJKLMNOPQRSTUV"


def _record(msg: str, *args, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="clauster.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def test_json_format_is_parseable_with_expected_fields():
    out = JsonFormatter().format(_record("hello %s", "world"))
    obj = json.loads(out)  # one parseable JSON object
    assert obj["level"] == "INFO"
    assert obj["logger"] == "clauster.test"
    assert obj["message"] == "hello world"  # args substituted
    assert "time" in obj


def test_text_format_is_human_single_line():
    out = RedactingTextFormatter("%(levelname)s %(name)s: %(message)s").format(_record("hi"))
    assert out == "INFO clauster.test: hi"


def test_redaction_holds_in_json_mode():
    obj = json.loads(JsonFormatter().format(_record("got %s here", _SECRET)))
    assert _SECRET not in obj["message"]
    assert "session_<redacted>" in obj["message"]


def test_redaction_holds_in_text_mode():
    out = RedactingTextFormatter("%(message)s").format(_record("got %s here", _SECRET))
    assert _SECRET not in out
    assert "session_<redacted>" in out


def test_json_redacts_exception_traceback():
    try:
        raise ValueError(f"boom with {_SECRET}")
    except ValueError:
        import sys

        rec = _record("failed", exc_info=sys.exc_info())
    obj = json.loads(JsonFormatter().format(rec))
    assert _SECRET not in obj["exc_info"]
    assert "<redacted>" in obj["exc_info"]


def test_text_redacts_exception_traceback():
    try:
        raise ValueError(f"boom with {_SECRET}")
    except ValueError:
        import sys

        rec = _record("failed", exc_info=sys.exc_info())
    out = RedactingTextFormatter("%(message)s").format(rec)
    assert _SECRET not in out
    assert "<redacted>" in out


def test_redaction_strips_ansi_before_matching():
    # An ANSI escape inside an id must not split it past the \b-anchored regex.
    ansi_secret = "session_01ABCDEF\x1b[0mGHIJKLMNOPQRSTUV"
    out = RedactingTextFormatter("%(message)s").format(_record("got %s", ansi_secret))
    assert "GHIJKLMNOPQRSTUV" not in out  # the tail wasn't smuggled through


def test_json_redacts_stack_info():
    rec = _record("with stack")
    rec.stack_info = f"Stack (most recent call last):\n  carrying {_SECRET}"
    obj = json.loads(JsonFormatter().format(rec))
    assert _SECRET not in obj["stack_info"]
    assert "<redacted>" in obj["stack_info"]


def test_json_redacts_logger_name():
    rec = _record("x")
    rec.name = f"clauster.{_SECRET}"  # defensive: a dynamic logger name is still redacted
    obj = json.loads(JsonFormatter().format(rec))
    assert _SECRET not in obj["logger"]


def test_setup_logging_selects_formatter_and_is_idempotent():
    try:
        setup_logging("json")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        setup_logging("text")  # re-run must not stack handlers
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, RedactingTextFormatter)
    finally:
        logging.getLogger().handlers[:] = []  # don't leak config into other tests


def test_setup_logging_defaults_to_info():
    root = logging.getLogger()
    previous = root.level
    try:
        setup_logging("text")
        assert root.level == logging.INFO
    finally:
        root.handlers[:] = []
        root.setLevel(previous)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("warning", logging.WARNING),
        ("error", logging.ERROR),
        (logging.DEBUG, logging.DEBUG),  # a raw stdlib int still works
    ],
)
def test_setup_logging_applies_level(level, expected):
    # #993: the level is what `log_level` sets; every LogLevel name must map to its
    # stdlib level, and the int form stays accepted for direct callers.
    root = logging.getLogger()
    previous = root.level
    try:
        setup_logging("text", level=level)
        assert root.level == expected
    finally:
        root.handlers[:] = []
        root.setLevel(previous)


def test_log_levels_names_match_the_literal():
    # The mapping and the Literal are two lists that must not drift: a name in one and
    # not the other is either an unreachable entry or a KeyError at startup.
    assert set(LOG_LEVELS) == set(get_args(LogLevel))


def test_debug_records_are_emitted_and_still_redacted(capsys):
    # The point of the knob (#993) — a DEBUG record reaches the handler — plus the
    # invariant it must not weaken: raising verbosity does not bypass redaction.
    root = logging.getLogger()
    previous = root.level
    try:
        setup_logging("text", level="debug")
        logging.getLogger("clauster.test").debug("token=%s", _SECRET)
        err = capsys.readouterr().err
        assert "DEBUG" in err
        assert _SECRET not in err
    finally:
        root.handlers[:] = []
        root.setLevel(previous)
