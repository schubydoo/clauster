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
  version "1.0.2"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.2/clauster-1.0.2-macos-arm64"
      sha256 "40678431c9f7980631f90eed24a7f7a5293b94e6d629ba645b9f87efb6bbaf63"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.2/clauster-1.0.2-macos-x86_64"
      sha256 "e7655cf55c62c1eafffc905b837ef1c51d94e775e6096b0020bba0726bbe3e43"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.2/clauster-1.0.2-linux-x86_64"
      sha256 "8f01558ad0375344d968fba36fe84a659e0583a2554f67b99e56a7c70ff472c1"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.0.2/clauster-1.0.2-linux-arm64"
      sha256 "f4f984f8a90968c261c18ceaed2ec07d0af38b1df9f8e5727d1c142a33a7c1b7"
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
