#!/bin/bash
# Usage: game-admin.sh palworld <kick|ban|unban> <user-id>
# Moderation actions run from the portal. Only Palworld has a remote admin API.
set -u
source /usr/local/lib/games-common.sh

GAME="${1:-}"; ACTION="${2:-}"; USERID="${3:-}"
[ "$GAME" = "palworld" ] || { echo "only palworld supports remote moderation" >&2; exit 2; }
[[ "$ACTION" =~ ^(kick|ban|unban)$ ]] || { echo "invalid action" >&2; exit 2; }
[[ "$USERID" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || { echo "invalid user id" >&2; exit 2; }
game_running palworld || { echo "palworld is not running" >&2; exit 3; }

case "$ACTION" in
  kick) BODY=$(jq -cn --arg u "$USERID" '{userid: $u, message: "You were kicked by an admin."}') ;;
  ban) BODY=$(jq -cn --arg u "$USERID" '{userid: $u, message: "You were banned by an admin."}') ;;
  unban) BODY=$(jq -cn --arg u "$USERID" '{userid: $u}') ;;
esac

CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 15 -X POST -u "admin:$(palworld_admin_pw)" \
  -H 'Content-Type: application/json' -d "$BODY" "http://127.0.0.1:8212/v1/api/$ACTION")
logger -t game-admin "palworld $ACTION $USERID -> http $CODE"
# Refresh the portal's player list right away instead of waiting for the next minute.
systemctl start --no-block games-reconcile.service
[ "$CODE" = "200" ]
