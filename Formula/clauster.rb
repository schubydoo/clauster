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
  version "1.0.1"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.1/clauster-1.0.1-macos-arm64"
      sha256 "1121bfc30bef4f0ce88197ca0b4d8ea7a9f99d1979ad6c5fba3814aaa0a2d0b2"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.1/clauster-1.0.1-macos-x86_64"
      sha256 "07d69eb9315dd852362ab5db79fca9bda52225cadaa12ce961765714c661993e"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.1/clauster-1.0.1-linux-x86_64"
      sha256 "960384b728d94b2ed395cf3957c42b17c7500770a9e32d4089076918c8968285"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.1/clauster-1.0.1-linux-arm64"
      sha256 "bb47c17bdbded0fa6aae12e965e7d920f9e096eb2dd00863c19a0962405c8c66"
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
