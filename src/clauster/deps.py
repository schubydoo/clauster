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

import importlib.metadata
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from email.message import Message


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
    """Return an environment-correct one-line install *command* for ``entry``.

    A normal pip/uv install resolves the extra through the package index
    (``pip install 'clauster[pty]'``). The frozen binary can't pip-install into itself, so it
    bundles pip and offers the managed ``clauster deps install <extra>`` command instead (#904
    slice 2b) — a real, runnable command on the binary. Both forms are runnable, so callers
    render either with a "run" imperative.
    """
    if is_frozen():
        return f"clauster deps install {entry.extra_name}"
    return f"pip install 'clauster[{entry.extra_name}]'"


# ----- managed side-install for the frozen binary (#904 slice 2) -----------
#
# The standalone binary can't ``pip install`` an extra into itself and ignores
# site-packages, so ``clauster deps install <extra>`` fetches the extra's wheels
# into a managed ``<state_dir>/deps`` directory that :func:`add_deps_dir_to_sys_path`
# puts on ``sys.path`` at startup. All pure-Python orchestration lives here (the
# pip invocation is a monkeypatchable seam); the CLI wiring is in ``__main__``.

DEPS_SUBDIR = "deps"

#: Shown before any managed install: the wheels come from PyPI, NOT the signed
#: release, so the operator is trusting PyPI + the publishers directly. Printing
#: this and requiring confirmation keeps the provenance boundary honest and keeps
#: LGPL ``pyte`` in a separate user directory that is never relinked into the binary.
PROVENANCE_NOTICE = (
    "This downloads third-party wheels from the Python Package Index into a managed\n"
    "directory beside your Clauster state. They are NOT covered by the Clauster release\n"
    "signature — installing them means trusting PyPI and the wheel publishers directly."
)


class DepsPipUnavailableError(RuntimeError):
    """Raised when pip can't be imported to drive a managed side-install.

    The frozen binary will bundle pip once #904 slice 2b lands; until then (and on any
    stripped/older build) pip is absent, so this surfaces with a clear fallback hint.
    """


def managed_deps_dir(state_dir: str | Path) -> Path:
    """Return the managed side-install directory ``<state_dir>/deps``.

    Single source of truth for where ``deps install`` writes and where
    :func:`add_deps_dir_to_sys_path` looks, so the two can never drift apart.
    """
    return Path(state_dir).expanduser() / DEPS_SUBDIR


def extra_names() -> tuple[str, ...]:
    """Return the distinct pip-extra names in registry order (e.g. ``("pty", "notify")``)."""
    ordered: list[str] = []
    for entry in EXTRAS:
        if entry.extra_name not in ordered:
            ordered.append(entry.extra_name)
    return tuple(ordered)


def extras_for(extra_name: str) -> tuple[Extra, ...]:
    """Return every registered :class:`Extra` belonging to ``extra_name`` (all platforms).

    Platform filtering is the caller's job (via :func:`applies`) so ``deps list`` can show a
    Windows-only entry on Linux while ``deps install`` skips it — see the two call sites.
    """
    return tuple(entry for entry in EXTRAS if entry.extra_name == extra_name)


def canonical_name(name: str) -> str:
    """Return a PEP 503-canonicalised distribution name for case/separator-insensitive matching.

    Mirrors PEP 503 normalisation (lowercase; runs of ``-``, ``_``, ``.`` collapse to a
    single ``-``) so a RECORD's ``Name`` matches our registry's ``dist`` regardless of how
    the wheel spelled it.
    """
    out = []
    prev_dash = False
    for ch in name.lower():
        if ch in "-_.":
            if not prev_dash:
                out.append("-")
            prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out).strip("-")


def add_deps_dir_to_sys_path(state_dir: str | Path) -> None:
    """Append the managed deps dir to ``sys.path`` so side-installed extras import (frozen only).

    Generalises the ``pyte`` env-var shim (``pty_screen._maybe_add_external_pyte_path``) to the
    managed ``<state_dir>/deps`` directory that ``clauster deps install`` populates. Frozen-only:
    a normal pip/uv install resolves extras through site-packages, so the managed dir is consulted
    only for the standalone binary, which ignores site-packages. APPEND (never prepend) so a
    bundled/installed copy always wins and the side-install is a fallback — matching the pyte shim.
    Best-effort: a missing dir or an ``expanduser``/OS error is swallowed, never raised from the
    startup path.
    """
    if not is_frozen():
        return
    try:
        target = managed_deps_dir(state_dir)
        if not target.is_dir():
            return
        path_str = str(target)
    except (OSError, RuntimeError):
        return
    if path_str not in sys.path:
        sys.path.append(path_str)


