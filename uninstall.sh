#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Clauster uninstaller (#816)
#
#   curl -fsSL https://raw.githubusercontent.com/schubydoo/clauster/main/uninstall.sh | bash
#
# The counterpart to install.sh. Auto-detects how clauster was installed (the
# standalone frozen binary from install.sh, or a `uv tool` / `pipx` / `pip`
# package), removes the right artifact, stops + removes a clauster service unit
# if one was installed, and removes the state directory (clauster.db, state.json,
# hosted_state.json, tls/, backups/, sockets, logs) and the config yaml.
#
# Safe by construction:
#   * --dry-run           list exactly what WOULD be removed; change nothing.
#   * confirmation prompt before any deletion (skip with -y).
#   * --keep-config       preserve clauster.yml (moved aside to a printed backup).
#   * --keep-data         preserve clauster.db (moved aside to a printed backup).
#   * never deletes a path outside the known clauster locations, and refuses a
#     state_dir that resolves to a dangerous root ($HOME, /, empty).
#   * fails closed: if no install can be identified, it reports and exits non-zero
#     rather than guessing and deleting.
#
# Environment overrides (mirror install.sh + the app's own resolution):
#   CLAUSTER_INSTALL_DIR   where the frozen binary lives; default: ~/.local/bin
#   CLAUSTER_STATE_DIR     state directory; default: read from config, else ~/.clauster
#   CLAUSTER_CONFIG        config path; default: the app's search order
# ============================================================================

TOOL_NAME="clauster"

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
have() { command -v "$1" >/dev/null 2>&1; }

DRY_RUN=0
ASSUME_YES=0
KEEP_CONFIG=0
KEEP_DATA=0

usage() {
    cat <<EOF
Usage: uninstall.sh [options]

  --dry-run        Show what would be removed without removing anything.
  -y, --yes        Do not prompt for confirmation.
  --keep-config    Preserve clauster.yml (moved aside to a printed backup path).
  --keep-data      Preserve clauster.db (moved aside to a printed backup path).
  -h, --help       Show this help.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)     DRY_RUN=1 ;;
        -y|--yes)      ASSUME_YES=1 ;;
        --keep-config) KEEP_CONFIG=1 ;;
        --keep-data)   KEEP_DATA=1 ;;
        -h|--help)     usage; exit 0 ;;
        *)             err "Unknown option: $1"; usage; exit 2 ;;
    esac
    shift
done

# --- Resolution ------------------------------------------------------------

# The config path, following the app's documented search order:
#   $CLAUSTER_CONFIG -> ./clauster.yml -> $CLAUSTER_HOME/clauster.yml
resolve_config() {
    # Expand a leading ~ the way the app (Path.expanduser) does — a user may set
    # CLAUSTER_CONFIG=~/custom/clauster.yml literally.
    local env_cfg; env_cfg="$(expand_tilde "${CLAUSTER_CONFIG:-}")"
    if [ -n "$env_cfg" ] && [ -f "$env_cfg" ]; then
        echo "$env_cfg"; return
    fi
    if [ -f "./clauster.yml" ]; then
        echo "$(pwd)/clauster.yml"; return
    fi
    local env_home; env_home="$(expand_tilde "${CLAUSTER_HOME:-}")"
    if [ -n "$env_home" ] && [ -f "${env_home}/clauster.yml" ]; then
        echo "${env_home}/clauster.yml"; return
    fi
    echo ""
}

# Expand a leading ~ to $HOME (the config stores paths like "~/.clauster").
expand_tilde() {
    case "$1" in
        "~") echo "${HOME}" ;;
        "~/"*) echo "${HOME}/${1#\~/}" ;;
        *) echo "$1" ;;
    esac
}

# The state directory: an explicit override, else the config's `state_dir:`, else
# the app default ~/.clauster. The grep is a best-effort read of a top-level scalar
# key (quotes stripped); an override or the default covers anything fancier.
resolve_state_dir() {
    if [ -n "${CLAUSTER_STATE_DIR:-}" ]; then
        expand_tilde "${CLAUSTER_STATE_DIR}"; return
    fi
    local cfg; cfg="$(resolve_config)"
    if [ -n "$cfg" ]; then
        local sd
        sd="$(grep -E '^[[:space:]]*state_dir:[[:space:]]*' "$cfg" 2>/dev/null \
              | head -n1 | sed -E 's/^[[:space:]]*state_dir:[[:space:]]*//; s/^["'\'']//; s/["'\'']$//; s/[[:space:]]+#.*$//' \
              | sed -E 's/[[:space:]]+$//')"
        if [ -n "$sd" ]; then expand_tilde "$sd"; return; fi
    fi
    echo "${HOME}/.clauster"
}

