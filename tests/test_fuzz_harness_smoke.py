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
is unavailable rather than failing a Windows/macOS CI cell.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("atheris", reason="atheris is Linux-only; harnesses can't import without it")

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
