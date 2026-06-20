"""Atheris fuzz harness for ``auth.normalize_origin`` (the CSRF/CORS origin gate).

``normalize_origin`` parses the raw inbound HTTP ``Origin`` header — ``app.py``'s
``_origin_allowed`` hands it straight in — so it must tolerate ANY string without
raising: a malformed Origin has to normalize to a non-matching value and get
rejected (403), never crash the request (500). This harness found a real bug —
``urlsplit().port`` raised ``ValueError`` on an out-of-range/non-numeric port (the
``validate_clone_url`` ``.port`` class, #122) — so any exception it surfaces is a
genuine bug; it catches nothing.
"""

import sys

import atheris

with atheris.instrument_imports():
    from clauster import auth


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    origin = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    # Contract: total over any string. A malformed Origin yields a non-matching
    # normalized form, not an exception.
    auth.normalize_origin(origin)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