def _dist_name(dist: importlib.metadata.Distribution) -> str | None:
    """Return a distribution's ``Name`` header, or ``None`` if absent.

    Reads through the underlying :class:`email.message.Message` via ``.get`` — the safe accessor
    that returns ``None`` for a missing header. (``metadata["Name"]`` returns ``None`` too but is
    deprecated for it, and ``PackageMetadata`` doesn't type ``.get`` — hence the cast.)
    """
    return cast("Message", dist.metadata).get("Name")


def installed_versions(state_dir: str | Path) -> dict[str, str]:
    """Return ``{canonical dist name: version}`` for distributions in the managed deps dir.

    Reads wheel metadata from ``<state_dir>/deps`` via :func:`importlib.metadata.distributions`
    (scoped to that path, so nothing from the ambient environment leaks in). An absent directory
    yields ``{}``; a distribution with no ``Name`` is skipped rather than crashing the listing.
    """
    target = managed_deps_dir(state_dir)
    if not target.is_dir():
        return {}
    found: dict[str, str] = {}
    for dist in importlib.metadata.distributions(path=[str(target)]):
        name = _dist_name(dist)
        if name:
            found[canonical_name(name)] = dist.version
    return found


def _default_pip_main(argv: list[str]) -> int:
    """Run pip in-process via its private CLI entry, returning pip's exit code.

    Uses ``pip._internal.cli.main.main`` rather than ``runpy.run_module("pip")``: even once pip
    is bundled, PyInstaller collects pip's modules but not the dynamically imported
    ``pip.__main__``, so ``runpy`` fails in-frozen while the private entry works both frozen and
    unfrozen (spike, 2026-07-14). The frozen binary does NOT bundle pip yet — that arrives with
    the ``--collect-all pip`` spec change in #904 slice 2b — so on today's binary this raises
    :class:`DepsPipUnavailableError`, which the caller turns into a clean error + fallback hint.
    The private API is pinned at build time — guard it.
    """
    # import_module (not a static import) keeps pip out of the declared dependency graph:
    # it's bundled into the frozen binary at build time and present in a dev env, but is not
    # a runtime requirement of the wheel, so a static import would be an unresolved reference.
    try:
        pip_cli = importlib.import_module("pip._internal.cli.main")
    except ImportError as exc:  # pip genuinely absent (stripped/older frozen build)
        raise DepsPipUnavailableError(
            "pip is unavailable to install extras — reinstall Clauster from a Python "
            "environment with pip, or use `pip install 'clauster[...]'` directly."
        ) from exc
    return int(pip_cli.main(argv))


