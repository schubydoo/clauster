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
  version "1.3.1"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.1/clauster-1.3.1-macos-arm64"
      sha256 "4f7bcdc8e414ccd5a57a318e9607d575e0c1dc06d797c9e052ad76b8fa1ae709"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.1/clauster-1.3.1-macos-x86_64"
      sha256 "87812aad6e9647c24610627d7afa30c74cc48bd9fb1b85c60174ae8086016d47"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.1/clauster-1.3.1-linux-x86_64"
      sha256 "dc69c1fa4db1ee595a4bac5bd250464c5eb3b1427c43d114076cdb3bafee157d"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.1/clauster-1.3.1-linux-arm64"
      sha256 "b0c2a809bff1c14ac022eacec861ea804f452742d7a7cb83ee6fb6d27901679a"
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
