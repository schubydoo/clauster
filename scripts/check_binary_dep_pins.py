#!/usr/bin/env python3
"""Verify — or, with ``--fix``, refresh — each :data:`clauster.deps.BINARY_DEPS` pin.

Renovate can bump the ``_*_VERSION`` constant + derived URL of a managed binary
(:mod:`clauster.deps`), but the hosted Renovate App cannot rewrite the source-pinned
``sha256`` — the ``github-releases`` datasource exposes the git-tag commit digest, not the
release *asset's* file checksum. GitHub itself, however, now publishes a per-asset SHA-256
(the ``digest`` field on a release asset), so this script closes the loop: it fetches that
published digest for each pinned asset and asserts it equals the ``sha256`` in ``deps.py``.

Two modes:

* **verify** (default) — a stale hash fails loudly and the script prints the correct value to
  paste. This is what the ``binary-dep-pins`` CI workflow runs; it is platform-independent and
  never mutates the tree.
* **``--fix``** — the same check, but a stale hash is *rewritten in place* in ``deps.py`` (from
  GitHub's published digest) instead of merely reported. This is what the self-hosted Renovate
  CE runs as a ``postUpgradeTask`` on a Shawl/claustrum version bump, so the refreshed hash lands
  in the same branch/commit as the bump — no human paste step. Fetch/lookup errors still fail
  loudly in both modes (fail-closed): a bump that can't be verified never silently "passes".

To keep ``--fix`` runnable under a bare ``python3`` (Renovate CE's postUpgradeTask has no venv
and doesn't ``pip install`` clauster), the in-repo ``src/`` is put on ``sys.path`` before the
import — ``deps.py`` and ``clauster/__init__`` are stdlib-only, so no ``uv sync`` is needed.

Exit codes: ``0`` every pin verified (or, with ``--fix``, now reconciled), or
genuinely-unverifiable-but-not-wrong; ``1`` a pin's ``sha256`` disagrees with GitHub's published
digest (verify mode), or a pin could not be fetched/located to verify.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_RELEASE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
    r"(?P<tag>[^/]+)/(?P<asset>[^/]+)$"
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


def _published_digest(dep) -> tuple[str, str | None]:  # noqa: ANN001 - dep is a clauster.deps.BinaryDep
    """Resolve one pin against GitHub's published asset digest.

    Returns ``(status, value)`` where ``status`` is one of:

    * ``"ok"``       — the pin matches; ``value`` is ``None``.
    * ``"mismatch"`` — the pin is stale; ``value`` is the correct sha256 to write.
    * ``"skip"``     — not a github releases-download URL; ``value`` is a reason to warn+pass.
    * ``"warn"``     — asset exists but GitHub publishes no sha256; ``value`` is a reason.
    * ``"error"``    — could not fetch/locate the asset; ``value`` is the failure detail.

    Only the network/lookup path lives here; the caller decides how each status prints and, in
    ``--fix`` mode, what to rewrite. Errors are surfaced (never swallowed) so the caller can
    fail-closed — this runs only when ``deps.py`` changed, so an unverifiable pin must not pass.
    """
    match = _RELEASE_URL_RE.match(dep.url)
    if not match:
        return "skip", f"not a github releases-download URL, skipping: {dep.url}"
    owner, repo, tag, asset = (match.group(k) for k in ("owner", "repo", "tag", "asset"))
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
        return "warn", f"GitHub publishes no sha256 for {asset} — pinned {dep.sha256}"
    published = digest.removeprefix("sha256:")
    if published == dep.sha256:
        return "ok", None
    return "mismatch", published


def _apply_fixes(path: Path, replacements: dict[str, str]) -> list[str]:
    """Rewrite each stale ``old`` sha256 to its ``new`` value in ``path``; return failure notes.

    Each sha256 is a unique 64-hex literal in ``deps.py`` (Shawl as a ``sha256=`` kwarg, each
    claustrum variant as a table entry), so a textual replace is unambiguous. A ``new`` value the
    ``old`` string can't be found for is reported (never silently dropped) so the caller fails
    closed. Writes once, only when at least one replacement actually applied.
    """
    text = path.read_text(encoding="utf-8")
    notes: list[str] = []
    applied = 0
    for old, new in replacements.items():
        count = text.count(old)
        if count == 0:
            notes.append(f"could not locate sha256 {old} in {path.name} to rewrite")
            continue
        text = text.replace(old, new)
        applied += count
    if applied:
        path.write_text(text, encoding="utf-8")
    return notes


def main(argv: list[str] | None = None) -> int:
    """Verify (or, with ``--fix``, refresh) every BINARY_DEPS pin against GitHub's digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help="rewrite a stale sha256 in deps.py from GitHub's published digest (postUpgradeTask)",
    )
    args = parser.parse_args(argv)

    binary_deps, deps_path = _load_deps()
    failures = 0
    replacements: dict[str, str] = {}
    for dep in binary_deps:
        status, value = _published_digest(dep)
        if status == "skip":
            # Not a GitHub releases-download URL (e.g. a future non-GitHub binary dep):
            # nothing to verify against here — warn, don't block.
            print(f"WARN {dep.key}: {value}")
        elif status == "error":
            # STRICT: this runs ONLY when deps.py changed, so refuse to pass without verifying.
            # A transient blip is fixed by a re-run; a permanent 404 means a genuinely broken pin.
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
                replacements[dep.sha256] = value
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

    if args.fix and replacements:
        for note in _apply_fixes(deps_path, replacements):
            print(f"FAIL {note}")
            failures += 1
        if not failures:
            print(f"wrote {len(replacements)} refreshed sha256 pin(s) to {deps_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
