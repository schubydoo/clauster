"""Atheris fuzz harness for ``usage._line_to_turn``.

``_line_to_turn`` turns one line of a Claude transcript JSONL into the redacted,
render-ready turn the browser is shown. The transcript is written by ``claude``, so
every byte in it originated in a model response or a tool's output — attacker-
influenceable text on a path whose whole job is to be safe to render.

Its docstring makes two promises, and this harness holds it to both:

1. **"Never raises on a malformed line."** Blank, non-JSON, non-dict, missing a
   ``message`` dict, or missing a non-empty ``role`` all return ``None`` instead.
   The harness catches nothing, so any escape is a crash.
2. **Redaction before render (D11).** Every free-text field is passed through
   :func:`redact.sanitize_line`, so no bare ``env_``/``session_``/``cse_`` identifier
   can reach the browser. Asserted the same way ``redact_fuzzer`` asserts it, with the
   matcher restated here rather than imported — a leak the production matcher would
   miss is one this harness must still catch.

Two further properties are checked because they are what the *caller* relies on:

* **Shape** — the returned mapping has exactly ``{role, content, model, timestamp}``,
  with ``role``/``content`` strings and ``model``/``timestamp`` ``str | None``. The
  template renders those fields unconditionally; a stray type is a 500 in the page,
  not in this function.
* **Skip-rule agreement** — ``None`` is returned for exactly the set of lines the
  docstring enumerates, re-derived here from ``json.loads`` rather than from the
  function's own branches. This is the oracle that would catch a *widened* accept path
  (a line the docs say is unrenderable being rendered anyway); ``tests/
  test_fuzz_harness_smoke.py`` pins it against the implementation so a deliberate
  contract change fails ``just check`` rather than surfacing days later as SARIF.

Not asserted: that ``content`` reproduces any particular flattening of the message
blocks. ``_render_content``'s ``[tool_use]``-style placeholders are a deliberate
first-cut product decision (#431), not a contract, and pinning them here would report
the next legitimate change to the viewer as a fuzz crash.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md: `json` is the parser under test on the reject path,
    # and `re` backs the leak matcher. `usage` imports `redact`, so the redaction
    # regexes are instrumented through it.
    import json
    import re

    from clauster import usage

#: Mirrors ``redact._ID_RE`` exactly (``env_``/``session_``/``cse_`` + 6 or more chars).
#: Restated, not imported: an oracle that asks the redactor whether it redacted cannot
#: fail. A 6-character id the production matcher stopped catching must fail here.
_LEAK = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")

#: The exact key set the dashboard's transcript template renders.
_TURN_KEYS = {"role", "content", "model", "timestamp"}

#: The fields the leak property is asserted over — the ones ``_line_to_turn`` actually
#: routes through ``redact.sanitize_line``, which is now every string field it returns.
#:
#: ``timestamp`` was absent until issue 1353: this harness found that the docstring
#: promised "``{role, content, model, timestamp}`` with **every free-text field** passed
#: through :func:`redact.sanitize_line`" while the code returned it verbatim, so a record
#: whose ``timestamp`` carried an ``env_``/``session_``/``cse_`` identifier rendered it to
#: the browser — what invariant 4 and that docstring both say cannot happen. The field is
#: sanitized now, so the leak property covers it too.
_SANITIZED_FIELDS = ("role", "content", "model", "timestamp")


def _reference_is_renderable(line: str) -> bool:
    """Whether the docstring's skip rules say ``line`` is a renderable message.

    Re-derived from the documented rules — blank, not JSON, not a dict, no ``message``
    dict, no non-empty string ``role`` — using ``json.loads`` directly, so it is not a
    restatement of ``_line_to_turn``'s control flow. ``RecursionError`` is caught for the
    same reason the target catches it: CPython's recursive scanner raises it (and it is
    not a ``ValueError``) before ``json`` can report a decode error.
    """
    line = line.strip()
    if not line:
        return False
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(record, dict):
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    role = message.get("role")
    return isinstance(role, str) and bool(role)


def check(line: str) -> None:
    """Assert every property above for one transcript line.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    turn = usage._line_to_turn(line)

    assert (turn is not None) == _reference_is_renderable(line), (
        f"skip rules disagree for {line!r}: got {turn!r}"
    )
    if turn is None:
        return

    assert set(turn) == _TURN_KEYS, f"turn shape changed: {sorted(turn)!r}"
    assert isinstance(turn["role"], str), f"role not a str: {turn['role']!r}"
    assert isinstance(turn["content"], str), f"content not a str: {turn['content']!r}"
    for optional in ("model", "timestamp"):
        assert turn[optional] is None or isinstance(turn[optional], str), (
            f"{optional} not str|None: {turn[optional]!r}"
        )

    for field in _SANITIZED_FIELDS:
        value = turn[field]
        if isinstance(value, str):
            assert not _LEAK.search(value), f"redaction leak in {field}: {value!r}"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # ConsumeBytes + decode, NOT ConsumeUnicodeNoSurrogates — see fuzz/README.md's
    # seed-passthrough trap. The unicode consumer spends the first byte on an internal
    # encoding selector, so a seed file's first character never reaches the target: every
    # `{`-leading JSON seed here would arrive as `"message":…`, unparseable, and the whole
    # corpus would exercise only the reject path. Measured over 37s: 26 edges that way
    # against 65 this way. `errors="replace"` mirrors how a transcript line is read.
    raw = fdp.ConsumeBytes(fdp.remaining_bytes())
    check(raw.decode("utf-8", "replace"))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
