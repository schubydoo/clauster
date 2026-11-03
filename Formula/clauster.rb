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
  version "1.3.3"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.3/clauster-1.3.3-macos-arm64"
      sha256 "0fba3d6e4401b503f46a7d976cb0506c48e0a288ad8ce2078114a99539867958"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.3/clauster-1.3.3-macos-x86_64"
      sha256 "87d3c7a0f86703bd35398c4c008b3b3aa202e1b137706e988d3731641918a190"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.3/clauster-1.3.3-linux-x86_64"
      sha256 "85ac7eb74139b84a9d72cc6fb50d5ace4ed9207d247197f0b4e7e4f6d8793273"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.3/clauster-1.3.3-linux-arm64"
      sha256 "9f7e57604a074f24d272d759b589c4c7d93fa4158ab334ed2a45c0693f76c717"
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
