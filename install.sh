#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Clauster installer
#
#   curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/install.sh | bash
#
# Downloads the signed standalone `clauster` binary for your OS + architecture
# from the latest GitHub release, verifies its SHA-256 against the release's
# signed SHA256SUMS, and installs it onto your PATH. No Python required.
#
# Clauster spawns the `claude` CLI but does not vendor it — install Claude Code
# separately and keep it on PATH.
#
# Environment overrides:
#   CLAUSTER_VERSION       pin a version (e.g. 0.10.0); default: latest release
#   CLAUSTER_INSTALL_DIR   install directory; default: ~/.local/bin
# ============================================================================

REPO_OWNER="schubydoo"
REPO_NAME="clauster"
TOOL_NAME="clauster"

# Scratch dir, cleaned up on exit. Global so the EXIT trap can see it even after
# main() returns (a `local` would be out of scope and trip `set -u`).
WORKDIR=""
cleanup() { [ -n "$WORKDIR" ] && rm -rf "$WORKDIR"; }
trap cleanup EXIT

# --- Colour output (disabled when stdout is not a TTY) ---
if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi
info() { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
ok()   { printf "${GREEN}[ OK ]${NC}  %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
err()  { printf "${RED}[ERR ]${NC}  %s\n" "$*" >&2; }
die()  { err "$@"; exit 1; }

# --- Fallback hint for unsupported targets ---
pip_fallback() {
    cat >&2 <<'EOF'

No standalone binary is published for this OS/architecture (yet). Install with
Python (3.11+) instead — pick whichever you have:

  uv tool install clauster      # https://docs.astral.sh/uv/
  uvx clauster run -c clauster.yml
  pipx install clauster
  pip install clauster

Full install guide: https://schubydoo.github.io/clauster/installation/
EOF
}

# --- Detection ---
detect_os() {
    local os; os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    case "$os" in
        linux*)               echo "linux" ;;
        darwin*)              echo "macos" ;;
        mingw*|msys*|cygwin*) echo "windows" ;;
        *)                    die "Unsupported operating system: $os" ;;
    esac
}

detect_arch() {
    local arch; arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)  echo "x86_64" ;;
        aarch64|arm64) echo "arm64" ;;
        *)             echo "$arch" ;;  # surfaced verbatim in the unsupported-target error
    esac
}

# Map (os, arch) -> release asset basename, or empty for an unknown arch.
# Whether the named asset actually exists in a given release is decided later,
# against that release's SHA256SUMS (the authoritative list of built binaries).
asset_for() {
    local os="$1" arch="$2" ver="$3"
    case "${os}-${arch}" in
        linux-x86_64)  echo "clauster-${ver}-linux-x86_64" ;;
        linux-arm64)   echo "clauster-${ver}-linux-arm64" ;;
        macos-arm64)   echo "clauster-${ver}-macos-arm64" ;;
        macos-x86_64)  echo "clauster-${ver}-macos-x86_64" ;;
        *)             echo "" ;;
    esac
}

# --- HTTP helpers (curl or wget) ---
have() { command -v "$1" >/dev/null 2>&1; }

http_to() {  # http_to <url> <dest>
    local url="$1" dest="$2"
    if have curl; then
        curl -fsSL -o "$dest" "$url"
    elif have wget; then
        wget -qO "$dest" "$url"
    else
        die "Need curl or wget to download files."
    fi
}

resolve_latest() {  # echo the latest version (tag minus leading v)
    local url tag
    if have curl; then
        url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
            "https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/latest")"
    elif have wget; then
        # HTTP headers are CRLF-terminated, so strip the trailing CR — otherwise the
        # version carries a \r into the asset URL and 404s on wget-only hosts.
        url="$(wget -q -S -O /dev/null \
            "https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/latest" 2>&1 \
            | awk '/^[[:space:]]*Location:/ {print $2}' | tail -n1 | tr -d '\r')"
    else
        die "Need curl or wget to resolve the latest version."
    fi
    tag="${url##*/tag/}"          # .../releases/tag/v0.10.0 -> v0.10.0
    tag="${tag#v}"               # v0.10.0 -> 0.10.0
    [ -n "$tag" ] && [ "$tag" != "$url" ] || die "Could not resolve the latest release version."
    echo "$tag"
}

