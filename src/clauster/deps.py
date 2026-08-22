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

import hashlib
import importlib.metadata
import importlib.util
import io
import platform
import shutil
import sys
import tarfile
import zipfile
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


def host_arch() -> str:
    """Return this host's CPU arch normalised to a release-asset token (``x86_64``/``arm64``).

    ``platform.machine()`` reports platform-specific spellings — ``x86_64``/``AMD64`` for 64-bit
    Intel, ``aarch64``/``arm64``/``ARM64`` for 64-bit ARM — that GoReleaser (claustrum) collapses
    to ``x86_64``/``arm64``. An unrecognised machine returns its lowercased raw value, which
    matches no registered variant (an unsupported arch resolves to "no build", never a wrong one).
    """
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def applies(entry: Extra | BinaryDep) -> bool:
    """Return whether ``entry`` (an :class:`Extra` or :class:`BinaryDep`) is relevant here.

    A ``None`` platform marker applies everywhere; otherwise the entry is only relevant when its
    marker matches :data:`sys.platform` (e.g. ``pywinpty``/``shawl`` on ``"win32"``). A
    :class:`BinaryDep` may additionally pin ``arch_marker`` — then the host arch must also match
    (:func:`host_arch`) — so doctor and the UI don't nag a Linux host about a Windows-only binary,
    nor offer an arm64 host the x86_64 archive.
    """
    if entry.platform_marker is not None and sys.platform != entry.platform_marker:
        return False
    arch = getattr(entry, "arch_marker", None)  # Extra has no arch_marker -> arch-agnostic
    return arch is None or host_arch() == arch


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

    The frozen binary bundles pip (``clauster.spec`` ``collect_all("pip")``), so this normally
    can't happen there; it surfaces only on a stripped/older build — or a non-frozen environment
    that lacks pip — with a clear fallback hint.
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
    the wheel spelled it. Leading/trailing dashes are additionally stripped, which PEP 503
    itself does not do (``_foo_`` → ``foo`` here, ``-foo-`` per the spec).
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

    Uses ``pip._internal.cli.main.main`` rather than ``runpy.run_module("pip")``: PyInstaller
    collects pip's modules (via ``clauster.spec`` ``collect_all("pip")``) but not the dynamically
    imported ``pip.__main__``, so ``runpy`` fails in-frozen while the private entry works both
    frozen and unfrozen (spike, 2026-07-14). The frozen binary bundles pip, so this resolves there;
    a stripped/older build (or a non-frozen env without pip) raises
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
    matched distribution could not be fully removed (no RECORD manifest, or a file survived the
    unlink — locked or permission-denied), ``0`` otherwise (removing an absent extra is a no-op,
    not an error — the end state already holds).
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
            # The file survives a lock/permission error, so the extra is NOT fully removed
            # (see docstring). Record it and don't claim success.
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


# ----- managed binary dependencies (#904 slice 2b): Shawl, the Windows service wrapper -------
#
# Some clauster capabilities need a standalone *binary*, not a pip extra. The Windows
# ``install-service`` path wraps clauster as a service with Shawl (mtkennerly/shawl on GitHub,
# MIT) — a single .exe. ``clauster deps install shawl`` fetches the pinned GitHub release, refuses
# it unless it matches a hardcoded SHA-256, and places ``shawl.exe`` under ``<state_dir>/deps/bin``
# where install-service points the service at it. This is a download-verify-place, not pip — but it
# shares the managed dir + provenance gate with the extras.

BIN_SUBDIR = "bin"

#: Shown before a binary side-install. Unlike the pip extras, the artifact is pinned to an exact
#: SHA-256 (so it can't silently change), but it is still a third-party binary fetched over the
#: network and not covered by the Clauster release signature — so we still notice + confirm.
PROVENANCE_NOTICE_BINARY = (
    "This downloads a third-party binary from its GitHub release and checks it against a pinned\n"
    "SHA-256. It is NOT covered by the Clauster release signature — you are trusting that\n"
    "project's release, pinned to the exact build below."
)


