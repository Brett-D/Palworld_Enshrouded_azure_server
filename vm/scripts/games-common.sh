# Shared helpers for the game server scripts. Sourced, not executed.

# Deployment-specific values (STORAGE_ACCOUNT, GAMES, IDLE_MINUTES, MAX_UPTIME_HOURS) are written here
# by the deploy scripts from gameservers.config.json.
[ -r /etc/gameservers.conf ] && source /etc/gameservers.conf

ACCOUNT="${STORAGE_ACCOUNT:?STORAGE_ACCOUNT missing from /etc/gameservers.conf}"
GAMES="${GAMES:-palworld enshrouded}"
LOCK=/run/games-ops.lock

# The VM's own subscription, resource group and name come from Azure's instance metadata service.
imds_compute() {
  local cache=/run/games/imds-compute.json
  if [ ! -s "$cache" ]; then
    mkdir -p /run/games
    curl -s -m 10 -H Metadata:true \
      "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01" > "$cache.tmp" && mv "$cache.tmp" "$cache"
  fi
  jq -r ".$1" "$cache"
}
SUB_ID=$(imds_compute subscriptionId)
RG=$(imds_compute resourceGroupName)
VM=$(imds_compute name)

game_valid() { [[ " $GAMES " == *" $1 "* ]]; }

game_dir() {
  case "$1" in
    palworld) echo /opt/palworld ;;
    enshrouded) echo /opt/enshrouded ;;
  esac
}

# Base folder and save folder name inside it, used by backup and restore.
save_base() {
  case "$1" in
    palworld) echo /opt/palworld/data/Pal ;;
    enshrouded) echo /opt/enshrouded/game/server ;;
  esac
}
save_name() {
  case "$1" in
    palworld) echo Saved ;;
    enshrouded) echo savegame ;;
  esac
}
save_owner() {
  case "$1" in
    palworld) echo 1000:1000 ;;
    enshrouded) echo 4711:4711 ;;
  esac
}

game_running() { [ -n "$(docker ps -q --filter "name=^$1\$")" ]; }

palworld_admin_pw() { grep -oP 'ADMIN_PASSWORD: "\K[^"]+' /opt/palworld/docker-compose.yml; }

# Prints the player count, or nothing if the server isn't answering yet.
game_players() {
  local n=""
  case "$1" in
    palworld)
      n=$(curl -s -m 10 -u "admin:$(palworld_admin_pw)" http://127.0.0.1:8212/v1/api/players | jq '.players | length' 2>/dev/null) ;;
    enshrouded)
      n=$(/usr/local/bin/a2s-players 127.0.0.1 15637 2>/dev/null) ;;
  esac
  [[ "$n" =~ ^[0-9]+$ ]] && echo "$n"
}

take_lock() {
  # $1: "wait" to block up to 10 minutes, anything else to give up immediately if busy.
  [ -n "${GAMES_LOCK_HELD:-}" ] && return 0
  exec 9>"$LOCK"
  if [ "${1:-}" = "wait" ]; then flock -w 600 9 || return 1; else flock -n 9 || return 1; fi
  export GAMES_LOCK_HELD=1
}

imds_token() {
  curl -s -m 15 -H Metadata:true \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=$1" | jq -r .access_token
}

blob_url() { echo "https://${ACCOUNT}.blob.core.windows.net/$1/$2"; }

# blob_get <container> <name> <outfile>: prints the HTTP status code.
blob_get() {
  curl -s -o "$3" -w '%{http_code}' -m 60 -H "Authorization: Bearer $(imds_token https://storage.azure.com/)" \
    -H "x-ms-version: 2023-11-03" "$(blob_url "$1" "$2")"
}

