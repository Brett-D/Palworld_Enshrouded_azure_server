#!/bin/bash
# Runs every minute. Starts games the portal asked for, stops games that are idle or no longer
# wanted (with a backup), publishes status for the portal, and deallocates the VM when nothing runs.
set -u
source /usr/local/lib/games-common.sh

IDLE_MINUTES=${IDLE_MINUTES:-20}
MAX_UPTIME_HOURS=${MAX_UPTIME_HOURS:-8}
START_TIMEOUT_MINUTES=${START_TIMEOUT_MINUTES:-15}
IMAGE_REFRESH_DAYS=${IMAGE_REFRESH_DAYS:-7}
HISTORY_SAMPLES=480
BOOT_GRACE_SECONDS=600
STATE_DIR=/run/games
PERSIST_DIR=/var/lib/games
mkdir -p "$STATE_DIR" "$PERSIST_DIR"

take_lock nowait || exit 0

now=$(date +%s)
uptime_s=$(cut -d. -f1 /proc/uptime)
boot_time=$(( now - uptime_s ))

# Start requests left over from before this boot (e.g. after the nightly auto-shutdown) are dropped,
# so a VM start for one game doesn't also bring back the other.
if [ ! -f "$STATE_DIR/booted" ]; then
  for g in $GAMES; do
    m=$(blob_mtime config "desired-$g")
    if [ -n "$m" ] && (( m < boot_time - 900 )); then
      blob_delete config "desired-$g" >/dev/null
      logger -t games "dropped stale start request for $g"
    fi
  done
  touch "$STATE_DIR/booted"
fi

# The portal shows failed starts from config/failures.json ({game: {time, reason, log}}).
report_failure() {
  local g=$1 reason=$2 tmp logs
  tmp=$(mktemp /tmp/games-failures.XXXXXX)
  [ "$(blob_get config failures.json "$tmp")" = "200" ] && jq empty "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  # Last log lines help explain the failure; anything that looks like a credential is dropped.
  logs=$(docker logs --tail 60 "$g" 2>&1 | sed -E 's/\x1b\[[0-9;]*[A-Za-z]//g' | grep -viE 'password|passwd|secret|token|key=' |
    tail -20 | cut -c1-200 | jq -R . | jq -sc .)
  jq --arg g "$g" --argjson t "$now" --arg r "$reason" --argjson l "${logs:-[]}" '.[$g] = {time: $t, reason: $r, log: $l}' \
    "$tmp" > "$tmp.new" && blob_put config failures.json "$tmp.new" application/json >/dev/null
  rm -f "$tmp" "$tmp.new"
  logger -t games "$g failed to start: $reason"
}

clear_failure() {
  local g=$1 tmp
  tmp=$(mktemp /tmp/games-failures.XXXXXX)
  if [ "$(blob_get config failures.json "$tmp")" = "200" ] && jq -e --arg g "$g" 'has($g)' "$tmp" >/dev/null 2>&1; then
    jq --arg g "$g" 'del(.[$g])' "$tmp" > "$tmp.new" && blob_put config failures.json "$tmp.new" application/json >/dev/null
  fi
  rm -f "$tmp" "$tmp.new"
}

start_game() {
  local g=$1 dir stamp
  dir=$(game_dir "$g")
  # Retries (e.g. a container that keeps exiting) reuse the same attempt so the watchdog still fires.
  if [ ! -f "$STATE_DIR/$g.started" ]; then
    logger -t games "starting $g"
    echo "$now" > "$STATE_DIR/$g.started"
    clear_failure "$g"
    # Publish startup stages for the portal's progress bar. A separate transient unit, because a
    # oneshot service's leftover processes are stopped when it finishes.
    systemctl stop "game-start-watch-$g.service" 2>/dev/null
    systemd-run --quiet --collect --unit="game-start-watch-$g" /usr/local/bin/game-start-watch.sh "$g" "$now" ||
      logger -t games "could not start the progress watcher for $g"
    # Refresh the server image about weekly so a game patch that needs a newer image still starts.
    # The game files themselves update inside the container on every start.
    stamp="$PERSIST_DIR/$g.image-pulled"
    if [ ! -f "$stamp" ] || (( now - $(stat -c %Y "$stamp") >= IMAGE_REFRESH_DAYS * 86400 )); then
      if (cd "$dir" && timeout 600 docker compose pull -q >/dev/null 2>&1); then
        touch "$stamp"; logger -t games "refreshed $g server image"
      else
        logger -t games "$g image refresh failed, using the current image"
      fi
    fi
  else
    logger -t games "starting $g again (attempt began $(( (now - $(cat "$STATE_DIR/$g.started")) / 60 )) min ago)"
  fi
  /usr/local/bin/game-apply-settings.sh "$g" fetch
  (cd "$dir" && docker compose up -d >/dev/null 2>&1)
  [ -f "$STATE_DIR/$g.idle-since" ] || echo "$now" > "$STATE_DIR/$g.idle-since"
}

stop_game() {
  local g=$1 reason=$2 backup=${3:-backup}
  logger -t games "stopping $g: $reason"
  if [ "$g" = "palworld" ]; then
    curl -s -m 30 -X POST -u "admin:$(palworld_admin_pw)" http://127.0.0.1:8212/v1/api/save >/dev/null
    docker stop -t 60 palworld >/dev/null
  else
    docker stop -t 90 "$g" >/dev/null
  fi
  # A failed backup must never block the shutdown, or the VM would keep billing.
  if [ "$backup" = "backup" ]; then
    /usr/local/bin/game-backup.sh "$g" shutdown >/dev/null || logger -t games "$g shutdown backup failed"
  fi
  rm -f "$STATE_DIR/$g.idle-since" "$STATE_DIR/$g.started"
  blob_delete config "progress-$g.json" >/dev/null
}

