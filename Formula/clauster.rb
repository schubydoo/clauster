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
  version "0.12.3"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.3/clauster-0.12.3-macos-arm64"
      sha256 "95c5cc5b58a14c92090b89eba08bce91e24d386947e93b855bb0f673e37ca426"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.3/clauster-0.12.3-macos-x86_64"
      sha256 "983630d9b6be41d64910026e76d6d508e8d5df392632f4a2d6d1bf44faadfe5a"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.3/clauster-0.12.3-linux-x86_64"
      sha256 "c3c0ab95738a9c11f079e6c8064b91284b02069d09a3797b2f8eb3e4785ebc5e"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.3/clauster-0.12.3-linux-arm64"
      sha256 "aed0e6d3f9efb49e71f52c63050bfa9cc8d79c26bafbe5362ff248ee3b2e357c"
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
