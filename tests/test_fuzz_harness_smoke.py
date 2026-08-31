"""Import-smoke test for the Atheris fuzz harnesses (issue #356).

The harnesses under ``fuzz/`` are standalone OSS-Fuzz entry points, not part of
the package, so nothing in the default suite imports them — a rename or
signature change to a fuzzed function (``redact.sanitize_line``,
``provisioning.validate_clone_url``, …) silently breaks a harness until the next
scheduled fuzz run. This test imports each harness so that drift fails fast.

Importing a harness runs its module body, which calls
``atheris.instrument_imports()`` around the real ``clauster`` import — so the
fuzzed symbols must still exist and import cleanly. ``main()`` / ``atheris.Fuzz()``
is guarded behind ``if __name__ == "__main__"`` and never runs here.

Atheris ships Linux-only wheels (see ``pyproject.toml``); the test skips where it
is unavailable rather than failing a Windows/macOS CI cell. It also skips on a
Python newer than Atheris supports — Atheris 2.0 raises ``RuntimeError`` (not
``ImportError``) at import on an unsupported version (e.g. 3.14), which
``importorskip`` would not catch.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

try:
    import atheris  # noqa: F401  — availability gate only; harnesses import it themselves
except (ImportError, RuntimeError) as exc:  # RuntimeError: unsupported Python (Atheris 2.0)
    pytest.skip(f"atheris unavailable: {exc}", allow_module_level=True)

_FUZZ_DIR = Path(__file__).resolve().parent.parent / "fuzz"
_HARNESSES = sorted(p.name for p in _FUZZ_DIR.glob("*_fuzzer.py"))


def test_fuzz_dir_has_harnesses() -> None:
    """Guard against the glob silently matching nothing (e.g. a moved fuzz/ dir)."""
    assert _HARNESSES, f"no *_fuzzer.py harnesses found under {_FUZZ_DIR}"


@pytest.mark.parametrize("harness", _HARNESSES)
def test_fuzz_harness_imports_and_exposes_entrypoints(harness: str) -> None:
    """Each harness imports cleanly and exposes the Atheris TestOneInput + main entry points."""
    path = _FUZZ_DIR / harness
    spec = importlib.util.spec_from_file_location(f"_fuzz_smoke.{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Executes the module body: instruments + imports the real clauster symbols,
    # so a signature/rename drift surfaces here as an ImportError/AttributeError.
    spec.loader.exec_module(module)
    assert callable(module.TestOneInput), f"{harness} missing a callable TestOneInput"
    assert callable(module.main), f"{harness} missing a callable main"


def _load(harness: str):
    path = _FUZZ_DIR / harness
    spec = importlib.util.spec_from_file_location(f"_fuzz_smoke.{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# URLs spanning every branch of the authorize-path / known-auth-host predicates:
# the real endpoint, the bare-/authorize fallback, the query-string decoy the path
# check exists to reject, docs/marketing exclusions, an ACCEPTED subdomain on each
# parent domain (the `endswith("." + suffix)` branch, which every excluded-prefix row
# short-circuits past), an unknown host that proves the leading dot is load-bearing,
# and the invalid-IPv6-bracket URL that makes urlsplit raise.
_PREDICATE_URLS = [
    "https://claude.com/cai/oauth/authorize?client_id=x",
    "https://claude.ai/oauth/authorize",
    "https://console.anthropic.com/authorize",
    "https://platform.claude.com/oauth/authorize",
    "https://claude.com/settings?redirect_uri=%2Foauth%2Fauthorize",
    "https://docs.anthropic.com/en/docs/oauth/authorize",
    "https://help.claude.com/oauth/authorize",
    "https://support.claude.ai/authorize",
    "https://www.claude.com/oauth/authorize",
    "https://anthropic.com/oauth/authorize",
    "https://notclaude.com/oauth/authorize",
    "https://evil.example/cai/oauth/authorize",
    "https://claude.com/",
    # Parses fine — urlsplit validates a port only when `.port` is read, and neither
    # helper reads it. Kept to pin that, not to drive the ValueError branch below.
    "https://claude.com:notaport/oauth/authorize",
    "https://[::1/oauth/authorize",
    "https://[bad/authorize",
    "",
]


@pytest.mark.parametrize("url", _PREDICATE_URLS)
def test_pty_login_scan_restated_predicates_match_pty_screen(url: str) -> None:
    """The harness's independent oracles must agree with the functions they judge.

    ``pty_login_scan_fuzzer`` deliberately restates ``_is_authorize_path`` and
    ``_is_known_auth_host`` rather than calling them, so an oracle cannot be fooled by
    a misclassification inside the code it is checking. That independence has a cost:
    a maintainer who legitimately broadens either predicate would otherwise get a green
    ``just check`` and learn about the drift days later, as an ``assert`` in ``fuzz/``
    surfacing in the Security tab. This test moves that failure back into the suite.
    """
    from clauster import pty_screen

    harness = _load("pty_login_scan_fuzzer.py")
    assert harness._path_is_authorize(url) == pty_screen._is_authorize_path(url), url
    assert harness._host_is_known_auth(url) == pty_screen._is_known_auth_host(
        pty_screen._url_host(url)
    ), url


# Inputs spanning every branch `redact_secret_lines_fuzzer` differentiates: each masked
# span shape, the empty-body `${}` and unterminated `${` the interpolation scan must NOT
# match, the leading `-` and doubled `://` the URL scan's lookbehind-free walk exists for,
# a non-ASCII scheme letter that case-folds into `[a-z]`, the author/authn boundary of the
# secret-key lookahead, and the CR/LF/vertical-tab line shapes.
_REDACT_LINE_INPUTS = [
    "",
    "nothing secret here",
    "env: ${GITHUB_TOKEN}",
    "a ${b${c} d ${} e ${f",
    "clone https://user:pw@example.test/repo.git",
    "-slack+v2://xoxb-0@host",
    "http://http://u@h",
    "q://<a://b@h",  # redacts to a URL shape that was never in the input
    "\u212a://user@host",
    "\u017fsh://u@h",
    # Low-entropy on purpose: gitleaks scans the PR's commit range and its generic-api-key
    # rule has an entropy gate; the KV matcher under test reads only the key + value SHAPE.
    "api_key: sk-live-FAKEFAKEFAKEFAKE",
    "AUTH_TOKEN = xoxb-1111   ",
    "authors: Someone",
    "author = Someone Else",
    "authn: masked",
    "token: token",
    "token: a\r\nplain\r\n${X}\rmid\x0bvtab: b\n",
    "data=" + "a" * 64 + "://b@c",
    "*" * 8,
]


@pytest.mark.parametrize("text", _REDACT_LINE_INPUTS)
def test_redact_secret_lines_reference_oracle_matches_implementation(text: str) -> None:
    """The differential harness's regex oracle must agree with the shipped scanners.

    ``redact_secret_lines_fuzzer`` re-derives the answer from the quadratic regexes the
    linear scanners in ``config_write`` replaced — deliberately *not* by calling those
    scanners, so a rewrite that under-masks cannot move both sides of the comparison
    together. Same trade as the ``pty_login_scan`` predicates above: a maintainer who
    changes the contract on purpose should fail ``just check``, not learn about it days
    later from a Security-tab SARIF.
    """
    from clauster import config_write

    harness = _load("redact_secret_lines_fuzzer.py")
    assert harness._reference_redact(text) == config_write.redact_secret_lines(text), text


def test_redact_secret_lines_oracle_fires_on_a_broken_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break the redaction on purpose; the harness must notice.

    ``fuzz/README.md``: an oracle that has never fired is indistinguishable from no
    oracle at all. Each property is fired **separately**, because they short-circuit in
    order: with only ``redact_secret_lines`` broken the differential rejects it first and
    the payload-survival assert is never reached, so a single case would leave the one
    property that directly encodes "no ``${…}`` leaks" with no proof it can fail at all.
    Breaking the reference the same way makes the two sides agree and hands the leak
    check the pass-through output it exists to catch.
    """
    from clauster import config_write

    harness = _load("redact_secret_lines_fuzzer.py")
    leaky = "env: ${GITHUB_TOKEN}"
    harness.check(leaky)  # unbroken: passes

    monkeypatch.setattr(config_write, "redact_secret_lines", lambda text: text)
    with pytest.raises(AssertionError, match="^differential:"):
        harness.check(leaky)

    monkeypatch.setattr(harness, "_reference_redact", lambda text: text)
    with pytest.raises(AssertionError, match="^leak:"):
        harness.check(leaky)
