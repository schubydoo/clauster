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
  version "0.12.6"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.6/clauster-0.12.6-macos-arm64"
      sha256 "c6f89de06bb74191e046526b9902c99ac3e6858020de1f32986c796aeb4979ef"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.6/clauster-0.12.6-macos-x86_64"
      sha256 "f5dabf5a4367d8a4144cf50906ca7d7bb13f5f11db8711b1d3b809e68cac3062"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.6/clauster-0.12.6-linux-x86_64"
      sha256 "49ebf62d536d6b655d6f2133f4abd3d6d57bd1db4a85d190b2b8dc030c9f0f15"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.6/clauster-0.12.6-linux-arm64"
      sha256 "43a5ab00868cac0acda3057f98008335423d31bffb704301276e5df236dbf919"
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
