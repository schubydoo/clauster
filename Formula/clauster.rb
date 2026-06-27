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
  version "0.12.7"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.7/clauster-0.12.7-macos-arm64"
      sha256 "5363a8680bceb61be2915c6737eeee22bff2367d918f739b9e842014c62d3859"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.7/clauster-0.12.7-macos-x86_64"
      sha256 "cd01fa8293d236f71f89042bf4884ede808bc5d6c6089f0bba8155c48c97b1da"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.7/clauster-0.12.7-linux-x86_64"
      sha256 "2f388adc7db41ee99f22507998820811bb5bc193b2fe2e37e9f36c3f7a864b53"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.7/clauster-0.12.7-linux-arm64"
      sha256 "75c541d1e280fda830879d73096e957de3484bb00258109dde584cbe309ff886"
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
