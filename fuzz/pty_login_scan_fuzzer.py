"""Atheris fuzz harness for the ``claude`` login-output scanners in ``pty_screen``.

``login_shepherd`` drives ``claude auth login`` / ``claude setup-token`` and scrapes
their raw terminal output for the authorize URL it shows the operator to click, and
for the ``CLAUDE_CODE_OAUTH_TOKEN=`` the flow prints. That output is another process's
untrusted, version-coupled, escape-laden byte stream — the same class of input as the
already-fuzzed ``bridge_log`` markers, but on a *credential* path.

Three pure scanners share that boundary and are exercised together here because they
run over the same blob (``PtyScreen.find_authorize_url`` feeds OSC 8 URIs into the very
selector ``extract_authorize_url`` uses, so fuzzing them apart would miss the seam):

* ``extract_authorize_url`` — ANSI-strip, regex-scan, then *select* among candidates.
* ``extract_osc8_hyperlinks`` — pull URIs out of raw OSC 8 escapes (the ConPTY path,
  where the URL exists only inside the escape).
* ``extract_oauth_token`` — the printed long-lived credential.

All three are documented as total (``None`` / empty list, never raising), so any escape
is a bug; the harness catches nothing. Beyond crashes it asserts the security property
the selector exists for: **a decoy link can never outrank a real authorize endpoint.**
If any candidate URL's PATH identifies an OAuth authorize endpoint, the winner must be
one of those — otherwise a docs/help URL printed alongside the real one could be the
link an operator is told to open.

Note ``urlsplit``'s ``ValueError`` on a malformed port (the ``#122`` ``.port`` class the
``normalize_origin`` harness found) is already guarded inside ``_url_host``/``_url_path``;
this harness keeps that guard honest under adversarial input.
"""

import sys

import atheris

with atheris.instrument_imports():
    from clauster import pty_screen, redact


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    raw = fdp.ConsumeBytes(fdp.remaining_bytes())
    # One chunk feeds BOTH layers, as it does in reality: `login_shepherd` scans the
    # raw bytes for OSC 8 escapes and the decoded text for the URL/token. Decoding
    # with errors="replace" mirrors the reader (invalid UTF-8 off a PTY is replaced,
    # never fatal) and keeps a seed file a plain capture of real login output.
    text = raw.decode("utf-8", "replace")

    # 1. OSC 8 hyperlink recovery over raw PTY bytes. Contract: every complete
    #    open-sequence yields a non-empty ASCII-decoded URI; a stray byte is
    #    replaced, never raised on.
    for uri in pty_screen.extract_osc8_hyperlinks(raw):
        assert isinstance(uri, str) and uri, "OSC 8 URIs are non-empty strings"

    # 2. The printed setup-token credential. Contract: None or the matched value,
    #    which is `\S+` and therefore whitespace-free.
    token = pty_screen.extract_oauth_token(text)
    assert token is None or (token and not any(c.isspace() for c in token))

    # 3. The authorize URL. Contract: None or one of the URLs actually present in
    #    the ANSI-stripped output — never a fabricated or mangled one.
    url = pty_screen.extract_authorize_url(text)
    if url is None:
        return
    cleaned_output = redact.strip_ansi(text)
    assert url.startswith("https://"), "the URL regex is https-anchored"
    assert url in cleaned_output, "the winner must appear verbatim in the scanned output"

    # 4. Anti-decoy: when ANY candidate is a real authorize endpoint, the winner
    #    must be one too. A docs/help/settings link printed next to the real one
    #    must never become the link the operator is told to open.
    matches = pty_screen._URL_RE.finditer(cleaned_output)
    candidates = [c for c in (pty_screen._clean_url(m.group(0)) for m in matches) if c]
    if any(pty_screen._is_authorize_path(c) for c in candidates):
        assert pty_screen._is_authorize_path(url), "an authorize endpoint must outrank a decoy"


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
