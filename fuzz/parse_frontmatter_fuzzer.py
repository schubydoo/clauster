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
  error. ``yaml.YAMLError`` and the ``RecursionError`` #1326 closed are handled inside;
  anything else reaching the caller is a 500 on a path that is about to write a file.
  The harness catches only ``InvalidCandidateError``, so every other escape is a crash.
* **The body is returned verbatim.** Both promise it — for subagents in so many words
  ("returned verbatim ... never parsed or executed"). Asserted as ``text.endswith(body)``,
  i.e. the returned body is a genuine suffix of the input, so nothing was re-encoded,
  normalized, or dropped on the way through. Checked against the *input*, so it holds
  without reference to the other parser.
* **The header is a mapping**, which is the type every downstream ``validate_*`` assumes.

And across the pair:

* **No header drift.** *When both accept the same text, they must return the same
  mapping.* The header is the part that matters: it is where a subagent's ``tools`` and a
  skill's ``allowed-tools`` come from, so two parsers reading one file into two different
  grants is the divergence with consequences. Stated as an implication rather than as
  equality of the accept sets, deliberately — the two do not accept the same inputs today
  and this harness must not rule on which is right.

⚠️ Mapping equality goes through :func:`_same`, which falls back to ``repr``. YAML's
``.nan`` is real input here and ``float('nan') != float('nan')``, so a plain ``==`` would
report two identical mappings as a divergence on the fuzzer's first ``.nan``.

**Body equality is deliberately NOT asserted, because it does not hold today.** The first
run of this harness found it on its own seed corpus, and the divergence is systematic
rather than incidental: subagents' fence pattern ends ``---[ \\t]*\\r?\\n?`` and skills'
ends ``---\\r?\\n?``, so anything trailing the closing ``---`` is swallowed by one parser
and handed to the caller as the start of the body by the other. ``---\\na: 1\\n--- \\nbody``
yields a body of ``'body'`` from subagents and ``' \\nbody'`` from skills. That is reported
as an open finding on the PR that introduced this harness — **not** accepted here as
correct, and **not** silently excluded: ``tests/test_fuzz_harness_smoke.py`` pins the
current divergence, so whoever converges the two parsers gets a failing ``just check``
pointing straight at this paragraph rather than a green suite. Asserting it in the harness
instead would mean a red SARIF on every batch run, which is what the issue 1322 audit
deferred these harnesses to avoid.

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


#: Explicit YAML tags whose constructors raise something that is **not** a ``YAMLError``,
#: so they escape both parsers' documented "only ``InvalidCandidateError``" contract.
#: Measured against PyYAML 6.x's ``SafeLoader``:
#:
#: * ``!!int x``        -> ``ValueError``
#: * ``!!float x``      -> ``ValueError``
#: * ``!!bool x``       -> ``KeyError``
#: * ``!!timestamp x``  -> ``AttributeError`` (``construct_yaml_timestamp`` calls
#:   ``.groupdict()`` on an unchecked ``re.match``)
#:
#: This is the same class #1326 closed for ``RecursionError`` and it is still open for
#: these four: a ``SKILL.md`` or subagent file arriving with a cloned repository can make
#: the **code-executing write tier** raise out of a path documented to fail as a 422.
#: Reported as an open finding on the PR that added this harness; **not fixed here**,
#: because widening the ``except`` in two parsers is a change on the write tier and wants
#: its own review. Inputs carrying one are skipped entirely rather than having the
#: exception swallowed — swallowing would hide the *next*, unknown escape class too, and
#: the point of this harness is to find those. Pinned in the suite so the fix flips
#: ``just check`` and sends the next reader here to delete this tuple.
_YAML_UNCONTRACTED_TAGS = ("!!int", "!!float", "!!bool", "!!timestamp")


def _header_region(text: str) -> str:
    """The text either parser would hand to ``yaml.safe_load``, for the tag scan above.

    Taken from **the parsers' own fence regexes**, not from a hand-rolled split. A
    ``text.partition("\\n---")`` heuristic looks equivalent and is not: a header whose first
    line itself starts with ``---`` truncates the region to the opening fence, so
    ``"---\\n---x: 1\\nk: !!int z\\n---\\nbody\\n"`` slipped past the scan and raised the very
    ``ValueError`` this exclusion exists to keep out of the Security tab.

    The union of both groups is used because the two regexes do not accept the same inputs
    (skills rejects a fence with trailing whitespace, subagents does not), and a tag inside
    a region *either* parser will load is enough to reach the escape. On no match, the whole
    text is returned — the conservative direction: a skipped input costs one iteration,
    while a missed one costs a false crash on every batch run.
    """
    regions = [
        match.group(1)
        for pattern in (
            config_write_subagents._FRONTMATTER_RE,
            config_write_skills._FRONTMATTER_RE,
        )
        if (match := pattern.match(text)) is not None
    ]
    return "\n".join(regions) if regions else text


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
    if any(tag in _header_region(text) for tag in _YAML_UNCONTRACTED_TAGS):
        return  # open finding — see _YAML_UNCONTRACTED_TAGS

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
        # Headers only — see the module docstring on why body equality is not asserted.
        head_a, head_b = results["subagents"][0], results["skills"][0]
        assert _same(head_a, head_b), f"drift: headers differ for {text!r}: {head_a!r} {head_b!r}"


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
