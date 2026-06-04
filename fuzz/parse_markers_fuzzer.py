"""Atheris fuzz harness for ``bridge_log.parse_bridge_markers``.

The bridge debug log is raw, attacker-influenceable program output that the
runner parses with regexes to decide a bridge's state (ready / env id / shutdown
/ trust-error). The parser must tolerate *any* input without raising — so any
exception this harness surfaces is a real bug (a sibling of the uncaught
``UnicodeDecodeError`` fixed in #122).
"""

import sys

import atheris

with atheris.instrument_imports():
    from clauster import bridge_log


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    text = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    bridge_log.parse_bridge_markers(text)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
