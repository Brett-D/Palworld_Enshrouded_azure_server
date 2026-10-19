#!/bin/bash
# Samples CPU and memory every 5 seconds and records the highest value since the manager last read it,
# so per-minute charts still show short spikes. Writes /run/games/{cpumax,memmax} for the VM and
# /run/games/<game>.{cpumax,memmax} per game; the manager reads and deletes them each minute.
# Also notices start/stop requests within ~10 seconds and runs the manager straight away.
set -u
source /usr/local/lib/games-common.sh

INTERVAL=5
mkdir -p /run/games
declare -A prev_usage

read_vm() { read -r _ u n s i w q sq st _ < /proc/stat; echo "$((i + w)) $((u + n + s + i + w + q + sq + st))"; }

# Raises the stored maximum in file $1 to $2 if higher (the manager deletes the file after reading it).
bump() {
  local cur=0
  [ -f "$1" ] && read -r cur < "$1"
  (( $2 > cur )) && echo "$2" > "$1"
}

read -r pidle ptotal < <(read_vm)
pnow=$(( $(date +%s%N) / 1000 ))
tick=0; check_pid=""

# Every ~10 s, look for start/stop requests from the portal and run the manager right away instead of
# at its next minute. Runs in the background so it never delays sampling.
check_requests() {
  local g state
  for g in $GAMES; do
    state=$(desired_state "$g")
    if { [ "$state" = yes ] && ! game_running "$g"; } || { [ "$state" = no ] && game_running "$g"; }; then
      systemctl start --no-block games-reconcile.service
      return
    fi
  done
}

while sleep "$INTERVAL"; do
  tick=$(( tick + 1 ))
  if (( tick % 2 == 0 )) && { [ -z "$check_pid" ] || ! kill -0 "$check_pid" 2>/dev/null; }; then
    check_requests &
    check_pid=$!
  fi
  read -r idle total < <(read_vm)
  if (( total > ptotal )); then
    bump /run/games/cpumax $(( 100 * ((total - ptotal) - (idle - pidle)) / (total - ptotal) ))
  fi
  pidle=$idle; ptotal=$total
  bump /run/games/memmax $(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print t-a}' /proc/meminfo)

  now=$(( $(date +%s%N) / 1000 )); elapsed=$(( now - pnow )); pnow=$now
  for g in $GAMES; do
    id=$(docker inspect -f '{{.Id}}' "$g" 2>/dev/null) || continue
    cg=/sys/fs/cgroup/system.slice/docker-$id.scope
    [ -r "$cg/cpu.stat" ] || continue
    usage=$(awk '/^usage_usec/{print $2}' "$cg/cpu.stat")
    key="$g:$id"
    if [ -n "${prev_usage[$key]:-}" ] && (( elapsed > 0 && usage >= prev_usage[$key] )); then
      bump "/run/games/$g.cpumax" $(( 100 * (usage - prev_usage[$key]) / (elapsed * $(nproc)) ))
    fi
    prev_usage[$key]=$usage
    bump "/run/games/$g.memmax" $(( ($(cat "$cg/memory.current") - $(awk '/^inactive_file /{print $2}' "$cg/memory.stat")) / 1024 ))
  done
done