# blob_put <container> <name> <file> [content-type]: prints the HTTP status code.
blob_put() {
  curl -s -o /dev/null -w '%{http_code}' -m 300 -X PUT -T "$3" \
    -H "Authorization: Bearer $(imds_token https://storage.azure.com/)" -H "x-ms-version: 2023-11-03" \
    -H "x-ms-blob-type: BlockBlob" -H "Content-Type: ${4:-application/octet-stream}" "$(blob_url "$1" "$2")"
}

blob_delete() {
  curl -s -o /dev/null -w '%{http_code}' -m 30 -X DELETE \
    -H "Authorization: Bearer $(imds_token https://storage.azure.com/)" -H "x-ms-version: 2023-11-03" "$(blob_url "$1" "$2")"
}

# Prints the blob's Last-Modified as epoch seconds, or nothing if it doesn't exist.
blob_mtime() {
  local h
  h=$(curl -s -I -m 30 -H "Authorization: Bearer $(imds_token https://storage.azure.com/)" \
    -H "x-ms-version: 2023-11-03" "$(blob_url "$1" "$2")")
  echo "$h" | head -1 | grep -q ' 200' || return 0
  date -d "$(echo "$h" | grep -i '^last-modified:' | cut -d' ' -f2- | tr -d '\r')" +%s
}

desired_state() {
  # Prints "yes" or "no" for whether the portal asked for a game, or "unknown" if storage could not be
  # reached. Callers must not treat "unknown" as "no", or a network blip would stop a running game.
  local code auth
  auth="Authorization: Bearer $(imds_token https://storage.azure.com/)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -I -m 15 -H "$auth" -H "x-ms-version: 2023-11-03" \
    "$(blob_url config "desired-$1")")
  case "$code" in 200) echo yes ;; 404) echo no ;; *) echo unknown ;; esac
}

desired() { [ "$(desired_state "$1")" = yes ]; }

# Prints a JSON array of online players: [{name, id, level}] for Palworld, [{name}] for Enshrouded.
game_player_list() {
  local out=""
  case "$1" in
    palworld)
      out=$(curl -s -m 10 -u "admin:$(palworld_admin_pw)" http://127.0.0.1:8212/v1/api/players |
        jq -c '[.players[] | {name: (.name // "" | .[0:64]), id: (.userId // ""), level: (.level // null)}]' 2>/dev/null) ;;
    enshrouded)
      # Enshrouded's Steam query gives the count but blank names; names come from the server log.
      local since count
      since=$(docker inspect -f '{{.State.StartedAt}}' enshrouded 2>/dev/null)
      count=$(/usr/local/bin/a2s-players 127.0.0.1 15637 2>/dev/null)
      [ -n "$since" ] && out=$(docker logs --since "$since" enshrouded 2>&1 |
        python3 /usr/local/bin/enshrouded-players "${count:-}" 2>/dev/null) ;;
  esac
  echo "${out:-[]}"
}

# Prints the Palworld ban list as a JSON array of user ids.
palworld_bans() {
  local f=/opt/palworld/data/Pal/Saved/SaveGames/banlist.txt
  if [ -f "$f" ]; then
    grep -oE '^[A-Za-z0-9_-]{1,64}' "$f" | jq -R . | jq -sc .
  else
    echo '[]'
  fi
}

