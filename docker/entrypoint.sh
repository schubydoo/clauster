#!/bin/sh
# PUID/PGID entrypoint (homelab convention): remap the bundled `clauster` user
# to the host's ids, own the writable mounts, then drop privileges and exec.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -g clauster)" != "$PGID" ]; then
    groupmod -o -g "$PGID" clauster
fi
if [ "$(id -u clauster)" != "$PUID" ]; then
    usermod -o -u "$PUID" clauster
fi

# Best-effort: a bind-mounted /config may already be owned correctly (and may be
# read-only); never fail startup on chown.
chown clauster:clauster /config 2>/dev/null || true

exec su-exec clauster:clauster "$@"
