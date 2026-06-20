"""Atheris fuzz harness for ``inspector.parse_agents_json``.

``parse_agents_json`` parses the raw stdout of the external ``claude agents --json``
subprocess — third-party CLI output whose shape is Anthropic-controlled and
version-dependent, i.e. genuinely external bytes. It stays strict on malformed
JSON (raises ``json.JSONDecodeError``, its documented fail-closed contract); the
per-item ``KeyError``/``TypeError``/``ValueError`` are swallowed inside the loop.
``JSONDecodeError`` on bad JSON and ``RecursionError`` from CPython's recursive
scanner on a deeply-nested payload are both expected parse-failures (caught here,
not clauster bugs); any OTHER escaping exception is a real bug.
"""

import json
import sys

import atheris

with atheris.instrument_imports():
    from clauster import inspector


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    text = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    try:
        inspector.parse_agents_json(text)
    except (json.JSONDecodeError, RecursionError):
        # Expected parse-failures on a malformed/pathological payload (the function
        # stays strict by design): JSONDecodeError on bad JSON, RecursionError from
        # the recursive scanner on deeply-nested input. Anything else is a finding.
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