@dataclass(frozen=True)
class BinaryDep:
    """One managed standalone-binary dependency: a pinned release archive + the exe inside it.

    ``platform_marker`` mirrors :class:`Extra` (a :data:`sys.platform` value, e.g. ``"win32"``),
    so :func:`applies` gates it off-platform. ``arch_marker`` is a normalised CPU arch
    (``"x86_64"``/``"arm64"``, see :func:`host_arch`) or ``None`` for arch-agnostic binaries
    (Shawl); a multi-arch tool (claustrum) registers one entry per (platform, arch). ``url``/
    ``sha256`` pin an exact release archive; ``member`` is the file to extract from that archive
    (``.zip`` or ``.tar.gz``, chosen by the url suffix) and ``dest`` its filename under the managed
    ``bin`` dir. Bumping the version means updating ``version``/``url``/``sha256`` together.
    """

    key: str
    label: str
    platform_marker: str
    version: str
    url: str
    sha256: str
    member: str
    dest: str
    arch_marker: str | None = None


# The Shawl release the Windows service wrapper is pinned to. Renovate watches
# mtkennerly/shawl (github-releases) via the customManager keyed on this `# renovate:`
# line (renovate.json, #934) and bumps ONLY this constant — the download URL derives
# from it, so version + URL can never drift out of sync. The `sha256` below is NOT
# auto-updated (the hosted Renovate App reads github-releases' git-tag digest, not the
# release ASSET's file checksum), so a version-bump PR carries a stale hash until it is
# refreshed from GitHub's own published asset digest (`scripts/check_binary_dep_pins.py`
# verifies each pin against that digest and prints the correct value; and `deps install`
# fail-closes on a mismatch, so a stale hash can never silently ship — it just refuses).
# renovate: datasource=github-releases depName=mtkennerly/shawl
_SHAWL_VERSION = "v1.9.0"

# The claustrum Direct Session daemon (schubydoo/claustrum, Apache-2.0) — a GoReleaser Go binary
# shipped as one archive per (OS, arch): Linux/Darwin as `.tar.gz`, Windows as `.zip`, with the
# binary at the archive root (`claustrum` / `claustrum.exe`). We register one BinaryDep per variant
# (all keyed "claustrum"); :func:`applies` selects the row matching this host's (sys.platform,
# host_arch()). The version is a single Renovate-bumped constant the URLs derive from; the sha256s
# are the release's checksums.txt asset digests (refreshed by scripts/check_binary_dep_pins.py and
# fail-closed at install, exactly like Shawl above). Same `# renovate:` customManager pattern.
# renovate: datasource=github-releases depName=schubydoo/claustrum
_CLAUSTRUM_VERSION = "v1.9.0"
_CLAUSTRUM_VER_BARE = _CLAUSTRUM_VERSION.removeprefix("v")  # tag "v1.7.1" -> asset infix "1.7.1"

# (platform_marker, GoReleaser OS token, arch_marker, archive ext, sha256 of the archive)
_CLAUSTRUM_VARIANTS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "linux",
        "Linux",
        "x86_64",
        "tar.gz",
        "3a843cc4c6b46b29443c3f4dbcc2e8add31aa9769a60e1add7cc0380c6e16cda",
    ),
    (
        "linux",
        "Linux",
        "arm64",
        "tar.gz",
        "d9d8b44bc47f3c398db7def9c5d8d401705fa769ef7efecf1711731045a828d0",
    ),
    (
        "darwin",
        "Darwin",
        "x86_64",
        "tar.gz",
        "ef61cfcc72aeeeb2eafd22ff522145e60b522c49bd1b637309f3b2a58caefeaf",
    ),
    (
        "darwin",
        "Darwin",
        "arm64",
        "tar.gz",
        "fa705d05f4a732999b94e0910dc1702ae61024f69d9bbbe6dbd44be8b6b79c91",
    ),
    (
        "win32",
        "Windows",
        "x86_64",
        "zip",
        "b84fcee5679e1085d103a29a8fad31d0e35f6eacb53aaf6b4cdb28bd09948b05",
    ),
    (
        "win32",
        "Windows",
        "arm64",
        "zip",
        "bc41899916d870d05c9410c01e01852441b51f7bb6d5cb079a21b20637d0b71b",
    ),
)


