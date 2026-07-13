"""Optional-extras detection (#904): registry, side-effect-free probe, frozen-aware hints."""

from __future__ import annotations

import pytest

from clauster import deps


def test_registry_covers_the_three_extras():
    keys = {e.key for e in deps.EXTRAS}
    assert keys == {"pyte", "pywinpty", "apprise"}
    # pyte + pywinpty both belong to the `pty` extra; apprise to `notify`.
    assert {e.extra_name for e in deps.EXTRAS} == {"pty", "notify"}
    # pywinpty is the only platform-scoped entry (win32); the rest are cross-platform.
    assert deps.by_key("pywinpty").platform_marker == "win32"
    assert deps.by_key("pyte").platform_marker is None
    assert deps.by_key("apprise").platform_marker is None


def test_by_key_unknown_raises():
    with pytest.raises(KeyError):
        deps.by_key("nope")


def test_applies_honours_platform_marker(monkeypatch):
    win = deps.by_key("pywinpty")
    monkeypatch.setattr(deps.sys, "platform", "linux")
    assert deps.applies(win) is False
    assert deps.applies(deps.by_key("pyte")) is True  # None marker applies everywhere
    monkeypatch.setattr(deps.sys, "platform", "win32")
    assert deps.applies(win) is True


def test_probe_present_when_find_spec_returns_a_spec(monkeypatch):
    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: object())
    assert deps.probe(deps.by_key("pyte")) is True


def test_probe_absent_when_find_spec_returns_none(monkeypatch):
    monkeypatch.setattr(deps.importlib.util, "find_spec", lambda name: None)
    assert deps.probe(deps.by_key("pyte")) is False


@pytest.mark.parametrize("exc", [ImportError("x"), ModuleNotFoundError("x"), ValueError("x")])
def test_probe_swallows_lookup_errors_as_absent(monkeypatch, exc):
    # A submodule whose parent isn't a package raises ModuleNotFoundError; a half-initialised
    # module raises ValueError — both must read as "absent", never propagate out of a probe.
    def _raise(name):
        raise exc

    monkeypatch.setattr(deps.importlib.util, "find_spec", _raise)
    assert deps.probe(deps.by_key("apprise")) is False


def test_probe_never_imports_the_module(monkeypatch):
    # The whole point of find_spec over import: no side effects (LGPL pyte is never executed).
    called = {}
    monkeypatch.setattr(
        deps.importlib.util, "find_spec", lambda name: called.setdefault("n", name)
    )
    deps.probe(deps.by_key("pywinpty"))
    assert called["n"] == "winpty"  # probes the IMPORT name, not the dist name


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(deps.sys, "frozen", raising=False)
    assert deps.is_frozen() is False
    monkeypatch.setattr(deps.sys, "frozen", True, raising=False)
    assert deps.is_frozen() is True


def test_install_hint_pip_form_when_not_frozen(monkeypatch):
    monkeypatch.setattr(deps, "is_frozen", lambda: False)
    assert deps.install_hint(deps.by_key("pyte")) == "pip install 'clauster[pty]'"
    assert deps.install_hint(deps.by_key("apprise")) == "pip install 'clauster[notify]'"


def test_install_hint_deps_command_when_frozen(monkeypatch):
    # The frozen binary ignores site-packages, so `pip install` is a dead end — point at the
    # side-install command instead (delivered in the follow-up slice).
    monkeypatch.setattr(deps, "is_frozen", lambda: True)
    assert deps.install_hint(deps.by_key("pyte")) == "clauster deps install pty"
