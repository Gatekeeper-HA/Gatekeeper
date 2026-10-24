# Shared by the rig scripts (README, "Running a rig"): moves to the stack's checkout, loads
# .env, and sets up the state directory and notifications. Source it, don't run it.

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
# shellcheck disable=SC2034  # used by the scripts that source this
stack=$(pwd)
set -a; . ./.env; set +a
state="${XDG_STATE_HOME:-$HOME/.local/state}/gatekeeper"
mkdir -p "$state"

# notify TITLE MESSAGE [PRIORITY 1-5] [TAGS]: straight to ntfy, so it works while
# Gatekeeper itself is down.
notify() {
  curl -fsS -m 10 -o /dev/null \
    -H "Authorization: Bearer ${NTFY_GATEKEEPER_TOKEN:-}" \
    -H "Title: $1" -H "Priority: ${3:-3}" -H "Tags: ${4:-}" \
    --data-binary "$2" "http://${SERVER_IP}:8090/doorbell" \
    || echo "$(date -Is) ntfy failed: $1" >&2
}

# The image Gatekeeper runs (gatekeeper:local, or gatekeeper:local-cuda with the GPU).
gatekeeper_image() {
  docker inspect -f '{{.Config.Image}}' gatekeeper 2>/dev/null || echo gatekeeper:local
}

# Is a visit in progress? Gatekeeper's /healthz says how old the current one is.
visit_in_progress() {
  docker exec gatekeeper python -c '
import json, urllib.error, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:'"${HEALTH_PORT:-8099}"'/healthz", timeout=5)
except urllib.error.HTTPError as e:
    r = e
print(json.load(r).get("visit_age_seconds") is not None)' 2>/dev/null | grep -qx True
}

# A recreated container takes its log with it (#14, #59). keep_logs saves each container's
# last 24 h, with Docker's timestamps, to $state/logs/<name>-<id>.log before deploy.sh
# recreates it (overwritten if it isn't, so it's safe to run every time).
keep_logs() {
  local name id
  mkdir -p "$state/logs"
  for name in gatekeeper frigate; do
    id=$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null | cut -c1-12 || true)
    [ -n "$id" ] || continue
    docker logs --timestamps --since 24h "$name" > "$state/logs/$name-$id.log" 2>&1 || true
  done
  find "$state/logs" -name '*.log' -mtime +1 -delete 2>/dev/null || true
}

# day_log NAME: the container's last 24 h of log, after what keep_logs saved of the
# containers it replaced (oldest first, the window's lines only, timestamps stripped).
day_log() {
  local name=$1 id cutoff f
  id=$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null | cut -c1-12 || true)
  cutoff=$(date -u -d '-24 hour' +%Y-%m-%dT%H:%M:%S)
  for f in $(ls -tr "$state/logs/$name"-*.log 2>/dev/null); do
    [ "$f" = "$state/logs/$name-$id.log" ] && continue
    awk -v c="$cutoff" 'substr($1, 1, 19) >= c { sub(/^[^ ]* /, ""); print }' "$f"
  done
  docker logs --since 24h "$name" 2>&1
}

# kept_logs: the containers whose day_log includes a replaced container's log, as JSON.
kept_logs() {
  local name id cutoff f out=()
  cutoff=$(date -d '-24 hour' +%s)
  for name in gatekeeper frigate; do
    id=$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null | cut -c1-12 || true)
    for f in "$state/logs/$name"-*.log; do
      [ -f "$f" ] && [ "$f" != "$state/logs/$name-$id.log" ] \
        && [ "$(stat -c %Y "$f")" -ge "$cutoff" ] && { out+=("$name"); break; }
    done
  done
  printf '%s\n' "${out[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))'
}