# Prints and clears a peak recorded by games-sampler.sh, or prints the fallback if there is none yet.
take_peak() {
  local f=/run/games/$1 v=""
  [ -f "$f" ] && read -r v < "$f" && rm -f "$f"
  [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo "$2"
}

# Prints the percentage of time since the previous call that some programs were stalled waiting
# for a resource (Linux pressure stall information): the most direct sign of an overloaded server.
pressure_pct() {
  local res=$1 state=/run/games/psi-$1.prev total now ptotal pnow
  [ -r "/proc/pressure/$res" ] || { echo null; return; }
  total=$(awk '/^some/{for(i=1;i<=NF;i++) if($i ~ /^total=/){sub("total=","",$i); print $i}}' "/proc/pressure/$res")
  now=$(( $(date +%s%N) / 1000 ))
  if [ -f "$state" ]; then
    read -r ptotal pnow < "$state"
    echo "$total $now" > "$state"
    (( now > pnow && total >= ptotal )) && { echo $(( 100 * (total - ptotal) / (now - pnow) )); return; }
  else
    echo "$total $now" > "$state"
  fi
  echo null
}

# Prints VM-wide usage as JSON. CPU is the average since the previous call (about a minute) and
# cpuMax/memMaxKb the highest 5-second values in that time; sizes are in kB so they stay exact.
vm_metrics() {
  local state=/run/games/cpu.prev cpu=null u n s i w q sq st idle total pidle ptotal
  mkdir -p /run/games
  read -r _ u n s i w q sq st _ < /proc/stat
  idle=$((i + w)); total=$((u + n + s + i + w + q + sq + st))
  if [ -f "$state" ]; then
    read -r pidle ptotal < "$state"
    (( total > ptotal )) && cpu=$(( 100 * ((total - ptotal) - (idle - pidle)) / (total - ptotal) ))
  fi
  echo "$idle $total" > "$state"
  local memTotal memAvail swapTotal swapFree diskTotal diskFree
  memTotal=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
  memAvail=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  swapTotal=$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)
  swapFree=$(awk '/^SwapFree:/{print $2}' /proc/meminfo)
  read -r diskTotal diskFree < <(df -Pk / | awk 'NR==2{print $2, $4}')
  jq -cn --argjson cpu "$cpu" --argjson cpuMax "$(take_peak cpumax "$cpu")" --argjson cores "$(nproc)" \
    --argjson mt "$memTotal" --argjson ma "$memAvail" --argjson memMax "$(take_peak memmax $((memTotal - memAvail)))" \
    --argjson stt "$swapTotal" --argjson sf "$swapFree" --argjson dt "$diskTotal" --argjson df "$diskFree" \
    --argjson pcpu "$(pressure_pct cpu)" --argjson pmem "$(pressure_pct memory)" \
    '{cpu: $cpu, cpuMax: $cpuMax, cores: $cores, memTotalKb: $mt, memAvailableKb: $ma, memMaxKb: $memMax,
      swapTotalKb: $stt, swapUsedKb: ($stt - $sf), diskTotalKb: $dt, diskFreeKb: $df,
      cpuPressure: $pcpu, memPressure: $pmem}'
}

# Prints a running game's container usage as JSON ({cpu, memKb}), or null.
# CPU is a percentage of the whole VM averaged since the previous call; memory excludes reclaimable file cache.
game_metrics() {
  local id cg usage now state pusage pt pid cpu=null mem
  id=$(docker inspect -f '{{.Id}}' "$1" 2>/dev/null) || { echo null; return; }
  cg=/sys/fs/cgroup/system.slice/docker-$id.scope
  [ -r "$cg/cpu.stat" ] || { echo null; return; }
  usage=$(awk '/^usage_usec/{print $2}' "$cg/cpu.stat")
  now=$(( $(date +%s%N) / 1000 ))
  state=/run/games/$1.cpu.prev
  if [ -f "$state" ]; then
    read -r pusage pt pid < "$state"
    if [ "$pid" = "$id" ] && (( now > pt && usage >= pusage )); then
      cpu=$(( 100 * (usage - pusage) / ((now - pt) * $(nproc)) ))
    fi
  fi
  echo "$usage $now $id" > "$state"
  mem=$(( ($(cat "$cg/memory.current") - $(awk '/^inactive_file /{print $2}' "$cg/memory.stat")) / 1024 ))
  jq -cn --argjson c "$cpu" --argjson cm "$(take_peak "$1.cpumax" "$cpu")" --argjson m "$mem" \
    --argjson mm "$(take_peak "$1.memmax" "$mem")" '{cpu: $c, cpuMax: $cm, memKb: $m, memMaxKb: $mm}'
}
