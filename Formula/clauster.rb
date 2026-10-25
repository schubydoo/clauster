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
  version "1.3.2"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.2/clauster-1.3.2-macos-arm64"
      sha256 "c9c021838f0b558285c1b659ca80690de61fa12399d134e770c823915be747c6"
    end
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.2/clauster-1.3.2-macos-x86_64"
      sha256 "780dce3f38cc41ba31ec25064c90370bc01da3054b75b45544290dd2e0060dca"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.2/clauster-1.3.2-linux-x86_64"
      sha256 "2237a37daec509cf4c9e23af42514518dfdd96a02257aa647bc8e7c38c9b240b"
    end
    on_arm do
      url "https://github.com/schubydoo/clauster/releases/download/v1.3.2/clauster-1.3.2-linux-arm64"
      sha256 "287fbbb778b07f4a36ecdd65b4bec5911bee7de10bf07af1f92333f95aaaf294"
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