def _claustrum_deps() -> tuple[BinaryDep, ...]:
    """Build the per-(OS, arch) claustrum :class:`BinaryDep` variants from the variant table."""
    deps = []
    for platform_marker, os_name, arch, ext, sha256 in _CLAUSTRUM_VARIANTS:
        member = "claustrum.exe" if os_name == "Windows" else "claustrum"
        deps.append(
            BinaryDep(
                key="claustrum",
                label="Direct Session daemon (claustrum)",
                platform_marker=platform_marker,
                arch_marker=arch,
                version=_CLAUSTRUM_VERSION,
                url=(
                    f"https://github.com/schubydoo/claustrum/releases/download/{_CLAUSTRUM_VERSION}"
                    f"/claustrum_{_CLAUSTRUM_VER_BARE}_{os_name}_{arch}.{ext}"
                ),
                sha256=sha256,
                member=member,
                dest=member,
            )
        )
    return tuple(deps)


BINARY_DEPS: tuple[BinaryDep, ...] = (
    BinaryDep(
        key="shawl",
        label="Windows service wrapper (Shawl)",
        platform_marker="win32",
        version=_SHAWL_VERSION,
        url=(
            f"https://github.com/mtkennerly/shawl/releases/download/"
            f"{_SHAWL_VERSION}/shawl-{_SHAWL_VERSION}-win64.zip"
        ),
        sha256="f883c5d09c9beae2efaeabd8513e7d3f57cd1d0864cec3df4f4a7b6ee904351c",
        member="shawl.exe",
        dest="shawl.exe",
    ),
    *_claustrum_deps(),
)


def binary_dep_names() -> tuple[str, ...]:
    """Return the registered managed-binary keys, de-duplicated (e.g. ``("shawl", "claustrum")``).

    A multi-arch binary (claustrum) has several :data:`BINARY_DEPS` rows under one key; the CLI
    choices and doctor want the key once, so collapse duplicates while preserving order.
    """
    seen: list[str] = []
    for dep in BINARY_DEPS:
        if dep.key not in seen:
            seen.append(dep.key)
    return tuple(seen)


def binary_dep_for(key: str) -> BinaryDep:
    """Return the first :class:`BinaryDep` row for ``key`` (raises ``KeyError`` if unknown).

    For a multi-arch binary this is any row (used only for key-agnostic fields like ``label``);
    to pick the row for *this* host use :func:`resolve_binary_dep`.
    """
    for dep in BINARY_DEPS:
        if dep.key == key:
            return dep
    raise KeyError(key)


def resolve_binary_dep(key: str) -> BinaryDep | None:
    """Return the :class:`BinaryDep` row for ``key`` that :func:`applies` here, else ``None``.

    ``None`` means the key is unknown OR there is no build for this (platform, arch) — callers
    treat both as "not installable/available here" (a clean skip, never a wrong-arch install).
    """
    for dep in BINARY_DEPS:
        if dep.key == key and applies(dep):
            return dep
    return None


def managed_bin_dir(state_dir: str | Path) -> Path:
    """Return the managed binary directory ``<state_dir>/deps/bin`` (holds e.g. ``shawl.exe``)."""
    return managed_deps_dir(state_dir) / BIN_SUBDIR


def claustrum_pinned_version() -> str:
    """Return the claustrum release version clauster pins/ships (e.g. ``v1.7.1``).

    The advisory compatibility floor for the Direct Session daemon (#1013): the doctor
    version check WARNs when the running/configured binary can't be confirmed at or above it.
    """
    return _CLAUSTRUM_VERSION


