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
  version "1.4.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.4.0/clauster-1.4.0-macos-arm64"
      sha256 "f07cbd1f85e1f0a251057cf5cefc92aca1994263bf98ac87c4a893adc016f80e"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.4.0/clauster-1.4.0-macos-x86_64"
      sha256 "4e3f403fc75dd696eea4c0a4f264c66b18b42e3caeb30ce3b8396fbc57fb98d1"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.4.0/clauster-1.4.0-linux-x86_64"
      sha256 "040de0b97b63e07bc14a8d0dbaa8374a7b247cc56d1ffe88afc5e53bf595b55e"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.4.0/clauster-1.4.0-linux-arm64"
      sha256 "6c568291533ad94fa2e5f52322611e40af9e289cbbd4a7a7df8cfe1d8ee160b2"
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
