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
  version "0.12.4"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.4/clauster-0.12.4-macos-arm64"
      sha256 "14098a5769b02e83b67b609758e80279cab7627704764d45f04968d71dfcb6ad"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.4/clauster-0.12.4-macos-x86_64"
      sha256 "5430b0e11cd1721665fbd7e372d344c28be835dc0e1789e5585a49564724ae98"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.4/clauster-0.12.4-linux-x86_64"
      sha256 "2465826200f0afb1c82b6395b2edc5713d512ca71812114d3a10505aa9f9ca1b"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.4/clauster-0.12.4-linux-arm64"
      sha256 "0246f435db04731880a885da07893123d1bb5f762beabe48ef2cb60ce11dc020"
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
