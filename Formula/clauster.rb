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
  version "0.12.5"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.5/clauster-0.12.5-macos-arm64"
      sha256 "41d46d9ca788658968f88b3989c72a3341390da1a60cd53c463eec0bddce99c3"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.5/clauster-0.12.5-macos-x86_64"
      sha256 "f4fdb85e31b06579693bb99ac0cda6f9ac49b339647b7ee151177a5026492a5f"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.5/clauster-0.12.5-linux-x86_64"
      sha256 "6d10b527a5b907d68f7ebbe1b44d5d3ef12a64af989938c5ba125cbc8831812a"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.5/clauster-0.12.5-linux-arm64"
      sha256 "0baa311d41e7190235026643e8c56a17c5cb3206881bfc9659f133e69cb75243"
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
