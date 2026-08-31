"""Differential Atheris fuzz harness for ``config_write.redact_secret_lines``.

``redact_secret_lines`` is the line-oriented redaction every free-text read path runs
before content leaves the host: ``config_file_writer`` reads back a skill script or a
``CLAUDE.md`` through it, ``config_write_mcp_cli`` / ``config_write_plugins`` run CLI
stderr through it before putting it in an error message, and ``config_editor`` runs a
validation message through it. Its input is a file from a cloned repository — i.e.
attacker-supplied.

**Why differential rather than "does not crash".** The three scanners behind it are
hand-rolled linear rewrites of regexes that were quadratic on hostile input
(``py/polynomial-redos``): ``_interp_spans`` replaced ``\\$\\{[^}]+\\}``,
``_url_cred_spans`` replaced a non-anchored ``[a-z][a-z0-9+.\\-]*://[^/@\\s]+@``, and
``_split_kv_line`` replaced the three-group ``^(\\s*[\\w.\\-]+\\s*[:=]\\s*)(\\S.*?)(\\s*)$``.
Each docstring claims the rewrite "reproduces the regex exactly" — and every one of them
is claiming it in the direction where being wrong *leaks* (under-masking), which is the
one direction the module says it must never fail in. A crash harness cannot see that:
an under-masking rewrite returns a perfectly well-formed string.

So this harness re-derives the answer from the **replaced regexes themselves** and
compares. The regexes are the independent oracle: they are what the module's contract is
written against, they are not the code under test, and they are not a restatement of the
new scanners' logic. A divergence means one of the three rewrites is not the equivalence
it documents — a leak if the shipped side masks less, an over-mask regression if more.

Four properties are checked per input:

1. **Differential equality** — ``redact_secret_lines(text)`` equals the regex-built
   reference for the same documented contract, byte for byte.
2. **No interpolation survives** — every ``${…}`` found in the *input* is absent from the
   *output*. Deliberately redundant with (1) for the case where both sides agree and both
   are wrong about masking at all: it compares the target's output against the input
   directly, so a bug in the reference's *composition* (masking in the wrong order, losing
   a line, rebuilding a span wrong) cannot hide a leak. It is not a total backstop — the
   payloads are selected with ``_INTERP_RE``, so a bug in that one regex would make the
   property vacuous rather than failing it.
3. **Line shape is preserved** — the documented "preserves the original line endings and
   count exactly" guarantee, which a caller diffing line numbers against the source
   depends on. Checked against the *input*, so it holds independently of the reference.
4. **Second-pass agreement** — the differential is re-run over the already-redacted
   output. Sentinel-laden text is a different input distribution that random bytes will
   not reach on their own: ``********`` is a perfectly good ``[^/@\\s]+`` userinfo run,
   and a masked value changes what the key-value matcher sees on the line. It is also the
   distribution the module's own sentinel round-trip note is about.

Property 4 deliberately does **not** assert idempotence. ``redact_secret_lines`` is not
idempotent and need not be: on ``http://http://u@h`` the first pass masks the credential
and leaves a bare ``http://`` in front of the sentinel, which the second pass then reads
as a fresh ``scheme://userinfo@`` and masks too (fixed point after two passes). That is
the *over*-masking direction the module says it prefers, so pinning a fixed point here
would report a deliberate safety bias as a crash on every batch run.

⚠️ The reference regexes are the **quadratic** originals, so the harness caps its input
at :data:`_MAX_CHARS`. That cap is on the oracle's cost, not the target's: this harness
does not measure the linear-vs-quadratic timing property those rewrites exist for. A
libFuzzer timeout here would be the harness's fault, not a finding.

Note the properties are stated over *what must be masked*, never over what may be left
alone. The module documents that it has no value-entropy check — a real secret under a
benign key is knowingly not detected — so asserting the converse would encode a
deliberate design limit as a bug.
"""

import sys

import atheris

with atheris.instrument_imports():
    # `re` is in-block for the fuzz/README.md convention, NOT for edge signal — it buys
    # none here. `import atheris` itself imports `re`, so by this line it is already in
    # sys.modules and the in-block import degrades to a lookup (checked: the run log shows
    # `Instrumenting clauster.config_write`, never `Instrumenting re`). Nothing is lost:
    # the matching happens in the `_sre` C extension, which Atheris cannot instrument
    # either way. Contrast pty_login_scan_fuzzer, where `urllib.parse` genuinely does get
    # instrumented in-block and the placement is worth ~4x its edges.
    import re

    from clauster import config_write

# --- shared POLICY inputs: imported, never copied --------------------------------------
# Neither of these is part of the differential. The sentinel is a display constant, and
# the secret-key vocabulary is a word list — an *input* to the contract, not an algorithm
# implementing it. Restating either would buy no independence (a copied word list cannot
# test a word list) while adding a drift trap: widen `_SECRET_KEY_RE` in config_write and
# a copy here would start reporting the deliberate change as a differential crash, in the
# Security tab, days later. Importing them makes the comparison what it should be — over
# *which spans get masked*, not over the policy both sides are handed.
_SENTINEL = config_write.REDACTION_SENTINEL
_SECRET_KEY_RE = config_write._SECRET_KEY_RE

