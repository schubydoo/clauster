"""Atheris fuzz harness for the two ``parse_frontmatter`` implementations.

``config_write_subagents.parse_frontmatter`` and ``config_write_skills.parse_frontmatter``
each split a ``---``-delimited YAML header off a file that arrived with a cloned
repository, on the **code-executing write tier**: what the header says becomes a
subagent's tool grant or a skill's ``allowed-tools``. They are near-identical by
intent — the audit on issue 1322 asked for **one** harness over both, precisely so a
divergence shows up as a fuzz finding rather than as two files quietly drifting apart.

Per parser, per input:

* **Only ``InvalidCandidateError`` escapes.** Both docstrings promise that a missing or
  malformed block, invalid YAML, and a non-mapping header are all rejected as that one
  error. ``yaml.YAMLError``, the ``RecursionError`` #1326 closed, and the explicit-tag
  ``ValueError``/``KeyError``/``AttributeError`` #1354 closed are all handled inside
  ``config_write.load_frontmatter_yaml``; anything else reaching the caller is a 500 on a
  path that is about to write a file. The harness catches only ``InvalidCandidateError``,
  so every other escape is a crash.
* **The body is returned verbatim.** Both promise it — for subagents in so many words
  ("returned verbatim ... never parsed or executed"). Asserted as ``text.endswith(body)``,
  i.e. the returned body is a genuine suffix of the input, so nothing was re-encoded,
  normalized, or dropped on the way through. Checked against the *input*, so it holds
  without reference to the other parser.
* **The header is a mapping**, which is the type every downstream ``validate_*`` assumes.

And across the pair:

* **No drift, header or body.** *When both accept the same text, they must return the
  same mapping and the same body.* The header is the part with the most consequence — it
  is where a subagent's ``tools`` and a skill's ``allowed-tools`` come from, so two
  parsers reading one file into two different grants is the divergence that matters — but
  the body is the file the operator is shown and re-submits, so a byte of disagreement
  there is a silent edit. Stated as an implication rather than as equality of the accept
  sets, deliberately: the two surfaces still differ on what they *accept* (skills rejects
  a non-mapping header that subagents reads as ``{}``) and this harness must not rule on
  which is right.

⚠️ Mapping equality goes through :func:`_same`, which falls back to ``repr``. YAML's
``.nan`` is real input here and ``float('nan') != float('nan')``, so a plain ``==`` would
report two identical mappings as a divergence on the fuzzer's first ``.nan``.

Body equality was **not** asserted when this harness landed: the first run found a
systematic divergence on its own seed corpus, because the two fence patterns disagreed on
trailing whitespace (``---\\na: 1\\n--- \\nbody`` gave ``'body'`` from subagents and
``' \\nbody'`` from skills). #1352 converged them onto one shared pattern —
``config_write.FRONTMATTER_RE``, aliased by both modules — so the property now holds and
is asserted here.

Not asserted: any particular parse of the YAML itself. Both call ``yaml.safe_load``; the
harness is about the framing around it, not about re-implementing a YAML parser.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md. `yaml` is the parser inside both targets — it is pure
    # Python, so instrumenting it is where most of this harness's edge signal comes from,
    # and a module-scope import would leave the reader/scanner/composer untraced.
    import yaml  # noqa: F401  — instrumented for coverage; the targets do the loading

    from clauster import config_write, config_write_skills, config_write_subagents


def _same(a: object, b: object) -> bool:
    """Structural equality that treats ``NaN`` as equal to itself (YAML ``.nan``)."""
    return bool(a == b) or repr(a) == repr(b)


def _parse(parser, text: str) -> tuple[dict, str] | None:
    """Run one parser, returning its ``(header, body)`` or ``None`` if it rejected.

    Only ``InvalidCandidateError`` is caught — the single rejection both contracts name.
    Every other exception propagates and is reported by the fuzzer as a crash.
    """
    try:
        return parser(text)
    except config_write.InvalidCandidateError:
        return None


def check(text: str) -> None:
    """Assert every property above for one candidate file.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    results = {}
    for label, parser in (
        ("subagents", config_write_subagents.parse_frontmatter),
        ("skills", config_write_skills.parse_frontmatter),
    ):
        parsed = _parse(parser, text)
        if parsed is None:
            continue
        header, body = parsed
        assert isinstance(header, dict), f"{label}: header is not a mapping: {header!r}"
        assert isinstance(body, str), f"{label}: body is not a str: {body!r}"
        assert text.endswith(body), f"{label}: body is not a suffix of the input: {body!r}"
        results[label] = (header, body)

    if len(results) == 2:
        (head_a, body_a), (head_b, body_b) = results["subagents"], results["skills"]
        assert _same(head_a, head_b), f"drift: headers differ for {text!r}: {head_a!r} {head_b!r}"
        assert body_a == body_b, f"drift: bodies differ for {text!r}: {body_a!r} {body_b!r}"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # ConsumeBytes + decode, NOT ConsumeUnicodeNoSurrogates — see fuzz/README.md's
    # seed-passthrough trap: the unicode consumer spends the first byte on an encoding
    # selector, which would strip the leading `-` off every `---` fence in the corpus and
    # leave the whole seed set on the "no frontmatter block" reject path.
    check(fdp.ConsumeBytes(fdp.remaining_bytes()).decode("utf-8", "replace"))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
