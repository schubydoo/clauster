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
  version "0.12.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.0/clauster-0.12.0-macos-arm64"
      sha256 "af507ba5449b8c40907206131b8731eb1f456b6c6bd65d0dd0b964cb9bbc21f7"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.0/clauster-0.12.0-macos-x86_64"
      sha256 "f51d6b3f2b1667fa422af70bdaf1c45f456768cbf3976acb6524e71f603634cd"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.0/clauster-0.12.0-linux-x86_64"
      sha256 "05db2c4b1084d0faca3ceeda096958a5cd599a7bdb05ea6f4f215cd5f8b9a4b1"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.0/clauster-0.12.0-linux-arm64"
      sha256 "6893277785b74e340b54ace961d6c6e44555d7d2ec2a72a45b79ab14e49a1355"
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
