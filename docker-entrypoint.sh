#!/bin/sh
# Make sure the config volume is writable, then run as PUID:PGID so that
# moved downloads end up owned by the same user as your media files.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/config}"
    chown "${PUID}:${PGID}" "${DATA_DIR:-/config}"
    chown "${PUID}:${PGID}" "${DATA_DIR:-/config}"/* 2>/dev/null || true
    exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups "$@"
fi
exec "$@"