def install_extra(
    extra_name: str,
    state_dir: str | Path,
    *,
    assume_yes: bool = False,
    pip_main: Callable[[list[str]], int] | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    """Side-install ``extra_name``'s wheels into the managed deps dir; return an exit code.

    Prints the :data:`PROVENANCE_NOTICE` and requires confirmation (unless ``assume_yes``) before
    fetching anything — never auto-installs. Platform-irrelevant entries are skipped via
    :func:`applies` (so ``deps install pty`` pulls only ``pyte`` on Linux, ``pyte`` + ``pywinpty``
    on Windows). ``pip_main``/``confirm`` are seams for testing. Exit codes: ``2`` unknown extra,
    ``1`` declined / mkdir or pip failure, ``0`` installed.
    """
    if extra_name not in extra_names():
        _err(f"unknown extra {extra_name!r}; choose from {', '.join(extra_names())}")
        return 2
    dists = [entry.dist for entry in extras_for(extra_name) if applies(entry)]
    target = managed_deps_dir(state_dir)
    print(PROVENANCE_NOTICE, file=sys.stderr)
    _err(f"will install {', '.join(dists)} into {target}")
    if not assume_yes:
        try:
            reply = confirm("Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            # No usable stdin (piped/closed) or Ctrl-C: fail closed — treat as a decline.
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            _err("aborted — nothing installed")
            return 1
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _err(f"could not create {target}: {exc}")
        return 1
    runner = pip_main or _default_pip_main
    # --upgrade so a re-install into an existing --target dir refreshes rather than erroring.
    argv = ["install", "--target", str(target), "--upgrade", *dists]
    try:
        rc = runner(argv)
    except DepsPipUnavailableError as exc:
        _err(str(exc))
        return 1
    if rc != 0:
        _err(f"pip install failed (exit {rc})")
        return 1
    _err(f"installed {extra_name} into {target} — restart Clauster to load the new capability.")
    return 0


def uninstall_extra(extra_name: str, state_dir: str | Path) -> int:
    """Remove ``extra_name``'s distributions from the managed deps dir; return an exit code.

    pip can't uninstall from a ``--target`` directory, so removal is manual: each matching
    distribution's RECORD files are deleted and the emptied directories pruned (never above the
    managed dir). Only the extra's own top-level distribution(s) are removed — shared/transitive
    dependencies stay (removing them safely needs a dependency graph; the bulk uninstaller that
    clears the whole managed dir is #904 slice 2b). Exit codes: ``2`` unknown extra, ``1`` when a
    matched distribution has no RECORD manifest so its files couldn't be removed, ``0`` otherwise
    (removing an absent extra is a no-op, not an error — the end state already holds).
    """
    if extra_name not in extra_names():
        _err(f"unknown extra {extra_name!r}; choose from {', '.join(extra_names())}")
        return 2
    target = managed_deps_dir(state_dir)
    wanted = {canonical_name(entry.dist) for entry in extras_for(extra_name)}
    removed: list[str] = []
    incomplete: list[str] = []
    if target.is_dir():
        for dist in importlib.metadata.distributions(path=[str(target)]):
            name = _dist_name(dist)
            if name and canonical_name(name) in wanted:
                (removed if _remove_distribution(dist, target) else incomplete).append(name)
    if incomplete:
        # Couldn't fully remove (no RECORD manifest, or a file was locked/permission-denied), so
        # files remain and would reload — we must NOT claim success. Surface loudly + fail (#933).
        _err(
            f"could not fully remove {', '.join(incomplete)} from {target} (no RECORD manifest, "
            f"or a file was locked/undeletable); delete any leftover files under {target} by hand."
        )
    if removed:
        _err(f"removed {', '.join(removed)} from {target}")
    if not removed and not incomplete:
        _err(f"{extra_name} is not installed in {target}")
    return 1 if incomplete else 0


def _remove_distribution(dist: importlib.metadata.Distribution, target: Path) -> bool:
    """Delete every RECORD file of ``dist`` under ``target``, prune emptied dirs; return success.

    Returns ``True`` only when the distribution was fully removed. ``False`` when it has no RECORD
    manifest to act on, OR when a manifest file survives the unlink (locked — e.g. a running
    process holds a Windows ``.pyd`` — or permission-denied): the caller must not then claim the
    extra was removed, since files remain on disk and would reload. An already-absent file is fine
    (nothing left behind); empty parent dirs are pruned bottom-up, never climbing above ``target``.
    """
    files = dist.files
    if not files:
        return False
    dirs: set[Path] = set()
    boundary = target.resolve()
    left_behind = False
    for rel in files:
        located = Path(str(dist.locate_file(rel)))
        # Containment guard: a tampered RECORD could list a `../`-escaping path. Never unlink
        # anything that resolves outside the managed dir — mirrors _prune_empty_dirs' boundary
        # (defense-in-depth; the provenance gate is the first line, this is the second).
        try:
            resolved = located.resolve()
        except OSError:
            # Can't even resolve the entry (e.g. a symlink loop) → can't confirm it's gone, so
            # don't claim full removal. Fail closed rather than silently skip a maybe-present file.
            left_behind = True
            continue
        if boundary != resolved and boundary not in resolved.parents:
            continue  # escapes the managed dir — not ours to remove
        try:
            located.unlink()
        except FileNotFoundError:
            pass  # already gone — nothing left behind
        except OSError:
            # Locked (e.g. a running process holds a Windows .pyd) or permission-denied: the
            # file SURVIVES, so the extra is NOT fully removed. Record it and don't claim success.
            left_behind = True
            continue
        dirs.add(located.parent)
    for directory in sorted(dirs, key=lambda d: len(str(d)), reverse=True):
        _prune_empty_dirs(directory, stop=target)
    return not left_behind


def _prune_empty_dirs(directory: Path, *, stop: Path) -> None:
    """Remove ``directory`` and empty parents, walking up but never past ``stop``."""
    try:
        current = directory.resolve()
        boundary = stop.resolve()
    except OSError:
        return
    while current != boundary and boundary in current.parents:
        try:
            current.rmdir()  # only succeeds on an empty directory
        except OSError:
            break
        current = current.parent


def _err(message: str) -> None:
    """Print a ``clauster:``-prefixed message to stderr (CLI convention: prose on stderr)."""
    print(f"clauster: {message}", file=sys.stderr)
