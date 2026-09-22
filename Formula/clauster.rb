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
  version "1.2.1"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.1/clauster-1.2.1-macos-arm64"
      sha256 "de6f2b1228e29c6598603301c66974170f147480cf3632c09cb26fee569c9cfe"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.1/clauster-1.2.1-macos-x86_64"
      sha256 "06163fb30066b89506049d86ab6a88987bae74fbb4466b43fed8b19652dd6374"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.1/clauster-1.2.1-linux-x86_64"
      sha256 "cd1341437bb7505459f47494ecfc2a1d3705422070bdadf9fc8b0dd2e5754549"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.2.1/clauster-1.2.1-linux-arm64"
      sha256 "3441bc0a5e947400f7e9935144524dd897e484d09f0e8ed214c741809200b32c"
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
