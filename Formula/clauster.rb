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
  version "0.12.2"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.2/clauster-0.12.2-macos-arm64"
      sha256 "209013cbb8b72b178f32c4e2249774e1cc66374f90ad7f85212c1de53b839944"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.2/clauster-0.12.2-macos-x86_64"
      sha256 "c6f6a3d49e471776468b4ac98af36961a8dd314e38ceee9e77eafe6f9ec99cee"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.2/clauster-0.12.2-linux-x86_64"
      sha256 "f9fa0c223d4296611dfa35d42612c249a4b41ae35a0ea5a09a476b5b19b33871"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.12.2/clauster-0.12.2-linux-arm64"
      sha256 "9c1b07ebc30bdf2b71c123aa9394d195d330850327b119141675e8a1294e1052"
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
