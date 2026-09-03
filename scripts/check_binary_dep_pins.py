#!/usr/bin/env python3
"""Verify — or, with ``--fix``, refresh — each pinned release-asset sha256 in this repo.

Renovate can bump the ``_*_VERSION`` constant + derived URL of a managed binary
(:mod:`clauster.deps`), but the hosted Renovate App cannot rewrite the source-pinned
``sha256`` — the ``github-releases`` datasource exposes the git-tag commit digest, not the
release *asset's* file checksum. GitHub itself, however, now publishes a per-asset SHA-256
(the ``digest`` field on a release asset), so this script closes the loop: it fetches that
published digest for each pinned asset and asserts it equals the pinned ``sha256``.

Two pin sources, checked the same way:

* :data:`clauster.deps.BINARY_DEPS` — the managed standalone-binary pins (Shawl, claustrum).
* :data:`_WORKFLOW_PINS` — the ``*_VERSION`` + ``*_SHA256`` pairs embedded in the GitHub Actions
  workflow files that download a release binary inline (``osv-scanner.yml``, ``actionlint.yml``).
  These carry the same ``# renovate:`` annotation, so self-hosted Renovate CE bumps the version
  but likewise can't rewrite the asset sha256 — ``--fix`` rewrites it in the workflow file.

Two modes:

* **verify** (default) — a stale hash fails loudly and the script prints the correct value to
  paste. This is what the ``binary-dep-pins`` CI workflow runs; it is platform-independent and
  never mutates the tree.
* **``--fix``** — the same check, but a stale hash is *rewritten in place* (from GitHub's
  published digest) instead of merely reported — in ``deps.py`` for a BINARY_DEPS pin, or in the
  workflow file for a workflow pin. This is what the self-hosted Renovate CE runs as a
  ``postUpgradeTask`` on a version bump, so the refreshed hash lands in the same branch/commit as
  the bump — no human paste step. Fetch/lookup errors still fail loudly in both modes
  (fail-closed): a bump that can't be verified never silently "passes".

To keep ``--fix`` runnable under a bare ``python3`` (Renovate CE's postUpgradeTask has no venv
and doesn't ``pip install`` clauster), the in-repo ``src/`` is put on ``sys.path`` before the
import — ``deps.py`` and ``clauster/__init__`` are stdlib-only, so no ``uv sync`` is needed. The
workflow pins import nothing (they read the workflow files directly), so they stay stdlib-only too.

Exit codes: ``0`` every pin verified (or, with ``--fix``, now reconciled), or
genuinely-unverifiable-but-not-wrong; ``1`` a pin's ``sha256`` disagrees with GitHub's published
digest (verify mode), or a pin could not be fetched/located/parsed to verify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_RELEASE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
    r"(?P<tag>[^/]+)/(?P<asset>[^/]+)$"
)


@dataclass(frozen=True)
class _WorkflowPin:
    """One ``*_VERSION`` + ``*_SHA256`` release-asset pin embedded in a workflow file.

    A GitHub Actions workflow that downloads a release binary inline pins it by version and by the
    asset's sha256 (``sha256sum -c`` fails the job on a mismatch). Renovate CE bumps ``version_re``
    via the ``# renovate:`` annotation but cannot rewrite ``sha_re`` — the same gap this script
    closes for :data:`clauster.deps.BINARY_DEPS`. ``tag`` derives the release tag from the pinned
    version and ``asset`` derives the downloaded asset's name, so both track a bump with no drift.
    """

    path: str  # workflow file, relative to the repo root
    owner: str
    repo: str
    version_re: re.Pattern[str]  # one capture group: the pinned version value
    sha_re: re.Pattern[str]  # one capture group: the current 64-hex sha256 (the rewrite target)
    tag: Callable[[str], str]  # version -> release tag (e.g. "1.7.12" -> "v1.7.12")
    asset: Callable[[str], str]  # version -> downloaded asset name


_WORKFLOW_PINS: tuple[_WorkflowPin, ...] = (
    # osv-scanner.yml pins the linux amd64 binary directly (a constant asset name). OSV_VERSION
    # already carries the leading "v", so the release tag is the version verbatim (e.g. "v2.5.1").
    _WorkflowPin(
        path=".github/workflows/osv-scanner.yml",
        owner="google",
        repo="osv-scanner",
        version_re=re.compile(r'OSV_VERSION:\s*"([^"]+)"'),
        sha_re=re.compile(r'OSV_SHA256:\s*"([0-9a-f]{64})"'),
        tag=lambda v: v,
        asset=lambda v: "osv-scanner_linux_amd64",
    ),
    # actionlint.yml pins the linux amd64 tarball, whose name embeds the version. The version is
    # bare (e.g. "1.7.12"), so the tag is "v" + version and the asset name is built the same way
    # the workflow's run-script builds its download URL (actionlint_${VERSION}_linux_amd64.tar.gz).
    _WorkflowPin(
        path=".github/workflows/actionlint.yml",
        owner="rhysd",
        repo="actionlint",
        version_re=re.compile(r'ACTIONLINT_VERSION:\s*"([^"]+)"'),
        sha_re=re.compile(r'ACTIONLINT_SHA256:\s*"([0-9a-f]{64})"'),
        tag=lambda v: f"v{v}",
        asset=lambda v: f"actionlint_{v}_linux_amd64.tar.gz",
    ),
)


def _load_deps() -> tuple[tuple, Path]:
    """Import :mod:`clauster.deps` and return ``(BINARY_DEPS, path to deps.py)``.

    Puts the in-repo ``src/`` on ``sys.path`` first so a bare ``python3`` (Renovate CE's
    postUpgradeTask, where clauster isn't installed) can import it — ``deps.py`` pulls only
    stdlib and ``clauster/__init__`` is import-cheap, so no venv/``uv sync`` is required. The
    returned path is where ``--fix`` writes, so it always targets the checked-out source file.
    """
    src = Path(__file__).resolve().parent.parent / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from clauster import deps

    return deps.BINARY_DEPS, Path(deps.__file__)


def _fetch_release(owner: str, repo: str, tag: str) -> dict:
    """Return the GitHub release JSON for ``owner/repo`` at ``tag`` (raises on failure)."""
    api = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
    # noqa justification: fixed https://api.github.com host; owner/repo/tag come from a
    # BINARY_DEPS URL that already matched the github.com releases-download regex above.
    req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json"})  # noqa: S310
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https api host
        return json.load(resp)


def _find_asset(release: dict, asset_name: str) -> dict | None:
    """Return the release asset object named ``asset_name``, or None if the release has none."""
    for asset in release.get("assets", []):
        if isinstance(asset, dict) and asset.get("name") == asset_name:
            return asset
    return None


def _resolve_digest(
    owner: str, repo: str, tag: str, asset: str, current_sha: str
) -> tuple[str, str | None]:
    """Resolve one release asset's published digest against ``current_sha``.

    Shared network/lookup core for both pin sources (BINARY_DEPS and the workflow pins). Returns
    ``(status, value)`` where ``status`` is one of:

    * ``"ok"``       — the pin matches; ``value`` is ``None``.
    * ``"mismatch"`` — the pin is stale; ``value`` is the correct sha256 to write.
    * ``"warn"``     — asset exists but GitHub publishes no sha256; ``value`` is a reason.
    * ``"error"``    — could not fetch/locate the asset, or GitHub published a malformed
      (non-64-hex) digest; ``value`` is the failure detail.

    Errors are surfaced (never swallowed) so the caller can fail-closed — this runs only when a
    pinned file changed, so an unverifiable pin must not pass.
    """
    try:
        release = _fetch_release(owner, repo, tag)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return "error", f"could not fetch {owner}/{repo}@{tag} to verify: {exc}"
    asset_obj = _find_asset(release, asset)
    if asset_obj is None:
        names = ", ".join(
            a.get("name", "?") for a in release.get("assets", []) if isinstance(a, dict)
        )
        return "error", f"release {tag} has no asset named {asset} (has: {names})"
    digest = asset_obj.get("digest")
    if not (isinstance(digest, str) and digest.startswith("sha256:")):
        return "warn", f"GitHub publishes no sha256 for {asset} — pinned {current_sha}"
    published = digest.removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", published) is None:
        # A malformed digest (empty / non-hex / wrong length) is NOT a legitimate "new" pin:
        # writing it would replace a good hash with a value that always fails install-time
        # verification. Refuse it — fail-closed, same as an unverifiable pin.
        return "error", f"GitHub published a malformed sha256 for {asset}: {digest!r}"
    if published == current_sha:
        return "ok", None
    return "mismatch", published


def _published_digest(dep) -> tuple[str, str | None]:  # noqa: ANN001 - dep is a clauster.deps.BinaryDep
    """Resolve one BINARY_DEPS pin against GitHub's published asset digest.

    Parses the ``owner/repo/tag/asset`` out of the pin's release-download URL, then delegates to
    :func:`_resolve_digest`. Adds one extra status over that helper:

    * ``"skip"`` — not a github releases-download URL; ``value`` is a reason to warn+pass.
    """
    match = _RELEASE_URL_RE.match(dep.url)
    if not match:
        return "skip", f"not a github releases-download URL, skipping: {dep.url}"
    owner, repo, tag, asset = (match.group(k) for k in ("owner", "repo", "tag", "asset"))
    return _resolve_digest(owner, repo, tag, asset, dep.sha256)


def _repo_root() -> Path:
    """Return the repository root (this script lives in ``<root>/scripts/``)."""
    return Path(__file__).resolve().parent.parent


def _load_workflow_pins() -> tuple[tuple[_WorkflowPin, ...], Path]:
    """Return ``(_WORKFLOW_PINS, repo root)`` — the workflow pin source and where they live.

    A seam mirroring :func:`_load_deps` so tests can point the workflow-pin source at a synthetic
    table + throwaway tree instead of the real workflow files.
    """
    return _WORKFLOW_PINS, _repo_root()


def _resolve_workflow_pin(pin: _WorkflowPin, root: Path) -> tuple[str, str | None, str | None]:
    """Resolve one workflow pin; return ``(status, value, current_sha)``.

    Reads the pin's version + sha256 out of its workflow file, derives the release tag and asset
    name from the version, and resolves the asset's published digest via :func:`_resolve_digest`.
    A missing/unparseable VERSION or SHA256 line is an ``"error"`` (fail closed) — the pin exists
    to be verified, so one the regex can't read must never pass. On error ``current_sha`` is the
    parsed value when known, else ``None``.
    """
    path = root / pin.path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "error", f"could not read {pin.path}: {exc}", None
    vmatch = pin.version_re.search(text)
    smatch = pin.sha_re.search(text)
    if vmatch is None or smatch is None:
        return "error", f"could not find the VERSION/SHA256 pin in {pin.path}", None
    version, current_sha = vmatch.group(1), smatch.group(1)
    status, value = _resolve_digest(
        pin.owner, pin.repo, pin.tag(version), pin.asset(version), current_sha
    )
    return status, value, current_sha


def _missing_pins(text: str, path_name: str, replacements: dict[str, str]) -> list[str]:
    """Return a note for each ``old`` sha256 absent from ``text`` (empty list means all ok)."""
    return [
        f"could not locate sha256 {old} in {path_name} to rewrite"
        for old in replacements
        if text.count(old) == 0
    ]


def _apply_fixes(path: Path, replacements: dict[str, str]) -> list[str]:
    """Rewrite each stale ``old`` sha256 to its ``new`` value in ``path``; return failure notes.

    Each sha256 is a unique 64-hex literal in ``deps.py`` (Shawl as a ``sha256=`` kwarg, each
    claustrum variant as a table entry), so a textual replace is unambiguous. A ``new`` value the
    ``old`` string can't be found for is reported (never silently dropped) so the caller fails
    closed. All-or-nothing: if ANY replacement can't be located the file is left untouched — a
    stale pin is never rewritten alongside one that couldn't be, so a failed run leaves no
    partially-reconciled tree.
    """
    text = path.read_text(encoding="utf-8")
    notes = _missing_pins(text, path.name, replacements)
    if notes:
        return notes  # fail closed: locate every pin before mutating any
    for old, new in replacements.items():
        text = text.replace(old, new)
    if replacements:
        path.write_text(text, encoding="utf-8")
    return notes


def main(argv: list[str] | None = None) -> int:
    """Verify (or, with ``--fix``, refresh) every pinned sha256 against GitHub's digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite a stale sha256 in place from GitHub's published digest (postUpgradeTask)",
    )
    args = parser.parse_args(argv)

    failures = 0
    # Path -> {old sha256: new sha256}. deps.py and each workflow file are keyed separately so a
    # cross-file --fix stays all-or-nothing (see the pre-flight below).
    fixes: dict[Path, dict[str, str]] = {}

    binary_deps, deps_path = _load_deps()
    for dep in binary_deps:
        status, value = _published_digest(dep)
        if status == "skip":
            # Not a GitHub releases-download URL (e.g. a future non-GitHub binary dep):
            # nothing to verify against here — warn, don't block.
            print(f"WARN {dep.key}: {value}")
        elif status == "error":
            # STRICT: this runs ONLY when a pinned file changed, so refuse to pass without
            # verifying. A transient blip is fixed by a re-run; a permanent 404 is a broken pin.
            print(f"FAIL {dep.key}: {value}")
            failures += 1
        elif status == "warn":
            # The asset EXISTS but GitHub publishes no sha256 for it (e.g. an old release
            # from before GitHub added asset digests) — nothing to retry into existence.
            print(f"WARN {dep.key}: {value}")
        elif status == "ok":
            print(f"OK   {dep.key}: sha256 matches GitHub's published digest")
        elif value is not None:  # mismatch — value is the correct published sha256
            match = _RELEASE_URL_RE.match(dep.url)
            asset = match.group("asset") if match else dep.url
            if args.fix:
                # Refresh in place: the corrected hash lands in the same branch as the bump.
                fixes.setdefault(deps_path, {})[dep.sha256] = value
                print(
                    f"FIX  {dep.key}: sha256 for {asset}\n"
                    f"       was:  {dep.sha256}\n"
                    f"       now:  {value}"
                )
            else:
                print(
                    f"FAIL {dep.key}: sha256 mismatch for {asset}\n"
                    f"       pinned:    {dep.sha256}\n"
                    f"       published: {value}   <- update deps.py to this"
                )
                failures += 1

    workflow_pins, root = _load_workflow_pins()
    for pin in workflow_pins:
        status, value, current_sha = _resolve_workflow_pin(pin, root)
        if status == "error":
            print(f"FAIL {pin.path}: {value}")
            failures += 1
        elif status == "warn":
            print(f"WARN {pin.path}: {value}")
        elif status == "ok":
            print(f"OK   {pin.path}: sha256 matches GitHub's published digest")
        elif value is not None:  # mismatch — value is the correct published sha256
            if args.fix:
                fixes.setdefault(root / pin.path, {})[current_sha] = value
                print(f"FIX  {pin.path}: sha256\n       was:  {current_sha}\n       now:  {value}")
            else:
                print(
                    f"FAIL {pin.path}: sha256 mismatch\n"
                    f"       pinned:    {current_sha}\n"
                    f"       published: {value}   <- update the *_SHA256 in this workflow to this"
                )
                failures += 1

    total = sum(len(repl) for repl in fixes.values())
    if args.fix and fixes and not failures:
        # Only mutate the tree once the WHOLE pin set verified: if any other pin failed to
        # fetch/locate, the run is already exit-1, so writing a subset would leave a partially-
        # reconciled tree behind a failed postUpgradeTask. Locate every pin across every file
        # BEFORE writing any, so a locate failure in one file leaves all files untouched.
        notes = [
            note
            for path, repl in fixes.items()
            for note in _missing_pins(path.read_text(encoding="utf-8"), path.name, repl)
        ]
        if notes:
            for note in notes:
                print(f"FAIL {note}")
                failures += 1
        else:
            # The pre-flight proved every pin locatable, so _apply_fixes should report nothing —
            # but never trust the write to have found what the check did: surface any residual
            # miss as a failure instead of printing success over a partial write.
            for path, repl in fixes.items():
                for note in _apply_fixes(path, repl):
                    print(f"FAIL {note}")
                    failures += 1
            if not failures:
                print(f"wrote {total} refreshed sha256 pin(s)")
    elif args.fix and fixes:
        print(
            f"SKIP not rewriting {total} stale pin(s): "
            f"{failures} pin(s) could not be verified — refusing a partial fix"
        )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
