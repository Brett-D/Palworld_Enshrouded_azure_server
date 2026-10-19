#!/bin/bash
# Usage: game-apply-settings.sh <palworld|enshrouded> [fetch|now]
#   fetch: download the game's settings from blob storage into its settings.env (no restart).
#          For a stopped Enshrouded server, also writes the portal's roles into its JSON config.
#   now:   same, then if the game is running warn players (Palworld), back up, and restart it
#          (Enshrouded roles are written while it is stopped).
set -u
source /usr/local/lib/games-common.sh

GAME="${1:-}"
MODE="${2:-fetch}"
game_valid "$GAME" || { echo "invalid game" >&2; exit 2; }
ENVFILE="$(game_dir "$GAME")/settings.env"
TAG="${GAME}-settings"

[ "$MODE" = "now" ] && { take_lock wait || exit 3; }

TMP=$(mktemp /tmp/game-settings.XXXXXX)
ROLES_TMP=$(mktemp /tmp/game-roles.XXXXXX)
PROGRESS_TMP=$(mktemp /tmp/game-progress.XXXXXX)
trap 'rm -f "$TMP" "$ROLES_TMP" "$PROGRESS_TMP"' EXIT

# Enshrouded runs through Wine/Proton, which uses the kernel's ntsync driver (Linux 6.14+) for faster
# thread synchronization when /dev/ntsync is passed in. The override is removed on kernels without it
# so the container still starts.
ensure_ntsync_override() {
  local f=/opt/enshrouded/docker-compose.override.yml
  if [ -e /dev/ntsync ]; then
    printf 'services:\n  enshrouded:\n    devices:\n      - /dev/ntsync:/dev/ntsync\n' > "$f"
  else
    rm -f "$f"
  fi
}
[ "$GAME" = "enshrouded" ] && ensure_ntsync_override

