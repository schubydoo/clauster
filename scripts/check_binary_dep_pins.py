#!/usr/bin/env python3
"""Verify each :data:`clauster.deps.BINARY_DEPS` pin against GitHub's published digest.

Renovate can bump the ``_*_VERSION`` constant + derived URL of a managed binary
(:mod:`clauster.deps`), but the hosted Renovate App cannot rewrite the source-pinned
``sha256`` — the ``github-releases`` datasource exposes the git-tag commit digest, not the
release *asset's* file checksum. GitHub itself, however, now publishes a per-asset SHA-256
(the ``digest`` field on a release asset), so this script closes the loop: it fetches that
published digest for each pinned asset and asserts it equals the ``sha256`` in ``deps.py``.

Run it in CI (or by hand) after a Renovate version bump: a stale hash fails loudly here and
the script prints the correct value to paste — no binary download, just one API call per
pin. ``GITHUB_TOKEN`` is used when present (higher rate limit); it is optional.

It runs from the dedicated ``binary-dep-pins`` workflow, which is **path-filtered** to only
trigger when ``deps.py`` (or this script) actually changes — so it never touches an unrelated
PR. Because it runs only on those (rare) PRs, it is deliberately STRICT: a fetch/API error
FAILS (a transient blip is fixed by a re-run; a permanent 404 means a broken pin), so a
version bump can never *pass without actually verifying* the hash. Cases that are genuinely
unverifiable rather than transient — a non-github-releases URL, or an asset GitHub publishes
no digest for — warn and pass (there is nothing to retry into existence).

Exit codes: ``0`` every pin verified, or genuinely-unverifiable-but-not-wrong; ``1`` a pin's
``sha256`` disagrees with GitHub's published digest, or a pin could not be fetched to verify.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

from clauster.deps import BINARY_DEPS

_RELEASE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/"
    r"(?P<tag>[^/]+)/(?P<asset>[^/]+)$"
)


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


def _published_digest(release: dict, asset_name: str) -> str | None:
    """Return the ``sha256:`` digest GitHub publishes for ``asset_name``, or None if absent."""
    for asset in release.get("assets", []):
        if isinstance(asset, dict) and asset.get("name") == asset_name:
            digest = asset.get("digest")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                return digest.removeprefix("sha256:")
            return None
    return None


def main() -> int:
    """Verify every BINARY_DEPS pin against GitHub's published asset digest."""
    failures = 0
    for dep in BINARY_DEPS:
        match = _RELEASE_URL_RE.match(dep.url)
        if not match:
            # Not a GitHub releases-download URL (e.g. a future non-GitHub binary dep):
            # nothing to verify against here — warn, don't block.
            print(f"WARN {dep.key}: not a github releases-download URL, skipping: {dep.url}")
            continue
        owner, repo, tag, asset = (match.group(k) for k in ("owner", "repo", "tag", "asset"))
        try:
            release = _fetch_release(owner, repo, tag)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # STRICT: this runs ONLY when deps.py changed, so refuse to pass without verifying.
            # A transient blip is fixed by a re-run; a permanent 404 means a genuinely broken pin.
            print(f"FAIL {dep.key}: could not fetch {owner}/{repo}@{tag} to verify: {exc}")
            failures += 1
            continue
        published = _published_digest(release, asset)
        if published is None:
            print(
                f"WARN {dep.key}: GitHub publishes no sha256 digest for {asset} "
                f"(nothing to verify against) — pinned {dep.sha256}"
            )
            continue
        if published == dep.sha256:
            print(f"OK   {dep.key}: {tag} {asset} sha256 matches GitHub's published digest")
        else:
            print(
                f"FAIL {dep.key}: sha256 mismatch for {asset}\n"
                f"       pinned:    {dep.sha256}\n"
                f"       published: {published}   <- update deps.py to this"
            )
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