deallocate() {
  logger -t games "deallocating VM: $1"
  blob_delete config history.json >/dev/null
  curl -s -m 60 -X POST -H "Authorization: Bearer $(imds_token https://management.azure.com/)" -H "Content-Length: 0" \
    "https://management.azure.com/subscriptions/${SUB_ID}/resourceGroups/${RG}/providers/Microsoft.Compute/virtualMachines/${VM}/deallocate?api-version=2024-03-01" >/dev/null
}

if (( uptime_s >= MAX_UPTIME_HOURS * 3600 )); then
  for g in $GAMES; do
    game_running "$g" && stop_game "$g" "max uptime reached"
    blob_delete config "desired-$g" >/dev/null
  done
  deallocate "max uptime reached"
  exit 0
fi

status='{}'
any_running=0
any_desired=0
for g in $GAMES; do
  case "$(desired_state "$g")" in
    yes) want=1 ;;
    no) want=0 ;;
    *)
      # Storage unreachable: leave the game as it is rather than guess.
      want=0; game_running "$g" && want=1
      logger -t games "could not read the start request for $g; leaving it as it is" ;;
  esac
  if (( want )) && ! game_running "$g"; then
    start_game "$g"
  elif (( ! want )) && game_running "$g"; then
    stop_game "$g" "stop requested"
  elif game_running "$g"; then
    players=$(game_players "$g")
    [ -f "$STATE_DIR/$g.idle-since" ] || echo "$now" > "$STATE_DIR/$g.idle-since"
    if [ -n "$players" ] && (( players > 0 )); then
      echo "$now" > "$STATE_DIR/$g.idle-since"
    fi
    idle_s=$(( now - $(cat "$STATE_DIR/$g.idle-since") ))
    if (( idle_s >= IDLE_MINUTES * 60 )); then
      stop_game "$g" "idle ${idle_s}s"
      blob_delete config "desired-$g" >/dev/null
      want=0
    fi
  fi

  # Start watchdog: a requested game that has not answered within START_TIMEOUT_MINUTES is stopped
  # and reported, so the portal can show why and the VM does not stay on for nothing.
  if (( want )) && [ -f "$STATE_DIR/$g.started" ]; then
    if game_running "$g" && [ -n "$(game_players "$g")" ]; then
      # The progress file is left as is (the watcher marks it "ready"); the portal ignores it once the
      # game shows as running, and the next start replaces it.
      rm -f "$STATE_DIR/$g.started"
    elif (( now - $(cat "$STATE_DIR/$g.started") >= START_TIMEOUT_MINUTES * 60 )); then
      report_failure "$g" "The server did not become ready within $START_TIMEOUT_MINUTES minutes."
      stop_game "$g" "did not become ready within $START_TIMEOUT_MINUTES minutes" nobackup
      blob_delete config "desired-$g" >/dev/null
      want=0
    fi
  fi

  running=false; ready=false; players=null; idle_min=0; list='[]'; bans='[]'; metrics=null
  if game_running "$g"; then
    running=true; any_running=1
    p=$(game_players "$g")
    if [ -n "$p" ]; then ready=true; players=$p; list=$(game_player_list "$g"); fi
    [ -f "$STATE_DIR/$g.idle-since" ] && idle_min=$(( (now - $(cat "$STATE_DIR/$g.idle-since")) / 60 ))
    [ "$g" = "palworld" ] && bans=$(palworld_bans)
    metrics=$(game_metrics "$g")
  fi
  (( want )) && any_desired=1
  status=$(echo "$status" | jq --arg g "$g" --argjson r "$running" --argjson rd "$ready" --argjson p "$players" \
    --argjson w "$( (( want )) && echo true || echo false)" --argjson i "$idle_min" \
    --argjson l "$list" --argjson b "$bans" --argjson m "${metrics:-null}" \
    '.[$g] = {running: $r, ready: $rd, players: $p, wanted: $w, idleMinutes: $i, list: $l, bans: $b, metrics: $m}')
done

TMP=$(mktemp /tmp/games-status.XXXXXX)
vmj=$(vm_metrics 2>/dev/null); [ -n "$vmj" ] || vmj=null
echo "$status" | jq --argjson t "$(date +%s)" --argjson u "$uptime_s" --argjson vm "$vmj" \
  '{updated: $t, uptime: $u, vm: $vm, games: .}' > "$TMP"
blob_put config status.json "$TMP" application/json >/dev/null

# Usage history for the portal's charts: one sample a minute, up to 8 hours, for the current
# session only (/run is cleared at boot and the uploaded copy is deleted when the VM deallocates).
HIST=/run/games/history.jsonl
jq -c '{t: .updated, cpu: .vm.cpu, cpuMax: .vm.cpuMax, mem: (if .vm then .vm.memTotalKb - .vm.memAvailableKb else null end),
        memMax: .vm.memMaxKb, memTotal: .vm.memTotalKb, pCpu: .vm.cpuPressure, pMem: .vm.memPressure,
        g: (.games | map_values(.metrics | if . then {cpu, cpuMax, mem: .memKb, memMax: .memMaxKb} else null end))}' "$TMP" >> "$HIST"
tail -n "$HISTORY_SAMPLES" "$HIST" > "$HIST.tmp" && mv "$HIST.tmp" "$HIST"
jq -sc . "$HIST" > "$TMP" && blob_put config history.json "$TMP" application/json >/dev/null
rm -f "$TMP"

if (( ! any_running && ! any_desired && uptime_s >= BOOT_GRACE_SECONDS )); then
  # Re-check right before deallocating in case someone pressed Start in the last few seconds.
  for g in $GAMES; do desired "$g" && exit 0; done
  deallocate "no games running"
fi
