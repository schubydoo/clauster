"""Atheris fuzz harness for ``config_write.load_settings_json_obj``.

This is the read side of the **code-executing write tier**. Before merging into a
``.claude/settings.json`` — the file that decides which hooks and permissions a
``claude`` process runs with — every child surface parses the existing file through
this function. That file arrives with a cloned repository, so its bytes are supplied
by whoever wrote the repo, and the audit on issue 1322 flagged it as having **no
direct unit tests** of its own.

Its contract is short and entirely about failing closed: return a ``dict`` (``{}``
for empty or whitespace-only input), or raise ``InvalidCandidateError`` — non-UTF-8,
malformed JSON, JSON nested past CPython's recursive scanner, and a well-formed
non-object each map to that one structural error, because *"we will not overwrite a
file we could not parse."*

Three properties per input:

1. **Only ``InvalidCandidateError`` escapes.** Anything else — the ``RecursionError``
   that #1326 closed, a ``UnicodeDecodeError``, a ``TypeError`` from an unexpected
   shape — would propagate out of a 422 handler as a 500 and, worse, out of a code
   path whose next step writes a file. Every other exception is left uncaught, so the
   fuzzer reports it as a crash.
2. **Accept/reject agreement.** Whether the function accepts is compared against the
   documented rule re-derived from ``bytes.decode`` + ``json.loads`` + an
   ``isinstance`` check — not from the function's own branches. This is the direction
   that matters: a *widened* accept path would hand a caller something it then merges
   into a settings file. The rejections it must make are asserted; the reasons are not.
3. **Value agreement.** An accepted result must equal what plain ``json.loads``
   produced — the function is a guard, not a transform, and a caller round-trips the
   returned mapping straight back to disk through ``render_json``.

⚠️ Equality is compared through ``json.dumps(..., sort_keys=True)`` rather than ``==``
because JSON's ``NaN`` extension is real input here: ``json.loads(b'{"a": NaN}')``
succeeds, and ``float('nan') != float('nan')``, so a plain ``==`` would report a
correct implementation as a crash on the fuzzer's first ``NaN``. Serializing both
sides renders it as the same ``NaN`` token.

Not asserted: that ``render_json(load(raw))`` round-trips to the same bytes. It does
not, by design — ``render_json`` re-indents and normalizes — so pinning it would
report a deliberate formatting choice as a finding.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md: `json` is the parser this harness is differentiating
    # against, and the target's own `json.loads` call is the code under test. Importing
    # it at module scope would leave the decoder uninstrumented for both sides.
    import json

    from clauster import config_write

#: Cap on the input the reference re-serializes. `json.dumps` recurses, so a document
#: that `json.loads` accepted can still overflow the *oracle* on the way back out; the
#: cap keeps that from being reported as a finding in the target. It is generous enough
#: that no realistic settings file reaches it.
_MAX_BYTES = 65536


def _normalized(data: object) -> str:
    """Render ``data`` to a canonical string for NaN-safe comparison (see the module doc)."""
    return json.dumps(data, sort_keys=True, allow_nan=True, ensure_ascii=False)


def _reference(raw: bytes) -> tuple[bool, object]:
    """The documented contract, re-derived from ``decode`` + ``json.loads``.

    Returns ``(accepted, value)``. ``accepted`` is ``False`` for non-UTF-8, malformed
    JSON, over-deep JSON, and a well-formed non-object — the four rejections the
    docstring enumerates. ``value`` is meaningful only when accepted.
    """
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return False, None
    if not text:
        return True, {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return False, None
    return (True, data) if isinstance(data, dict) else (False, None)


def check(raw: bytes) -> None:
    """Assert every property above for one settings-file body.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    expected_ok, expected = _reference(raw)

    try:
        out = config_write.load_settings_json_obj(raw)
    except config_write.InvalidCandidateError:
        assert not expected_ok, f"rejected an input the contract accepts: {raw!r}"
        return

    assert expected_ok, f"accepted an input the contract rejects: {raw!r} -> {out!r}"
    assert isinstance(out, dict), f"accepted result is not a dict: {out!r}"
    if len(raw) <= _MAX_BYTES:
        assert _normalized(out) == _normalized(expected), (
            f"value differs for {raw!r}\n  got {out!r}\n  ref {expected!r}"
        )


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Raw bytes, unmodified: the target's parameter *is* bytes off disk, and a seed file
    # is then a literal settings.json (see the fuzz/README.md passthrough note).
    check(fdp.ConsumeBytes(fdp.remaining_bytes()))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
