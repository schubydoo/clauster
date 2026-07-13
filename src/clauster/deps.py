"""Optional-extras detection for the frozen binary + doctor/UI surfaces (#904).

Clauster ships several capabilities behind optional pip *extras* that the signed
standalone binary deliberately does not bundle: ``pyte`` (LGPL) powers the live
terminal view (#534), ``pywinpty`` (win32-only) backs the Windows ConPTY keeper,
and ``apprise`` drives outbound notifications. This module is the single, pure,
side-effect-free source of truth for *which* extras exist, *whether* each is
importable in the running interpreter, and *how* to install it — consumed by
``clauster doctor`` (CLI + dashboard preflight) and the dashboard's live-terminal
control. Detection uses :func:`importlib.util.find_spec`, which locates a module
without importing it: no LGPL relinking, no import side effects, no import cost.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass


def is_frozen() -> bool:
    """Return whether we're running as a PyInstaller-frozen standalone binary.

    The frozen binary bundles no optional extras and ignores site-packages /
    ``PYTHONPATH``, so install hints must differ from a normal pip/uv install.
    Shared helper that dedupes the ``getattr(sys, "frozen", False)`` idiom.
    """
    return bool(getattr(sys, "frozen", False))


@dataclass(frozen=True)
class Extra:
    """One optional capability: its dist, import name, pip extra, and UI label.

    ``platform_marker`` is ``None`` for a cross-platform extra, or a
    :data:`sys.platform` value (e.g. ``"win32"``) when the capability only applies
    on that platform — off-platform the entry is irrelevant and callers skip it.
    """

    key: str
    dist: str
    import_name: str
    extra_name: str
    capability_label: str
    platform_marker: str | None = None


EXTRAS: tuple[Extra, ...] = (
    Extra(
        key="pyte",
        dist="pyte",
        import_name="pyte",
        extra_name="pty",
        capability_label="Live terminal view (#534)",
    ),
    Extra(
        key="pywinpty",
        dist="pywinpty",
        import_name="winpty",
        extra_name="pty",
        capability_label="Interactive Session on Windows (ConPTY keeper)",
        platform_marker="win32",
    ),
    Extra(
        key="apprise",
        dist="apprise",
        import_name="apprise",
        extra_name="notify",
        capability_label="Outbound notifications (Apprise)",
    ),
)


def by_key(key: str) -> Extra:
    """Return the registered :class:`Extra` for ``key`` (raises ``KeyError`` if unknown)."""
    for entry in EXTRAS:
        if entry.key == key:
            return entry
    raise KeyError(key)


def applies(entry: Extra) -> bool:
    """Return whether ``entry`` is relevant on the current platform.

    A ``None`` marker applies everywhere; otherwise the entry is only relevant when
    its marker matches :data:`sys.platform` (e.g. ``pywinpty`` on ``"win32"``), so
    doctor and the UI don't nag a Linux host about a Windows-only extra.
    """
    return entry.platform_marker is None or sys.platform == entry.platform_marker


def probe(entry: Extra) -> bool:
    """Return whether ``entry``'s import is resolvable, without importing it.

    Uses :func:`importlib.util.find_spec` so detection has no side effects — the
    module is never executed (critical for the LGPL ``pyte`` boundary) and there's
    no import cost. A missing top-level module returns ``None``; a submodule whose
    parent isn't a package raises :class:`ModuleNotFoundError`, and a
    half-initialised module raises :class:`ValueError` — both read as absent rather
    than propagating.
    """
    try:
        return importlib.util.find_spec(entry.import_name) is not None
    except (ImportError, ValueError):
        return False


def install_hint(entry: Extra) -> str:
    """Return an environment-correct one-line install hint for ``entry``.

    A normal pip/uv install resolves the extra through the package index
    (``pip install 'clauster[pty]'``). The frozen binary bundles nothing and
    ignores site-packages, so it points at the side-install command instead
    (``clauster deps install pty``). Kept honest per environment so doctor and the
    dashboard never hand the operator a dead-end command.
    """
    if is_frozen():
        return f"clauster deps install {entry.extra_name}"
    return f"pip install 'clauster[{entry.extra_name}]'"
