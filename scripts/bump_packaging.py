#!/usr/bin/env python3
"""Regenerate the packaging manifests to a released version from its SHA256SUMS.

After a release publishes, the Scoop bucket manifest, the Homebrew formula, and
the Nix flake must point at the new version's binaries and checksums. This helper
rewrites whichever of those manifests exist in the working tree, in place, keying
every checksum off the release's authoritative ``SHA256SUMS`` (the list of what
was actually built and signed).

Driven by ``.github/workflows/packaging-bump.yml``. A manifest that is not present
is skipped (it simply is not on ``main`` yet). It fails closed rather than write a
half-baked manifest: the Scoop manifest's Windows asset must be in ``SHA256SUMS``,
and after rewriting, no manifest may still reference a non-target version. Standard
library only. Usage::

    python scripts/bump_packaging.py <version> <owner/repo> <path-to-SHA256SUMS>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Any ``clauster-<x.y.z>-`` asset token left in a rewritten manifest: a missed arch.
_ASSET_VERSION_RE = re.compile(r"clauster-(\d+\.\d+\.\d+)-")


def parse_sums(text: str) -> dict[str, str]:
    """Parse ``SHA256SUMS`` text into an ``{asset_filename: sha256}`` mapping."""
    sums: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not _SHA256_RE.match(parts[0]):
            continue
        digest, name = parts
        sums[name.lstrip("*")] = digest  # coreutils marks binary mode with a leading '*'
    return sums


def asset_url(repo: str, version: str, asset: str) -> str:
    """Build the release download URL for one asset filename."""
    return f"https://github.com/{repo}/releases/download/v{version}/{asset}"


def arch_assets(version: str, sums: dict[str, str]) -> dict[str, str]:
    """Map each ``clauster-<version>-<arch>`` asset to its arch token and digest."""
    prefix = f"clauster-{version}-"
    return {
        asset[len(prefix) :]: digest for asset, digest in sums.items() if asset.startswith(prefix)
    }


def stale_versions(text: str, version: str) -> set[str]:
    """Return any asset-token versions in ``text`` other than the target version."""
    return set(_ASSET_VERSION_RE.findall(text)) - {version}


def bump_scoop(text: str, version: str, repo: str, sums: dict[str, str]) -> str:
    """Rewrite the Scoop bucket manifest's version, URL, and hash (Windows x86_64)."""
    asset = f"clauster-{version}-windows-x86_64.exe"
    digest = sums.get(asset)
    if digest is None:
        raise ValueError(f"Scoop manifest: release asset {asset!r} missing from SHA256SUMS")
    data = json.loads(text)
    data["version"] = version
    arch = data["architecture"]["64bit"]
    arch["url"] = f"{asset_url(repo, version, asset)}#/clauster.exe"
    arch["hash"] = digest
    return json.dumps(data, indent=4) + "\n"


def bump_formula(text: str, version: str, repo: str, sums: dict[str, str]) -> str:
    """Rewrite the Homebrew formula's version and each present arch's URL + sha256."""
    text = re.sub(r'version "[^"]+"', f'version "{version}"', text, count=1)
    for arch, digest in arch_assets(version, sums).items():
        url = asset_url(repo, version, f"clauster-{version}-{arch}")
        block = re.compile(
            r'(?P<head>url ")[^"]*-'
            + re.escape(arch)
            + r'(?P<mid>"\s*\n\s*sha256 ")[0-9a-f]{64}(?P<tail>")'
        )
        text = block.sub(
            lambda m, url=url, digest=digest: f"{m['head']}{url}{m['mid']}{digest}{m['tail']}",
            text,
        )
    return text


def bump_flake(text: str, version: str, sums: dict[str, str]) -> str:
    """Rewrite the Nix flake's version and each present system's sha256.

    The flake's ``file`` field interpolates ``${version}``, so only the top-level
    version string and the per-system checksums need rewriting. Because that
    interpolation hides the version from ``stale_versions`` (which only sees numeric
    tokens), fail closed here if the flake declares an arch missing from SHA256SUMS —
    otherwise its stale sha256 would silently survive the bump.
    """
    declared = set(re.findall(r'file = "clauster-\$\{version\}-([^"]+)"', text))
    missing = declared - set(arch_assets(version, sums))
    if missing:
        raise ValueError(f"flake.nix: arch(es) {sorted(missing)} absent from SHA256SUMS")
    text = re.sub(r'(version = ")[^"]+(";)', rf"\g<1>{version}\g<2>", text, count=1)
    for arch, digest in arch_assets(version, sums).items():
        block = re.compile(
            r'(?P<head>file = "clauster-\$\{version\}-'
            + re.escape(arch)
            + r'";\s*\n\s*sha256 = ")[0-9a-f]{64}(?P<tail>";)'
        )
        text = block.sub(lambda m, digest=digest: f"{m['head']}{digest}{m['tail']}", text)
    return text


def main(argv: list[str]) -> int:
    """Bump every present packaging manifest to ``version`` from ``SHA256SUMS``."""
    if len(argv) != 4:
        print("usage: bump_packaging.py <version> <owner/repo> <SHA256SUMS>", file=sys.stderr)
        return 2
    version, repo, sums_path = argv[1], argv[2], argv[3]
    if not _VERSION_RE.match(version):
        print(f"error: version {version!r} is not MAJOR.MINOR.PATCH", file=sys.stderr)
        return 2

    sums = parse_sums(Path(sums_path).read_text())
    if not arch_assets(version, sums):
        print(f"error: SHA256SUMS has no clauster-{version}-* assets", file=sys.stderr)
        return 1

    manifests = {
        Path("bucket/clauster.json"): lambda t: bump_scoop(t, version, repo, sums),
        Path("Formula/clauster.rb"): lambda t: bump_formula(t, version, repo, sums),
        Path("flake.nix"): lambda t: bump_flake(t, version, sums),
    }

    # First pass: compute + validate every present manifest. Fail closed BEFORE any
    # write so a mid-sequence abort never leaves a half-rewritten tree.
    planned: list[tuple[Path, str]] = []
    try:
        for path, rewrite in manifests.items():
            if not path.exists():
                print(f"skip {path} (not present)")
                continue
            new_text = rewrite(path.read_text())
            leftover = stale_versions(new_text, version)
            if leftover:
                raise ValueError(
                    f"{path}: still references version(s) {sorted(leftover)} after bump"
                )
            planned.append((path, new_text))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Second pass: write only the manifests that actually changed.
    changed: list[str] = []
    for path, new_text in planned:
        if new_text == path.read_text():
            print(f"unchanged {path}")
            continue
        path.write_text(new_text)
        changed.append(str(path))
        print(f"bumped {path}")

    print(f"changed={','.join(changed)}" if changed else "changed=")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
