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
  version "1.3.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.0/clauster-1.3.0-macos-arm64"
      sha256 "eb439551202d5c2789ba8868434fd3c97a4b723802d1250ba950df2d06e30012"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.0/clauster-1.3.0-macos-x86_64"
      sha256 "1fb7c8a04d8ea889a95b26fced84eaa1845eba2272d50f97f1432827f444ac60"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.0/clauster-1.3.0-linux-x86_64"
      sha256 "15b3a872d22bf025135c941bebe2f24fced7e368b8731abc63a26d636263bcb7"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.0/clauster-1.3.0-linux-arm64"
      sha256 "6d15bba08733c15684cea0ad9e466ec33b6c129cce71e973c54f8d0edefb6151"
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
