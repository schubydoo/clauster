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
  version "1.0.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.0/clauster-1.0.0-macos-arm64"
      sha256 "54fec37c9cbca0a750443e49d29954b0a67c8c60f9ee0069bb62021f5ac0bcaa"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.0/clauster-1.0.0-macos-x86_64"
      sha256 "fc2612dc58eef625d00ab79c1942ad30f133cb06c8f55c01855fd2c23cae9242"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.0/clauster-1.0.0-linux-x86_64"
      sha256 "018b8374225df5daac0e6e476806713bd09261af65cb079781d5ebe3b94c87c6"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.0/clauster-1.0.0-linux-arm64"
      sha256 "be747ca3a71e779dd670e1f0224efeb44a855d74f27d96f823735ad210da6728"
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
