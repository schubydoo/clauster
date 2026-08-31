"""Atheris fuzz harness for ``code_sessions._is_session_not_found``.

Of everything in the deferred set this is the one whose input is genuinely **remote
bytes**: the body of a 404 from ``api.anthropic.com``, parsed during a spawn preflight.
Nothing in clauster wrote it, and the module's own docstring says the API is *"a dated
beta"* — the shape is expected to churn.

What it decides is destructive. ``True`` means "this anchor session is gone", which
clears the preserved ``bridge-pointer.json`` and forces a cold start. The function
exists to keep a **route**-level 404 (a moved endpoint, a proxy error page, an HTML
splash) from being read as a *resource*-level one and clearing healthy pointers across
every project at once.

Two properties per input:

1. **Total and boolean.** Never raises — not on invalid UTF-8, not on a non-dict body,
   and not on the deeply-nested JSON that #1326 closed (``RecursionError`` is not a
   ``ValueError``, so it escaped the decoder's handler). The harness catches nothing.
2. **Fail closed.** ``True`` is only ever returned when the parsed body really does
   contain a mapping carrying **both** ``type == "not_found_error"`` and
   ``resource_type == "session"``. The oracle searches the whole decoded document for
   such a mapping, iteratively and at any depth, rather than re-reading the two places
   the target looks — so it is an independent check on the *answer*, not a copy of the
   lookup. One-directional on purpose: it can only catch a ``True`` that no evidence in
   the body supports, which is the direction that clears a live session's pointer.

The converse is deliberately not asserted. A body that carries the pair somewhere deep
inside — nested under an unrelated key, or inside a list of sub-errors — is one the
target is *supposed* to answer ``False`` to, because it only honours the top level and
``body["error"]``. Demanding ``True`` there would be asserting that the check should be
looser, on a function whose entire reason for existing is to be strict.

Also not asserted: anything about the ``404``/``archived``/``active`` classification
above this function. That lives in ``anchor_health``, needs an HTTP transport, and is
covered by ``tests/test_code_sessions.py``.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md: `json` is the parser under test on every reject path,
    # and it must be instrumented before `code_sessions` imports it.
    import json

    from clauster import code_sessions


def _carries_pair(body: object) -> bool:
    """Whether any mapping anywhere in ``body`` carries both not-found signals.

    Iterative, because the inputs this harness generates include documents nested deeply
    enough to overflow a recursive walk — a crash there would be the oracle's, not the
    target's, and would look identical in the Security tab.
    """
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "not_found_error" and node.get("resource_type") == "session":
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def check(raw: bytes) -> None:
    """Assert every property above for one 404 body.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    verdict = code_sessions._is_session_not_found(raw)
    assert isinstance(verdict, bool), f"not a bool: {verdict!r}"
    if not verdict:
        return

    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise AssertionError(f"said not-found for a body it could not parse: {raw!r}") from None
    assert _carries_pair(body), f"said not-found with no not_found_error/session pair: {raw!r}"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Raw bytes, unmodified: the target's parameter *is* the response body, so a seed file
    # is a literal captured 404 (see the fuzz/README.md passthrough note).
    check(fdp.ConsumeBytes(fdp.remaining_bytes()))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
