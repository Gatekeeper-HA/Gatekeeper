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
