"""Atheris fuzz harness for ``inspector.parse_agents_json``.

``parse_agents_json`` parses the raw stdout of the external ``claude agents --json``
subprocess — third-party CLI output whose shape is Anthropic-controlled and
version-dependent, i.e. genuinely external bytes. It stays strict on malformed
JSON (raises ``json.JSONDecodeError``, its documented fail-closed contract — caught
here); the per-item ``KeyError``/``TypeError``/``ValueError`` are swallowed inside
the loop, so any OTHER exception that escapes (a deep-nesting ``RecursionError``,
an uncaught type) is a real bug.
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
    except json.JSONDecodeError:
        # The documented strict-parse contract on a malformed payload — expected,
        # not a bug. Anything else escaping is a finding.
        pass


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
