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
  version "1.2.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.0/clauster-1.2.0-macos-arm64"
      sha256 "e1baae8d272b76d6378e7e5a8e14846adf34d403210cc62223ccdae903f994fb"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.0/clauster-1.2.0-macos-x86_64"
      sha256 "5402b179c4ad5a0c644e09fe015e1c4705f8d8e958882dd204279c8100f4f404"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.0/clauster-1.2.0-linux-x86_64"
      sha256 "4ee7df27f19d09ae1dff225aa63a6b4d42459ec048d4c0cd118d17b1f39f831e"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.0/clauster-1.2.0-linux-arm64"
      sha256 "3c7fec2deb17cecf9a0add4a6422d8e2a5bf4047535b1b67292fe2af4018c545"
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