def installed_binary_path(key: str, state_dir: str | Path) -> Path | None:
    """Return the managed path of binary ``key`` if it is installed here, else ``None``.

    Uses :func:`resolve_binary_dep` so the dest matches this host's variant (``claustrum`` vs
    ``claustrum.exe``); an unknown key or unsupported platform/arch reads as not-installed.
    """
    dep = resolve_binary_dep(key)
    if dep is None:
        return None
    dest = managed_bin_dir(state_dir) / dep.dest
    return dest if dest.is_file() else None


def resolve_effective_binary(
    key: str, configured: str, default: str, state_dir: str | Path
) -> str | None:
    """Resolve the binary a configurable dep would actually run, or ``None`` (#1013).

    Mirrors the precedence the claustrum daemon spawns with, so a presence check and the
    daemon can never disagree *by construction*: an explicit or ``PATH`` hit on the
    *configured* value wins, and the managed ``<state_dir>/deps/bin`` install is a fallback
    ONLY while the configured value is still the ``default`` — an operator who pointed
    ``binary`` at a specific path must see it fail rather than have a different version
    silently substituted. Pure/read-only (a ``shutil.which`` plus one file stat).
    """
    resolved = shutil.which(configured)
    if resolved is not None:
        return resolved
    if configured == default:
        managed = installed_binary_path(key, state_dir)
        if managed is not None:
            return str(managed)
    return None


#: Upper bound on a managed-binary download (Shawl's win64 zip is ~1.3 MB). A body larger than this
#: is truncated by the capped read → its SHA-256 won't match → refused; the cap only bounds memory
#: so a misbehaving (but cert-valid) endpoint can't stream an unbounded body in.
_MAX_FETCH_BYTES = 64 * 1024 * 1024


def _default_fetch(url: str) -> bytes:
    """Fetch ``url`` over HTTPS and return the body (a monkeypatchable seam for tests).

    The URL is a hardcoded ``https`` GitHub release constant (never user input) and the payload is
    SHA-256-verified by the caller, so an ``urlopen`` here is not an injection/SSRF surface. The
    read is capped at :data:`_MAX_FETCH_BYTES` to bound memory (an over-cap body fails the sha256).
    """
    import urllib.request

    if not url.startswith("https://"):  # defensive: only ever fetch our pinned https release URLs
        raise ValueError(f"refusing to fetch non-https url: {url}")
    with urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - pinned https, sha256-verified
        return resp.read(_MAX_FETCH_BYTES)


def _extract_member(data: bytes, url: str, member: str) -> bytes:
    """Return the bytes of a single ``member`` from a ``.zip`` or ``.tar.gz`` archive in memory.

    Format is chosen by ``url`` suffix (GoReleaser ships Linux/Darwin as ``.tar.gz``, Windows as
    ``.zip``). Only the ONE named member is read — never ``extractall`` — and nothing is written to
    disk here (the caller places the returned bytes atomically at a fixed ``dest``), so there is no
    traversal/symlink/device escape onto the host regardless of member type. The real integrity
    control is the caller's SHA-256 pin, verified BEFORE this runs — the checks here are
    defense-in-depth on an already-trusted archive: we insist the tar member is a **regular file**
    (a dir/symlink/hardlink squatting the name is refused, rather than silently following a link to
    some other in-archive entry's bytes). A missing member raises ``KeyError``; a non-regular tar
    member raises ``ValueError``. The caller (:func:`install_binary_dep`) catches both plus
    ``TarError``/``BadZipFile``.
    """
    if url.endswith((".tar.gz", ".tgz")):
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            info = archive.getmember(member)  # KeyError if absent
            if info.issym() or info.islnk():  # a link would be FOLLOWED to another entry's bytes
                raise ValueError(f"{member!r} is a link, not a regular file, in the archive")
            extracted = archive.extractfile(info)
            if extracted is None:  # a dir / device / fifo entry squatting the name
                raise ValueError(f"{member!r} is not a regular file in the archive")
            return extracted.read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(member)


