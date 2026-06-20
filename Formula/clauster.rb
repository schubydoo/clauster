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
  version "0.12.1"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.1/clauster-0.12.1-macos-arm64"
      sha256 "dab2a533f60dd1ead6977dd4706e63ce088117b139d02987d6c035fdd2825219"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.1/clauster-0.12.1-macos-x86_64"
      sha256 "792e850a7faad0d0b64824478e4f64746ed2573e3b77b5f85e0d2b859135773b"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.1/clauster-0.12.1-linux-x86_64"
      sha256 "ea38a15ccca6358cc50842de51d938eea3d65bce569d4378a26c971bf69885ee"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.1/clauster-0.12.1-linux-arm64"
      sha256 "e78afb3d5ae5e4d4f054e3987f00e03e2884c4b175782843704270e2ee17185a"
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
