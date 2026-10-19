#!/bin/bash
# Usage: game-start-watch.sh <game> <start-epoch> [start|restart] [log-since-epoch]
# Runs only while a game is starting (launched by games-reconcile.sh, or by game-apply-settings.sh for
# a restart, which reads the log only from when the new container started). Every few seconds it reads the
# game's log, works out the startup stage and publishes it to config/progress-<game>.json for the
# portal's progress bar. Exits when the game answers, the start ends (ready, failed or stopped), or
# after the start timeout.
set -u
source /usr/local/lib/games-common.sh

GAME="${1:-}"; START="${2:-}"; KIND="${3:-start}"; LOG_SINCE="${4:-$START}"
game_valid "$GAME" || exit 2
[[ "$START" =~ ^[0-9]+$ && "$LOG_SINCE" =~ ^[0-9]+$ ]] || exit 2
[[ "$KIND" =~ ^(start|restart)$ ]] || exit 2
TIMEOUT=$(( ${START_TIMEOUT_MINUTES:-15} * 60 + 60 ))
STARTED=/run/games/$GAME.started
TMP=$(mktemp /tmp/game-progress.XXXXXX)
trap 'rm -f "$TMP"' EXIT

last_key=""; last_stage=""; last_put=0; stage_since=$(date +%s)

# publish <stage> <pct|null> <label>: uploads only when something changed, or every 15s as a heartbeat.
publish() {
  local now; now=$(date +%s)
  [ "$1" != "$last_stage" ] && stage_since=$now
  if [ "$1|$2|$3" != "$last_key" ] || (( now - last_put >= 15 )); then
    jq -cn --arg s "$1" --argjson p "$2" --arg l "$3" --argjson st "$START" --argjson ss "$stage_since" --argjson t "$now" \
      --arg k "$KIND" '{stage: $s, pct: $p, label: $l, started: $st, stageSince: $ss, updated: $t, kind: $k}' > "$TMP"
    blob_put config "progress-$GAME.json" "$TMP" application/json >/dev/null
    last_key="$1|$2|$3"; last_stage=$1; last_put=$now
  fi
}

# Prints "<stage>\t<pct|null>\t<label>" from the game's log since this start. Later stages win.
classify() {
  local log=$1 pct="null" upd
  upd=$(grep -E 'Update state \(0x[0-9a-f]+\) [a-z ]+, progress: [0-9.]+' <<< "$log" | tail -1)
  if [ -n "$upd" ]; then
    pct=$(grep -oE 'progress: [0-9.]+' <<< "$upd" | grep -oE '[0-9.]+$' | awk '{printf "%d", $1}')
  fi
  case "$GAME" in
    palworld)
      if grep -qE 'Running Palworld dedicated server|REST API started' <<< "$log"; then printf 'world\tnull\tLoading the world\n'
      elif grep -qE 'Starting Server|Compiling PalWorldSettings' <<< "$log"; then printf 'server\tnull\tStarting the game server\n'
      elif grep -q 'verifying' <<< "$upd"; then printf 'update\t%s\tVerifying game files\n' "$pct"
      elif [ -n "$upd" ]; then printf 'update\t%s\tDownloading game update\n' "$pct"
      elif grep -q 'Checking for new Palworld Server updates' <<< "$log"; then printf 'update\tnull\tChecking for game updates\n'
      else printf 'launch\tnull\tPreparing the game server\n'
      fi ;;
    enshrouded)
      if grep -qE "HostOnline|Savegame deserialization" <<< "$log"; then printf 'world\tnull\tLoading the world\n'
      elif grep -qE 'enshrouded-server.*(\[game_server\]|Initializ|\[ecss\])' <<< "$log"; then printf 'server\tnull\tStarting the game server\n'
      elif grep -q 'verifying' <<< "$upd"; then printf 'update\t%s\tVerifying game files\n' "$pct"
      elif [ -n "$upd" ]; then printf 'update\t%s\tDownloading game update\n' "$pct"
      elif grep -qE 'enshrouded-updater|Checking for available update' <<< "$log"; then printf 'update\tnull\tChecking for game updates\n'
      else printf 'launch\tnull\tPreparing the game server\n'
      fi ;;
  esac
}

while (( $(date +%s) - START < TIMEOUT )); do
  # The manager removes this file when the start ends (ready, failed or stopped).
  [ -f "$STARTED" ] || exit 0
  if game_running "$GAME"; then
    if [ -n "$(game_players "$GAME")" ]; then
      publish ready 100 "Ready"
      # Let the portal see "Running" now instead of at the manager's next minute.
      systemctl start --no-block games-reconcile.service
      exit 0
    fi
    IFS=$'\t' read -r stage pct label < <(classify "$(docker logs --since "$LOG_SINCE" "$GAME" 2>&1 | tail -n 400)")
    publish "${stage:-launch}" "${pct:-null}" "${label:-Preparing the game server}"
  else
    publish launch null "Preparing the game server"
  fi
  sleep 3
done