sha256_of() {  # echo the sha256 of a file, portably
    if have sha256sum; then
        sha256sum "$1" | awk '{print $1}'
    elif have shasum; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "Need sha256sum or shasum to verify the download."
    fi
}

# --- Install-dir selection ---
choose_install_dir() {
    if [ -n "${CLAUSTER_INSTALL_DIR:-}" ]; then
        echo "$CLAUSTER_INSTALL_DIR"; return
    fi
    echo "${HOME}/.local/bin"
}

main() {
    info "Installing ${TOOL_NAME}"

    local os arch ver asset
    os="$(detect_os)"
    arch="$(detect_arch)"

    if [ "$os" = "windows" ]; then
        warn "On Windows, install with Scoop instead of this script:"
        warn "  scoop bucket add clauster https://github.com/${REPO_OWNER}/${REPO_NAME}"
        warn "  scoop install clauster"
        pip_fallback
        exit 1
    fi

    ver="${CLAUSTER_VERSION:-}"
    if [ -z "$ver" ]; then
        info "Resolving latest release..."
        ver="$(resolve_latest)"
    fi
    info "OS: ${os} | Arch: ${arch} | Version: ${ver}"

    asset="$(asset_for "$os" "$arch" "$ver")"
    if [ -z "$asset" ]; then
        err "No standalone binary for ${os}-${arch}."
        pip_fallback
        exit 1
    fi

    local base="https://github.com/${REPO_OWNER}/${REPO_NAME}/releases/download/v${ver}"
    WORKDIR="$(mktemp -d)" || die "Could not create a temporary directory."

    # The release's SHA256SUMS is the authoritative list of published binaries.
    # If our target isn't listed, no binary was built for this arch in this
    # release — fall back to the Python install rather than 404 on download.
    http_to "${base}/SHA256SUMS" "${WORKDIR}/SHA256SUMS"
    local expected actual
    expected="$(awk -v a="$asset" '$2 == a {print $1}' "${WORKDIR}/SHA256SUMS")"
    if [ -z "$expected" ]; then
        err "Release v${ver} has no ${os}-${arch} binary (${asset} not in SHA256SUMS)."
        pip_fallback
        exit 1
    fi

    info "Downloading ${asset}..."
    http_to "${base}/${asset}" "${WORKDIR}/${asset}"

    info "Verifying checksum..."
    actual="$(sha256_of "${WORKDIR}/${asset}")"
    if [ "$expected" != "$actual" ]; then
        die "Checksum mismatch for ${asset}: expected ${expected}, got ${actual}."
    fi
    ok "Checksum verified (sha256: ${actual:0:12}...)"

    local dir; dir="$(choose_install_dir)"
    mkdir -p "$dir" || die "Cannot create install directory: $dir"
    local dest="${dir}/${TOOL_NAME}"

    # Write atomically; use sudo only if the directory is not writable.
    chmod +x "${WORKDIR}/${asset}"
    if [ -w "$dir" ]; then
        mv -f "${WORKDIR}/${asset}" "$dest"
    elif have sudo; then
        warn "${dir} is not writable — using sudo to install."
        sudo mv -f "${WORKDIR}/${asset}" "$dest"
    else
        die "Cannot write to ${dir}. Re-run with CLAUSTER_INSTALL_DIR=~/.local/bin, or install sudo."
    fi
    ok "Installed ${dest}"

    # PATH hint
    case ":${PATH}:" in
        *":${dir}:"*) : ;;
        *) warn "${dir} is not on your PATH. Add it, e.g.:"
           warn "  echo 'export PATH=\"${dir}:\$PATH\"' >> ~/.bashrc && source ~/.bashrc" ;;
    esac

    # Verify: require a zero exit AND a 'clauster' identity banner — not merely
    # non-empty output (a failing binary could still print something to stdout).
    local got_ver=""
    if got_ver="$("$dest" --version 2>/dev/null)" && printf '%s' "$got_ver" | grep -qi '^clauster'; then
        ok "${got_ver} installed"
    else
        warn "Installed to ${dest}, but '${dest} --version' did not confirm a clauster binary."
    fi

    info "Clauster spawns the 'claude' CLI but does not vendor it — make sure Claude Code is on your PATH."
    ok "Installation complete!"
}

main "$@"
