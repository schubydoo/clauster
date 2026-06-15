# Homebrew formula for the signed standalone clauster binary.
#
#   brew install --formula ./Formula/clauster.rb
#
# Covers the published 0.10.0 targets: macOS arm64 (Apple Silicon) and Linux
# x86_64 (Linuxbrew). macOS Intel and Linux arm64 binaries are not built yet
# (those install via pip/uv/uvx for now).
class Clauster < Formula
  desc "Self-hosted web UI for spawning and managing Claude Code remote-control bridges"
  homepage "https://github.com/schubydoo/clauster"
  version "0.11.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-macos-arm64"
      sha256 "aec2d0c69c6aff0e74921e973e65bbc11229920c94ac575919af84d0271ab0b8"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.11.0/clauster-0.11.0-linux-x86_64"
      sha256 "9ef8e2d2757b8c6315c551db0e2698a9118ab4d110c1cd7fb90538e006893264"
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
