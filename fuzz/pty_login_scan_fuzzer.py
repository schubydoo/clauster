"""Atheris fuzz harness for the ``claude`` login-output scanners in ``pty_screen``.

``login_shepherd`` drives ``claude auth login`` / ``claude setup-token`` and scrapes
their raw terminal output for the authorize URL it shows the operator to click, and
for the ``CLAUDE_CODE_OAUTH_TOKEN=`` the flow prints. That output is another process's
untrusted, version-coupled, escape-laden byte stream — the same class of input as the
already-fuzzed ``bridge_log`` markers, but on a *credential* path.

Three pure scanners share that boundary and are exercised together here because
``PtyScreen.find_authorize_url`` runs them as one pipeline: the visible-text scan first,
and on a miss the OSC 8 hyperlink targets through the *same* selector behind a stricter
filter. Fuzzing them apart would leave that seam untested.

* ``extract_authorize_url`` — ANSI-strip, regex-scan, then *select* among candidates.
* ``extract_osc8_hyperlinks`` — pull URIs out of raw OSC 8 escapes (the ConPTY path,
  where the URL exists only inside the escape).
* ``extract_oauth_token`` — the printed long-lived credential.

All three are documented as total (``None`` / empty list, never raising), so any escape
is a bug; the harness catches nothing. Beyond crashes it asserts two selection
properties, and — because an oracle that merely re-runs the code it is checking cannot
catch a misclassification — it restates the *predicates* independently (reusing only
``pty_screen``'s host/path constants, which are data a maintainer may legitimately
extend, never its `_is_authorize_path` / `_is_known_auth_host` logic):

1. **Anti-decoy.** If any candidate URL's path is an authorize endpoint, the winner's
   path must be one too — a docs/help/settings link printed alongside the real one must
   never become the link the operator is told to open.
2. **Hidden targets clear a stricter bar.** An OSC 8 target is invisible to the operator
   (they see only the link label), so ``find_authorize_url`` accepts one only if it is
   BOTH on a known Claude/Anthropic auth host AND an authorize path. Replicated here so
   a hidden decoy on an unknown host, or a hidden non-authorize link on an allowed host,
   can never come back.

Note the ``urlsplit`` ``ValueError`` that ``_url_host`` / ``_url_path`` guard is the
**invalid-IPv6-bracket** one (``https://[::1/x``) — NOT the ``.port`` case the
``normalize_origin`` harness found, since neither helper reads ``.port``. The dictionary
and seed corpus target the bracket case accordingly.
"""

import sys

import atheris

with atheris.instrument_imports():
    # `urlsplit` MUST be imported inside this block. Importing it at module scope loads
    # `urllib.parse` before Atheris can instrument it, and `pty_screen`'s own later import
    # then reuses the uninstrumented module — measured: edge coverage collapses from ~210
    # to 52, because the URL parsing this harness exists to drive stops being traced.
    from urllib.parse import urlsplit

    from clauster import pty_screen, redact

    # Bound at module scope on purpose. Were these looked up inside TestOneInput, a
    # rename in `pty_screen` would sail past tests/test_fuzz_harness_smoke.py (which
    # only imports the module) and reappear as an AttributeError SARIF "crash" on the
    # next batch run. Binding makes the drift fail in the test suite instead.
    _URL_RE = pty_screen._URL_RE
    _clean_url = pty_screen._clean_url
    _select_authorize_url = pty_screen._select_authorize_url
    _is_known_auth_host = pty_screen._is_known_auth_host
    _is_authorize_path = pty_screen._is_authorize_path
    _url_host = pty_screen._url_host
    # Data, not logic — deliberately shared so extending the host list can't false-crash.
    _AUTHORIZE_MARKERS = pty_screen._AUTHORIZE_PATH_MARKERS
    _KNOWN_HOSTS = pty_screen._KNOWN_AUTH_HOST_SUFFIXES
    _NON_AUTH_PREFIXES = pty_screen._NON_AUTH_HOST_PREFIXES


def _path_is_authorize(url: str) -> bool:
    """Restate ``_is_authorize_path`` independently, so a drift in it is detectable."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return False  # matches _url_path's "" fallback, which satisfies neither test
    return any(marker in path for marker in _AUTHORIZE_MARKERS) or path.endswith("/authorize")


def _host_is_known_auth(url: str) -> bool:
    """Restate ``_is_known_auth_host`` independently, over an independently parsed host."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith(_NON_AUTH_PREFIXES):
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _KNOWN_HOSTS)


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
    uris = pty_screen.extract_osc8_hyperlinks(raw)
    for uri in uris:
        assert isinstance(uri, str) and uri, "OSC 8 URIs are non-empty strings"

    # 2. The OSC 8 seam. `PtyScreen.find_authorize_url` feeds these hidden targets to the
    #    shared selector behind a host AND path filter; replicate it and check the winner
    #    against the independent predicates.
    hidden = _select_authorize_url(
        u for u in uris if _is_known_auth_host(_url_host(u)) and _is_authorize_path(u)
    )
    if hidden is not None:
        assert _path_is_authorize(hidden), "a hidden OSC 8 target must be an authorize path"
        assert _host_is_known_auth(hidden), "a hidden OSC 8 target must be on a known auth host"

    # 3. The printed setup-token credential. Contract: None or the matched value,
    #    which is `\S+` and therefore whitespace-free.
    token = pty_screen.extract_oauth_token(text)
    assert token is None or (token and not any(c.isspace() for c in token))

    # 4. The authorize URL. Contract: None or one of the URLs actually present in
    #    the ANSI-stripped output — never a fabricated or mangled one.
    url = pty_screen.extract_authorize_url(text)
    if url is None:
        return
    cleaned_output = redact.strip_ansi(text)
    assert url.startswith("https://"), "the URL regex is https-anchored"
    assert url in cleaned_output, "the winner must appear verbatim in the scanned output"

    # 5. Anti-decoy: when ANY candidate is a real authorize endpoint, the winner must be
    #    one too, judged by the independent path check — so a change that made
    #    `_is_authorize_path` match the QUERY string (the thing it exists to prevent)
    #    cannot slip through by shifting both sides of the comparison together.
    matches = _URL_RE.finditer(cleaned_output)
    candidates = [c for c in (_clean_url(m.group(0)) for m in matches) if c]
    if any(_path_is_authorize(c) for c in candidates):
        assert _path_is_authorize(url), "an authorize endpoint must outrank a decoy"


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
