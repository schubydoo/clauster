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
  version "1.6.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.6.0/clauster-1.6.0-macos-arm64"
      sha256 "86b2625b06e540a509e717f9cfc360de0770bc47a2a724ff49e6b9ecd48b6a11"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.6.0/clauster-1.6.0-macos-x86_64"
      sha256 "a88f0702607ae2793e5b4c720e96bca9044761681c696d9a94441f8ac565d819"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.6.0/clauster-1.6.0-linux-x86_64"
      sha256 "3d047f8e63307d6c1f55450a0a313af9f985972af41fb18f7edb44706e1fa40d"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.6.0/clauster-1.6.0-linux-arm64"
      sha256 "d01c12e3fa3f5d0807db244aa346e899cfe902e10950f9f8d15b43a1cd784cd7"
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
