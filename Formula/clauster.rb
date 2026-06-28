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
  version "0.12.8"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.8/clauster-0.12.8-macos-arm64"
      sha256 "0ef1d41b88699952d4713c1c593183aef68f66dfc4bdc82793f6f4ba8f8ed675"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.8/clauster-0.12.8-macos-x86_64"
      sha256 "2ea82bbc8c5474a55779d333057e2cd37fbaa142253486c43f76d0805adaee65"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.8/clauster-0.12.8-linux-x86_64"
      sha256 "5a78b82918d1b52a4352cd239d91841e1b84367fdd450c3525d618cf2a6ff125"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.8/clauster-0.12.8-linux-arm64"
      sha256 "ee58441b7510c803cdd6703f0cc4c9c5894efa90909778041f54372216327afc"
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
