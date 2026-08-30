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
