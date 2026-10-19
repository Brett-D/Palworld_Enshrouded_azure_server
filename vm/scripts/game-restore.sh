#!/bin/bash
# Usage: game-restore.sh <backup-blob-name>
# Backs up the current world as "pre-restore", replaces it with the chosen backup,
# and restarts the game only if it was running.
set -u
source /usr/local/lib/games-common.sh

NAME="${1:-}"
[[ "$NAME" =~ ^(palworld|enshrouded)-[0-9]{8}-[0-9]{6}-[a-z-]+\.tar\.gz$ ]] || { echo "invalid backup name" >&2; exit 2; }
GAME="${BASH_REMATCH[1]}"
game_valid "$GAME" || { echo "$GAME is not enabled on this server" >&2; exit 2; }
BASE=$(save_base "$GAME")
DIR=$(save_name "$GAME")

take_lock wait || exit 3

TMP=$(mktemp /tmp/game-restore.XXXXXX)
trap 'rm -f "$TMP"' EXIT

[ "$(blob_get backups "$NAME" "$TMP")" = "200" ] || { echo "download failed" >&2; exit 4; }
tar -tzf "$TMP" >/dev/null 2>&1 || { echo "backup is not a valid archive" >&2; exit 5; }
tar -tzf "$TMP" | grep -q "^$DIR/" || { echo "archive has no $GAME save data" >&2; exit 6; }

logger -t game-restore "restoring $NAME"
WAS_RUNNING=0
if game_running "$GAME"; then
  WAS_RUNNING=1
  docker stop -t 90 "$GAME" >/dev/null
fi

if ! /usr/local/bin/game-backup.sh "$GAME" pre-restore >/dev/null; then
  logger -t game-restore "pre-restore backup failed, aborting"
  [ "$WAS_RUNNING" = 1 ] && docker start "$GAME" >/dev/null
  exit 7
fi

rm -rf "$BASE/$DIR".old.*
OLD=""
if [ -d "$BASE/$DIR" ]; then
  OLD="$BASE/$DIR.old.$(date -u +%s)"
  mv "$BASE/$DIR" "$OLD"
fi

if ! tar -xzf "$TMP" -C "$BASE"; then
  logger -t game-restore "extract failed, rolling back"
  rm -rf "${BASE:?}/$DIR"
  [ -n "$OLD" ] && mv "$OLD" "$BASE/$DIR"
  [ "$WAS_RUNNING" = 1 ] && docker start "$GAME" >/dev/null
  exit 8
fi

chown -R "$(save_owner "$GAME")" "$BASE/$DIR"
[ "$WAS_RUNNING" = 1 ] && docker start "$GAME" >/dev/null
logger -t game-restore "restored $NAME"