def install_binary_dep(
    key: str,
    state_dir: str | Path,
    *,
    assume_yes: bool = False,
    fetch: Callable[[str], bytes] | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    """Download + verify + place a managed binary (e.g. ``shawl``); return an exit code.

    Fetches the pinned release archive, refuses it unless its SHA-256 matches the hardcoded pin,
    extracts the exe into ``<state_dir>/deps/bin`` and marks it executable (no-op semantics on
    Windows; a chmod failure aborts the install like any other write error). Prints
    the provenance notice + requires confirmation unless ``assume_yes``; ``fetch``/``confirm`` are
    test seams. Exit codes: ``2`` unknown/off-platform, ``1`` declined / download / checksum /
    extract / write failure, ``0`` installed.
    """
    if key not in binary_dep_names():
        _err(f"unknown binary {key!r}; choose from {', '.join(binary_dep_names())}")
        return 2
    dep = resolve_binary_dep(key)
    if dep is None:
        _err(
            f"no {key} build for this platform/arch ({sys.platform}/{host_arch()}); "
            "nothing to install"
        )
        return 2
    dest = managed_bin_dir(state_dir) / dep.dest
    print(PROVENANCE_NOTICE_BINARY, file=sys.stderr)
    _err(f"will download {dep.label} {dep.version} and install it at {dest}")
    if not assume_yes:
        try:
            reply = confirm("Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            _err("aborted — nothing installed")
            return 1
    runner = fetch or _default_fetch
    try:
        data = runner(dep.url)
    except (OSError, ValueError) as exc:
        _err(f"download failed: {exc}")
        return 1
    actual = hashlib.sha256(data).hexdigest()
    if actual != dep.sha256:
        _err(f"checksum mismatch for {dep.url}: expected {dep.sha256}, got {actual} — refusing")
        return 1
    try:
        payload = _extract_member(data, dep.url, dep.member)
    except (KeyError, tarfile.TarError, zipfile.BadZipFile, OSError, ValueError) as exc:
        _err(f"could not extract {dep.member} from the archive: {exc}")
        return 1
    # Atomic install: write to a sibling temp then replace, so a partial write (e.g. a full disk)
    # never truncates a previously-working binary — the old one stays until the swap succeeds.
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload)
        tmp.chmod(0o755)  # no-op semantics on Windows; harmless if a POSIX host ever fetches it
        tmp.replace(dest)  # os.replace — atomic within the managed dir (same filesystem)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)  # don't leave a half-written temp behind
        except OSError:
            pass  # best-effort cleanup — the original write error is what we report
        _err(f"could not write {dest}: {exc}")
        return 1
    _err(f"installed {dep.label} {dep.version} at {dest}")
    return 0


def uninstall_binary_dep(key: str, state_dir: str | Path) -> int:
    """Remove a managed binary (e.g. ``shawl``) from ``<state_dir>/deps/bin``; return an exit code.

    Exit codes: ``2`` unknown binary, ``1`` when the file exists but can't be unlinked
    (locked/permission-denied), ``0`` otherwise (removing an absent binary, or one with no build
    for this platform/arch, is a no-op).
    """
    if key not in binary_dep_names():
        _err(f"unknown binary {key!r}; choose from {', '.join(binary_dep_names())}")
        return 2
    dep = resolve_binary_dep(key)
    if dep is None:  # no build for this platform/arch → nothing could have been installed here
        _err(f"{key} has no build for this platform/arch — nothing to remove")
        return 0
    dest = managed_bin_dir(state_dir) / dep.dest
    try:
        dest.unlink()
    except FileNotFoundError:
        _err(f"{key} is not installed in {dest.parent}")
        return 0
    except OSError as exc:
        _err(f"could not remove {dest}: {exc}")
        return 1
    _prune_empty_dirs(dest.parent, stop=managed_deps_dir(state_dir))
    _err(f"removed {key} from {dest.parent}")
    return 0
