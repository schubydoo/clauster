"""Atheris fuzz harness for ``redact.sanitize_line``.

``sanitize_line`` runs ANSI-stripping + ID/secret redaction regexes over
untrusted bridge-log lines before they're streamed to the browser. It must never
raise or hang (catastrophic-backtracking / ReDoS) on adversarial input, and the
output must not still contain a bare ``env_``/``session_``/``cse_`` id — a
redaction leak. Both are checked: exceptions/hangs surface as fuzzer crashes, and
the leak property is asserted.
"""

import re
import sys

import atheris

with atheris.instrument_imports():
    from clauster import redact

# A surviving bare id in redacted output is a leak. Mirror the families redact_ids
# masks (env_/session_/cse_ ULIDs); the assertion only fires if one slips through.
_LEAK = re.compile(r"\b(?:env|session|cse)_[A-Za-z0-9]{8,}")


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    line = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    out = redact.sanitize_line(line)
    assert not _LEAK.search(out), f"redaction leak: {out!r}"


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
