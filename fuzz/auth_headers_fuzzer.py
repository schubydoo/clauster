"""Atheris fuzz harness for the pre-auth header parsers ``auth.verify_proxy_hmac`` +
``auth.parse_bearer``.

Both read a raw inbound HTTP header **before any authentication has happened**, so
their input is attacker-controlled by definition, and Starlette decodes header bytes
as latin-1 — arbitrary bytes arrive as an arbitrary ``str``. Their sibling on the
same boundary, ``normalize_origin``, is already fuzzed (and that harness found a real
``.port`` ``ValueError``); these two were not.

``verify_proxy_hmac`` parses ``X-Proxy-Auth: t=<unix>,v1=<hex>`` and documents
"Returns False on any malformation rather than raising" — a boolean contract, and a
fail-closed gate (safety invariant 1). It already carries a scar of exactly this bug
class: ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII signature, which
500'd instead of returning False until an ``isascii()`` guard was added. Any exception
escaping here is a genuine bug; the harness catches nothing.

Two directions are exercised per input, because random bytes alone can only ever reach
the REJECT path:

* the fuzzed header string is verified against a fixed secret (must return ``False``
  without raising — a random string cannot forge an HMAC);
* a *correctly signed* header is then built from fuzzed user/method/path/timestamp
  values and must verify ``True``, which walks the whole success path and would catch
  a regression in what the signature commits to.

``parse_bearer`` (the ``Authorization: Bearer <token>`` split) is folded in rather than
given its own harness: same boundary, same input blob, four branches.
"""

import hashlib
import hmac
import sys

import atheris

with atheris.instrument_imports():
    from clauster import auth

# Fixed so the harness is deterministic: the fuzzed bytes are the input under test,
# not the server-side secret.
_SECRET = "fuzz-proxy-secret"  # noqa: S105 - a harness fixture, never a real credential
_NOW = 1_700_000_000
_WINDOW = 300


def _signed_header(user: str, method: str, path: str, t: int) -> str:
    """Build the header ``verify_proxy_hmac`` should accept for these values."""
    msg = f"{user}:{t}:{method}:{path}".encode()
    sig = hmac.new(_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    header = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 512))
    user = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    method = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 16))
    path = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 128))
    skew = fdp.ConsumeIntInRange(-2 * _WINDOW, 2 * _WINDOW)

    # 1. Reject path — total over any header string, and a random string is never a
    #    valid signature, so the answer must be a plain False.
    rejected = auth.verify_proxy_hmac(_SECRET, header, user, method, path, _WINDOW, now=_NOW)
    assert rejected is False, "a fuzzed header must never verify"

    # 2. Accept path — a header signed over the SAME values must verify inside the
    #    window and be rejected outside it. `remote_user` is falsy-gated by the
    #    function itself (empty user => False regardless), so only assert when set.
    if user:
        t = _NOW + skew
        accepted = auth.verify_proxy_hmac(
            _SECRET, _signed_header(user, method, path, t), user, method, path, _WINDOW, now=_NOW
        )
        assert accepted is (abs(skew) <= _WINDOW), "signed header verified against its window"

    # 3. `parse_bearer` on the same untrusted blob: None, or a space-free credential.
    credential = auth.parse_bearer(header)
    assert credential is None or (credential and " " not in credential)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