# --- the independent oracle: the regexes the shipped SCANNERS replaced ------------------
# These three are restated on purpose, and the independence is real: each is a regex that
# `config_write` rewrote as a hand-rolled linear scan, so the harness compares an algorithm
# against a different algorithm rather than against a copy of itself. Behaviour is pinned
# by tests/test_fuzz_harness_smoke.py, so a deliberate contract change fails `just check`.
_INTERP_RE = re.compile(r"\$\{[^}]+\}")
_URL_CRED_RE = re.compile(r"[a-z][a-z0-9+.\-]*://[^/@\s]+@", re.IGNORECASE)
_KV_LINE_RE = re.compile(r"^(?P<prefix>\s*[\w.\-]+\s*[:=]\s*)(?P<value>\S.*?)(?P<trail>\s*)$")

#: Input cap — see the module docstring: the oracle's regexes are the quadratic originals.
_MAX_CHARS = 4096


def _reference_redact(text: str) -> str:
    """Redact ``text`` per the documented contract, using only the replaced regexes."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body, eol = line, ""
        if body.endswith("\r\n"):
            body, eol = body[:-2], "\r\n"
        elif body.endswith("\n"):
            body, eol = body[:-1], "\n"
        # Replacement FUNCTIONS, not template strings: `re.sub` reads `\g`/`\1` escapes in
        # a template, so a future sentinel containing a backslash would silently change
        # what the oracle produces and false-crash the harness against a correct target.
        masked = _INTERP_RE.sub(lambda _m: _SENTINEL, body)
        masked = _URL_CRED_RE.sub(lambda _m: f"{_SENTINEL}@", masked)
        kv = _KV_LINE_RE.match(masked)
        if kv is not None and _SECRET_KEY_RE.search(kv.group("prefix")):
            masked = f"{kv.group('prefix')}{_SENTINEL}{kv.group('trail')}"
        out.append(masked + eol)
    return "".join(out)


def _secret_payloads(text: str) -> list[str]:
    """The literal substrings of ``text`` a surviving occurrence of which is a leak.

    Interpolations only, and that restriction is load-bearing in both directions:

    * **Sound for ``${…}``** — every occurrence on every line is inside some masked span
      (an occurrence not matched itself is nested in a longer match), and masking cannot
      manufacture a new one: it only ever inserts ``*`` and ``@``, so a ``${`` can never
      be created by removing the text between an existing ``$`` and ``{``.
    * **Unsound for ``scheme://user@``**, which is why it is not here. The replacement is
      ``********@`` — it *carries an ``@``* — so a ``scheme://`` left standing in front of
      one manufactures a fresh credential *shape* out of already-masked material:
      ``q://<a://b@h`` redacts to ``q://<********@h``, which contains a
      ``q://<********@`` that was never in the input. Nothing leaked (the userinfo is the
      sentinel), so a substring — or an "output holds no URL shape" — property would be
      reporting the redaction's own output as a finding. The URL scanner is covered by
      the differential instead, which compares spans rather than substrings.

    A secret-keyed KV *value* is excluded for a plainer reason: its text can legitimately
    reappear in the output, since a ``token: token`` line keeps its key.

    ⚠️ Scanned **per line**, because ``redact_secret_lines`` is. ``[^}]+`` happily crosses
    a newline, so a whole-text scan invents a ``${…}`` spanning two lines that the target
    is not contracted to mask, and the property would fail on the harness's own mistake.
    """
    return [m.group(0) for line in text.splitlines() for m in _INTERP_RE.finditer(line)]


def _eol(line: str) -> str:
    """The ``\\r\\n`` / ``\\n`` terminator of ``line``, or ``""`` for the other breaks.

    ``str.splitlines`` also breaks on ``\\v \\f \\x1c-\\x1e \\x85 U+2028 U+2029``, which
    ``redact_secret_lines`` does not treat as an EOL — it leaves them on the body, where
    they survive unchanged. Only the two it *does* strip and re-append are compared.
    """
    if line.endswith("\r\n"):
        return "\r\n"
    return "\n" if line.endswith("\n") else ""


def check(text: str) -> None:
    """Assert every property above for one input.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive the
    oracle from the suite: fuzz/README.md asks for proof that an assertion *can* fire, and
    an oracle that has never fired is indistinguishable from no oracle at all.
    """
    out = config_write.redact_secret_lines(text)

    expected = _reference_redact(text)
    assert out == expected, f"differential: {text!r}\n  got {out!r}\n  ref {expected!r}"

    for payload in _secret_payloads(text):
        assert payload not in out, f"leak: {payload!r} survived in {out!r}"

    in_lines = text.splitlines(keepends=True)
    out_lines = out.splitlines(keepends=True)
    assert len(out_lines) == len(in_lines), f"line count changed: {text!r} -> {out!r}"
    for src, dst in zip(in_lines, out_lines, strict=True):
        assert _eol(src) == _eol(dst), f"line ending changed: {src!r} -> {dst!r}"

    again = config_write.redact_secret_lines(out)
    expected_again = _reference_redact(out)
    assert again == expected_again, (
        f"differential (second pass): {out!r}\n  got {again!r}\n  ref {expected_again!r}"
    )


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    check(fdp.ConsumeUnicodeNoSurrogates(min(fdp.remaining_bytes(), _MAX_CHARS)))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