# Replaces userGroups in enshrouded_server.json with the roles saved from the portal.
# Only call while the Enshrouded container is stopped.
apply_enshrouded_roles() {
  local json=/opt/enshrouded/game/server/enshrouded_server.json code src out
  code=$(blob_get config roles-enshrouded.json "$ROLES_TMP")
  [ "$code" = "200" ] || { logger -t "$TAG" "no roles blob (http $code), keeping current roles"; return 0; }
  src="$json"
  if [ ! -f "$json" ]; then
    # Brand-new server: seed a config with just the roles; the image and game fill in the rest.
    mkdir -p "$(dirname "$json")"
    src=$(mktemp /tmp/enshrouded-seed.XXXXXX)
    echo '{}' > "$src"
  fi
  out=$(mktemp /tmp/enshrouded-json.XXXXXX)
  if jq --slurpfile r "$ROLES_TMP" '
      ($r[0].roles // []) as $roles
      | if ($roles | length) == 0 or ([$roles[] | select((.name // "") == "")] | length) > 0 then error("bad roles") else . end
      | .userGroups = [ $roles[] | {
          name: (.name | tostring), password: (.password | tostring),
          canKickBan: (.canKickBan == true), canAccessInventories: (.canAccessInventories == true),
          canEditWorld: (.canEditWorld == true), canEditBase: (.canEditBase == true),
          canExtendBase: (.canExtendBase == true), reservedSlots: ((.reservedSlots // 0) | tonumber | floor) } ]
    ' "$src" > "$out" 2>/dev/null; then
    # cat preserves the file's owner and permissions.
    cat "$out" > "$json"
    chown "$(save_owner enshrouded)" "$json"
    logger -t "$TAG" "applied $(jq '.userGroups | length' "$json") roles"
  else
    logger -t "$TAG" "roles blob invalid, keeping current roles"
  fi
  rm -f "$out"
  [ "$src" != "$json" ] && rm -f "$src"
  return 0
}

CODE=$(blob_get config "settings-${GAME}.json" "$TMP")
if [ "$CODE" = "200" ]; then
  # Only well-formed KEY=VALUE pairs are written, so a bad blob can't inject anything into the environment.
  if jq -r 'to_entries[] | select((.key|test("^[A-Z][A-Z0-9_]*$")) and ((.value|tostring)|test("^[A-Za-z0-9 ._!-]+$"))) | "\(.key)=\(.value)"' "$TMP" > "${ENVFILE}.new"; then
    mv "${ENVFILE}.new" "$ENVFILE"
    logger -t "$TAG" "applied $(wc -l < "$ENVFILE") settings from blob"
  else
    rm -f "${ENVFILE}.new"
    logger -t "$TAG" "settings blob was not valid JSON, kept previous settings"
  fi
elif [ "$CODE" = "404" ]; then
  logger -t "$TAG" "no settings blob yet, using defaults"
else
  logger -t "$TAG" "settings download failed http=$CODE, kept previous settings"
  [ "$MODE" = "now" ] && exit 4
fi

# Palworld: one server worker thread per core (what the deprecated MULTITHREADING flag did), worked out
# at each start so it stays right after the VM is resized.
if [ "$GAME" = "palworld" ]; then
  touch "$ENVFILE"
  sed -i '/^WORKER_THREADS_SERVER=/d' "$ENVFILE"
  echo "WORKER_THREADS_SERVER=$(nproc)" >> "$ENVFILE"
fi

[ "$MODE" = "now" ] || { [ "$GAME" = "enshrouded" ] && ! game_running enshrouded && apply_enshrouded_roles; exit 0; }

if ! game_running "$GAME"; then
  [ "$GAME" = "enshrouded" ] && apply_enshrouded_roles
  exit 0
fi

# The portal shows the restart on the game's Start button from config/progress-<game>.json, the same
# file a normal start uses. This script publishes the steps before the container goes down, then the
# start watcher takes over until the game answers.
RESTART=$(date +%s)
restart_step() {
  jq -cn --arg s "$1" --arg l "$2" --argjson st "$RESTART" --argjson t "$(date +%s)" \
    '{stage: $s, pct: null, label: $l, started: $st, stageSince: $t, updated: $t, kind: "restart"}' > "$PROGRESS_TMP"
  blob_put config "progress-$GAME.json" "$PROGRESS_TMP" application/json >/dev/null
}
# Arms the manager's start watchdog, so a restart that never comes back is reported like a failed start.
mkdir -p /run/games && echo "$RESTART" > "/run/games/$GAME.started"

if [ "$GAME" = "palworld" ]; then
  restart_step warn "Warning players (30 seconds)"
  curl -s -m 10 -u "admin:$(palworld_admin_pw)" -H 'Content-Type: application/json' \
    -d '{"message":"Applying new game settings. Server restarting in 30 seconds."}' \
    http://127.0.0.1:8212/v1/api/announce >/dev/null
  sleep 30
  curl -s -m 30 -X POST -u "admin:$(palworld_admin_pw)" http://127.0.0.1:8212/v1/api/save >/dev/null
fi
restart_step backup "Backing up the world"
/usr/local/bin/game-backup.sh "$GAME" pre-settings >/dev/null || logger -t "$TAG" "pre-settings backup failed"
restart_step stop "Stopping the server"
if [ "$GAME" = "enshrouded" ]; then
  # Roles live in the server's JSON file, which must only be edited while the server is stopped.
  docker stop -t 90 enshrouded >/dev/null
  apply_enshrouded_roles
fi
LOG_SINCE=$(date +%s)
# --force-recreate so "restart now" restarts even when no setting changed.
(cd "$(game_dir "$GAME")" && docker compose up -d --force-recreate)
logger -t "$TAG" "container recreated with new settings"
# Started only now: while the old container was up the watcher would have reported it as ready.
systemctl stop "game-start-watch-$GAME.service" 2>/dev/null
systemd-run --quiet --collect --unit="game-start-watch-$GAME" \
  /usr/local/bin/game-start-watch.sh "$GAME" "$RESTART" restart "$LOG_SINCE" ||
  logger -t "$TAG" "could not start the progress watcher"