# Guard: refuse a state_dir that is empty, root, or a home directory — a bug in
# resolution must never turn into `rm -rf $HOME`.
state_dir_is_safe() {
    local d="$1"
    [ -n "$d" ] || return 1
    case "$d" in
        "/"|"${HOME}"|"${HOME}/") return 1 ;;
    esac
    # Must be an absolute path at least two segments deep (e.g. /home/u/.clauster).
    case "$d" in
        /*/*) return 0 ;;
        *) return 1 ;;
    esac
}

INSTALL_DIR="${CLAUSTER_INSTALL_DIR:-${HOME}/.local/bin}"
BINARY_PATH="${INSTALL_DIR}/${TOOL_NAME}"
STATE_DIR="$(resolve_state_dir)"
CONFIG_PATH="$(resolve_config)"
# Service unit paths. The defaults are where install-service writes them; the
# overrides exist so the uninstaller stays testable without a real service unit
# (the CI smoke test + local tests pin them at a throwaway path).
SYSTEMD_UNIT="${CLAUSTER_SYSTEMD_UNIT:-/etc/systemd/system/clauster.service}"
LAUNCHD_PLIST="${CLAUSTER_LAUNCHD_PLIST:-${HOME}/Library/LaunchAgents/org.clauster.daemon.plist}"

# --- Detection -------------------------------------------------------------
# Collect every install method present. Package managers and the frozen binary
# are normally mutually exclusive, but we detect + act on each independently so a
# mixed/leftover install is fully cleaned rather than half-removed.

DETECTED=()  # human-readable method labels, for the plan + fail-closed check

is_uv_tool() { have uv && uv tool list 2>/dev/null | grep -qiE "^clauster( |$|\b)"; }
is_pipx()    { have pipx && pipx list --short 2>/dev/null | grep -qiE "(^| )clauster( |$)"; }
is_pip()     {
    local pip=""
    have pip && pip="pip" || { have pip3 && pip="pip3"; }
    [ -n "$pip" ] || return 1
    "$pip" show clauster >/dev/null 2>&1
}
# A standalone binary: a real file at the known install path. Detected INDEPENDENTLY
# of the package managers so a stale binary that coexists with a uv/pipx/pip install is
# still removed — the binary rm runs AFTER the package uninstalls (see the removal
# order below), so if the file was a package shim it's already gone (rm is a no-op) and
# a genuine leftover is cleaned up.
is_binary() { [ -f "$BINARY_PATH" ]; }

is_uv_tool && DETECTED+=("uv tool")
is_pipx    && DETECTED+=("pipx")
is_pip     && DETECTED+=("pip")
is_binary  && DETECTED+=("binary:${BINARY_PATH}")

# --- Plan (what will be removed) -------------------------------------------

info "Clauster uninstaller${DRY_RUN:+ (dry run)}"
echo
info "Detected install method(s): ${DETECTED[*]:-none}"
info "State directory:            ${STATE_DIR}$( [ -d "$STATE_DIR" ] || printf ' (absent)')"
info "Config file:                ${CONFIG_PATH:-<none found>}"
if [ -e "$SYSTEMD_UNIT" ]; then info "systemd unit:               ${SYSTEMD_UNIT}"; fi
if [ -e "$LAUNCHD_PLIST" ]; then info "launchd agent:              ${LAUNCHD_PLIST}"; fi
echo

if [ ${#DETECTED[@]} -eq 0 ] && [ ! -d "$STATE_DIR" ] && [ -z "$CONFIG_PATH" ] \
   && [ ! -e "$SYSTEMD_UNIT" ] && [ ! -e "$LAUNCHD_PLIST" ]; then
    die "No clauster install found (no binary/package, state dir, config, or service). Nothing to do."
fi

# Fail closed: a leftover state dir / config with NO identifiable install method is
# ambiguous — report it and let the operator remove it explicitly, don't guess.
if [ ${#DETECTED[@]} -eq 0 ]; then
    warn "Could not identify how clauster was installed (no binary/package on PATH)."
    warn "Leaving files in place. If you know they're clauster's, remove them manually:"
    [ -d "$STATE_DIR" ] && warn "  rm -rf ${STATE_DIR}"
    [ -n "$CONFIG_PATH" ] && warn "  rm -f ${CONFIG_PATH}"
    die "Refusing to guess-and-delete."
fi

# --- Confirm ---------------------------------------------------------------

if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    printf "%bProceed with removal? [y/N]%b " "$YELLOW" "$NC"
    read -r reply </dev/tty || reply=""
    case "$reply" in
        y|Y|yes|YES) : ;;
        *) info "Aborted."; exit 0 ;;
    esac
fi

# --- Actions ---------------------------------------------------------------
# Every mutating action funnels through run()/rm_path() so --dry-run prints the
# exact command and does nothing.

run() {
    if [ "$DRY_RUN" -eq 1 ]; then printf "  would: %s\n" "$*"; else "$@"; fi
}

rm_path() {  # rm_path <path> [rm-flags...]
    local path="$1"; shift || true
    [ -e "$path" ] || [ -L "$path" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then printf "  would remove: %s\n" "$path"; else rm "$@" "$path"; fi
}

# 1) Service unit (best-effort; the privileged step is surfaced, never assumed).
# The unit NAME is derived from the resolved unit PATH — never hardcoded — so an
# override ($CLAUSTER_SYSTEMD_UNIT, used by the tests) fully isolates stop/disable
# too, and can't target the real `clauster.service` while pointing the rm elsewhere.
if [ -e "$SYSTEMD_UNIT" ]; then
    unit_name="$(basename "$SYSTEMD_UNIT")"
    info "Removing systemd service '${unit_name}' (needs privileges)..."
    if have systemctl; then
        run sudo systemctl stop "$unit_name"
        run sudo systemctl disable "$unit_name"
    fi
    run sudo rm -f "$SYSTEMD_UNIT"
    run sudo systemctl daemon-reload
fi
if [ -e "$LAUNCHD_PLIST" ]; then
    info "Removing launchd agent..."
    have launchctl && run launchctl unload "$LAUNCHD_PLIST"
    rm_path "$LAUNCHD_PLIST" -f
fi

# 2) The binary / package, per detected method.
for m in "${DETECTED[@]:-}"; do
    case "$m" in
        "uv tool") info "Removing uv tool..."; run uv tool uninstall clauster ;;
        "pipx")    info "Removing pipx package..."; run pipx uninstall clauster ;;
        "pip")     info "Removing pip package..."
                   if have pip; then run pip uninstall -y clauster; else run pip3 uninstall -y clauster; fi ;;
        binary:*)  info "Removing binary..."; rm_path "${m#binary:}" -f ;;
    esac
done

# 3) Preserve config / data on request (moved aside to a printed backup path).
BACKUP_DIR="${HOME}/clauster-uninstall-backup"
preserve() {  # preserve <src> <label>
    local src="$1" label="$2"
    [ -f "$src" ] || return 0
    if [ "$DRY_RUN" -eq 1 ]; then
        printf "  would keep %s: move %s -> %s/\n" "$label" "$src" "$BACKUP_DIR"; return
    fi
    mkdir -p "$BACKUP_DIR"
    mv -f "$src" "$BACKUP_DIR/"
    ok "Kept ${label}: ${BACKUP_DIR}/$(basename "$src")"
}
[ "$KEEP_CONFIG" -eq 1 ] && [ -n "$CONFIG_PATH" ] && preserve "$CONFIG_PATH" "config"
[ "$KEEP_DATA" -eq 1 ] && preserve "${STATE_DIR}/clauster.db" "database"

# 4) State directory (guarded). Removed WHOLE, in both modes: with --keep-data the
# preserve step above already moved clauster.db out to the backup, so nothing else
# under state_dir (session.secret / session.epoch auth material, sockets, logs) is
# left behind — a selective child list would silently strand exactly those secrets.
if [ -d "$STATE_DIR" ]; then
    if state_dir_is_safe "$STATE_DIR"; then
        info "Removing state directory..."
        rm_path "$STATE_DIR" -rf
    else
        warn "Refusing to remove an unsafe state_dir path: ${STATE_DIR}"
    fi
fi

# 5) Config yaml (unless kept, and only when it wasn't already moved aside).
if [ "$KEEP_CONFIG" -eq 0 ] && [ -n "$CONFIG_PATH" ] && [ -f "$CONFIG_PATH" ]; then
    info "Removing config file..."
    rm_path "$CONFIG_PATH" -f
fi

echo
if [ "$DRY_RUN" -eq 1 ]; then
    ok "Dry run complete — nothing was removed."
else
    ok "Clauster uninstalled."
    warn "Claude Code (the 'claude' CLI) was installed separately and is left untouched."
fi
