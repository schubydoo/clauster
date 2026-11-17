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
  version "1.5.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.5.0/clauster-1.5.0-macos-arm64"
      sha256 "3d30b4699080b8a6f4b7d9555f05376860e44b7e0a496c3573d6a1f0c95623ae"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.5.0/clauster-1.5.0-macos-x86_64"
      sha256 "500290e4558664324a25a457917d54f0b0336876ea048fee6af96c08b70e729e"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.5.0/clauster-1.5.0-linux-x86_64"
      sha256 "a8d15e87a22e4472f87a6c5bbaddb0d1c14522605188deb4ea7c71d1d050152e"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.5.0/clauster-1.5.0-linux-arm64"
      sha256 "67ceae85a91940f5ef9274c3bd5c77275e9489de2dcbe6874d9c35c1919f6c8e"
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
