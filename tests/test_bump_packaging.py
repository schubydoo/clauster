"""``scripts/bump_packaging.py`` rewrites every present packaging manifest.

The packaging-bump workflow runs this after a release to point the Scoop bucket
manifest, the Homebrew formula, and the Nix flake at the new version + checksums.
These tests run it as a subprocess (matching ``test_config_docs.py``) against a
temp tree, asserting the rewrite and the fail-closed paths.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = "scripts/bump_packaging.py"

OLD_LINUX = "a" * 64
OLD_MACOS = "b" * 64
OLD_WIN = "c" * 64
NEW_LINUX = "1" * 64
NEW_MACOS = "2" * 64
NEW_WIN = "3" * 64

SCOOP = """\
{
    "version": "0.10.0",
    "architecture": {
        "64bit": {
            "url": "https://github.com/schubydoo/clauster/releases/download/v0.10.0/clauster-0.10.0-windows-x86_64.exe#/clauster.exe",
            "hash": "@WIN@"
        }
    },
    "bin": "clauster.exe"
}
""".replace("@WIN@", OLD_WIN)

FORMULA = f"""\
class Clauster < Formula
  version "0.10.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.10.0/clauster-0.10.0-macos-arm64"
      sha256 "{OLD_MACOS}"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.10.0/clauster-0.10.0-linux-x86_64"
      sha256 "{OLD_LINUX}"
    end
  end
end
"""

FLAKE = """\
{
  version = "0.10.0";
  assets = {
    "x86_64-linux" = {
      file = "clauster-${version}-linux-x86_64";
      sha256 = "@LINUX@";
    };
    "aarch64-darwin" = {
      file = "clauster-${version}-macos-arm64";
      sha256 = "@MACOS@";
    };
  };
}
""".replace("@LINUX@", OLD_LINUX).replace("@MACOS@", OLD_MACOS)

SUMS_FULL = (
    f"{NEW_LINUX}  clauster-0.11.0-linux-x86_64\n"
    f"{NEW_MACOS}  clauster-0.11.0-macos-arm64\n"
    f"{NEW_WIN}  clauster-0.11.0-windows-x86_64.exe\n"
)


def _run(tmp_path: Path, version: str = "0.11.0") -> subprocess.CompletedProcess[str]:
    """Run the bump script with ``tmp_path`` as the working tree."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / SCRIPT),
            version,
            "schubydoo/clauster",
            str(tmp_path / "SHA256SUMS"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def _seed(tmp_path: Path, *, scoop=SCOOP, formula=FORMULA, flake=FLAKE, sums=SUMS_FULL) -> None:
    """Write the three manifests + a SHA256SUMS into ``tmp_path``."""
    if scoop is not None:
        (tmp_path / "bucket").mkdir(exist_ok=True)
        (tmp_path / "bucket" / "clauster.json").write_text(scoop)
    if formula is not None:
        (tmp_path / "Formula").mkdir(exist_ok=True)
        (tmp_path / "Formula" / "clauster.rb").write_text(formula)
    if flake is not None:
        (tmp_path / "flake.nix").write_text(flake)
    (tmp_path / "SHA256SUMS").write_text(sums)


def test_bumps_all_three_manifests(tmp_path):
    _seed(tmp_path)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr

    scoop = json.loads((tmp_path / "bucket" / "clauster.json").read_text())
    arch = scoop["architecture"]["64bit"]
    assert scoop["version"] == "0.11.0"
    assert arch["url"].endswith("v0.11.0/clauster-0.11.0-windows-x86_64.exe#/clauster.exe")
    assert arch["hash"] == NEW_WIN

    formula = (tmp_path / "Formula" / "clauster.rb").read_text()
    assert 'version "0.11.0"' in formula
    assert NEW_MACOS in formula and NEW_LINUX in formula
    assert OLD_MACOS not in formula and OLD_LINUX not in formula
    assert "clauster-0.10.0-" not in formula

    flake = (tmp_path / "flake.nix").read_text()
    assert 'version = "0.11.0";' in flake
    assert NEW_LINUX in flake and NEW_MACOS in flake
    # The flake's file field interpolates ${version}; the literal is left intact.
    assert 'file = "clauster-${version}-linux-x86_64"' in flake


def test_idempotent_second_run_changes_nothing(tmp_path):
    _seed(tmp_path)
    assert _run(tmp_path).returncode == 0
    after_first = (tmp_path / "flake.nix").read_text()
    second = _run(tmp_path)
    assert second.returncode == 0
    assert "changed=\n" in second.stdout  # nothing changed on the second pass
    assert (tmp_path / "flake.nix").read_text() == after_first


def test_missing_manifests_are_skipped(tmp_path):
    _seed(tmp_path, scoop=None, formula=None, flake=None)
    result = _run(tmp_path)
    assert result.returncode == 0
    assert "skip" in result.stdout


def test_fail_closed_when_scoop_windows_asset_absent(tmp_path):
    sums = f"{NEW_LINUX}  clauster-0.11.0-linux-x86_64\n{NEW_MACOS}  clauster-0.11.0-macos-arm64\n"
    _seed(tmp_path, sums=sums)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "windows-x86_64.exe" in result.stderr


def test_fail_closed_when_present_arch_has_no_checksum(tmp_path):
    # Formula carries a linux block, but SHA256SUMS omits the linux asset: the
    # stale 0.10.0 reference would survive, so the bump must abort.
    sums = (
        f"{NEW_MACOS}  clauster-0.11.0-macos-arm64\n"
        f"{NEW_WIN}  clauster-0.11.0-windows-x86_64.exe\n"
    )
    _seed(tmp_path, flake=None, sums=sums)
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "0.10.0" in result.stderr
    # Two-pass: the Scoop manifest (validated first) must NOT have been written —
    # an aborted run leaves no half-rewritten tree.
    assert '"version": "0.10.0"' in (tmp_path / "bucket" / "clauster.json").read_text()


def test_rejects_malformed_version(tmp_path):
    _seed(tmp_path)
    result = _run(tmp_path, version="0.11")
    assert result.returncode == 2
