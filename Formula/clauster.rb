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
  version "0.10.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v0.10.0/clauster-0.10.0-macos-arm64"
      sha256 "be06f18dae69680bcffbf7b167faed6fec6cc81bf50329fbb39a20d9d8b3efa2"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v0.10.0/clauster-0.10.0-linux-x86_64"
      sha256 "c1052df6ee5bf0519e33c2981c522b59143281bea2f7cb9075c62f07aa4c8c87"
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
