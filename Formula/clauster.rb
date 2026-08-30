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
  version "1.1.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.1.0/clauster-1.1.0-macos-arm64"
      sha256 "157e6cf66bcb0beed8a0f0470fc6f99a803b561fb340174da478ebbd3ee27a64"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.1.0/clauster-1.1.0-macos-x86_64"
      sha256 "51dea1f6b7136122af361e0c7bb954a87dfc66aa5935f13f191f37f3d659910e"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.1.0/clauster-1.1.0-linux-x86_64"
      sha256 "14289cb102033ae83dffecd4278641c01a113fad77d591851453cc97d2958e3b"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.1.0/clauster-1.1.0-linux-arm64"
      sha256 "399e27343d53694d72bfbd87f740fbbb69dcaa6af5babf461a3fd9f78372d64e"
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
