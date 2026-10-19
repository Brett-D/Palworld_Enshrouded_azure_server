#!/bin/bash
# Usage: game-backup.sh <palworld|enshrouded|running> <label>
# Uploads a tar.gz of the game's save folder to the backups container and prints the blob name.
set -u
source /usr/local/lib/games-common.sh

GAME="${1:-}"
LABEL="${2:-manual}"
[[ "$LABEL" =~ ^[a-z-]+$ ]] || { echo "invalid label" >&2; exit 2; }

if [ "$GAME" = "running" ]; then
  rc=0
  for g in $GAMES; do
    if game_running "$g"; then "$0" "$g" "$LABEL" || rc=$?; fi
  done
  exit $rc
fi
game_valid "$GAME" || { echo "invalid game" >&2; exit 2; }

take_lock wait || exit 3

BASE=$(save_base "$GAME")
DIR=$(save_name "$GAME")
if [ ! -d "$BASE/$DIR" ]; then
  logger -t game-backup "$GAME has no save data yet, skipping"
  echo "no save data"
  exit 0
fi

if [ "$GAME" = "palworld" ] && game_running palworld; then
  curl -s -m 30 -X POST -u "admin:$(palworld_admin_pw)" http://127.0.0.1:8212/v1/api/save >/dev/null
  sleep 10
fi

NAME="${GAME}-$(date -u +%Y%m%d-%H%M%S)-${LABEL}.tar.gz"
TMP=$(mktemp /tmp/game-backup.XXXXXX)
trap 'rm -f "$TMP"' EXIT

# tar exit 1 means files changed while reading, which is acceptable for a live save.
tar -czf "$TMP" -C "$BASE" --exclude="$DIR/Logs" --exclude="$DIR/Crashes" "$DIR"
rc=$?
if (( rc > 1 )); then logger -t game-backup "$GAME tar failed rc=$rc"; exit 4; fi

CODE=$(blob_put backups "$NAME" "$TMP" application/gzip)
if [ "$CODE" = "201" ]; then
  logger -t game-backup "uploaded $NAME"
  echo "$NAME"
else
  logger -t game-backup "$GAME upload failed http=$CODE"
  exit 5
fi
