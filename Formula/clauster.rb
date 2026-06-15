# Homebrew formula for the signed standalone clauster binary.
#
#   brew install schubydoo/clauster/clauster
#
# Covers the published macOS (arm64 + Intel) and Linux (x86_64 + arm64) binaries.
# Windows installs via the Scoop bucket. Version + checksums are auto-bumped per
# release by packaging-bump.yml from the release SHA256SUMS.
class Clauster < Formula
  desc "Self-hosted web UI for spawning and managing Claude Code remote-control bridges"
  homepage "https://github.com/schubydoo/clauster"
  version "0.11.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-macos-arm64"
      sha256 "aec2d0c69c6aff0e74921e973e65bbc11229920c94ac575919af84d0271ab0b8"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-macos-x86_64"
      sha256 "88b688476865b53dd87aa41b37abd7bb8db1f655015e5cacfab03c96407289ac"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-linux-x86_64"
      sha256 "9ef8e2d2757b8c6315c551db0e2698a9118ab4d110c1cd7fb90538e006893264"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-linux-arm64"
      sha256 "0e08db0b8966cb5a7bdb2fe827e8be54ceed400dab33c5db1ca0ca24d1ea2dfc"
    end
  end

  def install
    # The release asset downloads under its versioned name; install it as `clauster`.
    bin.install Dir["clauster-*"].first => "clauster"
  end

  test do
    assert_match "clauster #{version}", shell_output("#{bin}/clauster --version")
  end
end
