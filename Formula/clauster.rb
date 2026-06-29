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
  version "0.12.9"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.9/clauster-0.12.9-macos-arm64"
      sha256 "ba53ddbb969d95c2a4ea5737e10353d4a89e413b5c52b7c2c26411e46b63c1b7"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.9/clauster-0.12.9-macos-x86_64"
      sha256 "9e06e97637a3b7404ea2236260cb4b421afe33f06012accf74832cc786bab101"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.9/clauster-0.12.9-linux-x86_64"
      sha256 "9105dd075e9597e697b1dc90e7ad6a7ba47bc5f4108aafb2e8fd072118c1840f"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.9/clauster-0.12.9-linux-arm64"
      sha256 "c7d6543afe5e0a78de88850eec2994bf9d432c67b832ccab732662dcd1281d9f"
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
