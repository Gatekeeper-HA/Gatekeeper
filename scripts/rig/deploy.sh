#!/bin/bash
# Deploy every merge: run every minute from cron, and the rig follows DEPLOY_BRANCH (main),
# live within a minute or two of each merge (README, "Running a rig").
#
#   * * * * * /docker/stack/scripts/rig/deploy.sh >> ~/.local/state/gatekeeper/deploy.log 2>&1
#
# - Builds while the old containers keep running, so a failed build changes nothing.
# - Restarts once no visit is in progress, and only what changed: services whose image or
#   compose settings changed (`docker compose up -d`), and services whose config files
#   changed (a git checkout replaces the file, and the container would keep the old one).
# - Then checks that Gatekeeper turns healthy (10 min) and Frigate gets camera frames
#   (3 min). If not, it rolls back to the commit that was running and alerts.
# - Commits that only touch docs, tests or CI just move the checkout.
# - Each deploy is added to deploys.jsonl (for the daily report) and sent via ntfy.
#
# It only moves forward along the branch. While the checkout is on something else (you
# checked out a branch to try it) or has local changes, it waits and says so once.
# `touch ~/.local/state/gatekeeper/deploy-paused` pauses it.

main() {
  set -euo pipefail
  . "$(dirname "$0")/lib.sh"
  exec 9>"$state/deploy.lock"
  flock -n 9 || return 0  # a deploy or the daily report is running
  [ -e "$state/deploy-paused" ] && return 0
  branch="${DEPLOY_BRANCH:-main}"

  git fetch -q origin "$branch"
  target=$(git rev-parse "origin/$branch")
  current=$(git rev-parse HEAD)
  subject=$(git log -1 --format=%s "$target")
  if [ "$target" = "$current" ]; then
    rm -f "$state/deploy-waiting"
    return 0
  fi
  [ "$(cat "$state/deploy-failed" 2>/dev/null)" = "$target" ] && return 0
  if ! git diff --quiet HEAD; then
    wait_once "local changes" "The rig's checkout has local changes, so $branch isn't deployed."
    return 0
  fi
  if ! git merge-base --is-ancestor "$current" "$target"; then
    wait_once "off $branch" "The rig runs $(git log -1 --format='%h %s'), which isn't on" \
      "$branch, so $branch isn't deployed until the rig is back on it."
    return 0
  fi
  rm -f "$state/deploy-waiting"

  changed=$(git diff --name-only "$current" "$target")
  if ! grep -qE '^(src/|Dockerfile|\.dockerignore|requirements|pyproject\.toml|VERSION|docker-compose|frigate/|go2rtc/|mosquitto/)' <<<"$changed"; then
    git checkout -q --detach "$target"
    record moved ""
    return 0
  fi
  # Services that read a changed config file.
  services=$(sed -nE 's#^(frigate|go2rtc)/.*#\1#p; s#^mosquitto/.*#mqtt#p' <<<"$changed" | sort -u | xargs)

  log "building $(short "$target") $subject"
  git checkout -q --detach "$target"
  if ! out=$(build 2>&1); then
    git checkout -q --detach "$current"
    echo "$target" > "$state/deploy-failed"
    record build-failed "$(tail -n 3 <<<"$out" | tr '\n' ' ')"
    notify "Deploy failed: $subject" "$(short "$target") didn't build; the rig still runs
$(short "$current").
$(tail -n 5 <<<"$out")" 4 warning
    return 1
  fi

  for _ in $(seq 60); do visit_in_progress || break; sleep 10; done
  restart
  if ! healthy; then
    rollback "Gatekeeper wasn't healthy after 10 minutes"
  elif ! frames; then
    rollback "Frigate got no camera frames for 3 minutes"
  else
    record deployed ""
    notify "Deployed: $subject" "$(short "$target") is live on the rig." 2 rocket
  fi
}

build() {
  if grep -q '^docker-compose' <<<"$changed"; then
    docker compose pull -q --ignore-buildable || return 1
  fi
  docker compose build
}

restart() {
  docker compose up -d
  [ -z "$services" ] || docker compose restart $services
}

rollback() {
  log "rolling back to $(short "$current"): $1"
  git checkout -q --detach "$current"
  docker compose build -q || true
  restart
  echo "$target" > "$state/deploy-failed"
  record rolled-back "$1"
  if healthy; then
    notify "Deploy rolled back: $subject" "$(short "$target"): $1. The rig is back on
$(short "$current")." 4 warning
  else
    notify "Gatekeeper is down" "Deploying $(short "$target") failed ($1), and Gatekeeper
isn't healthy after rolling back to $(short "$current") either." 5 rotating_light
  fi
}

visit_in_progress() {
  docker exec gatekeeper python -c '
import json, urllib.error, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:'"${HEALTH_PORT:-8099}"'/healthz", timeout=5)
except urllib.error.HTTPError as e:
    r = e
print(json.load(r).get("visit_age_seconds") is not None)' 2>/dev/null | grep -qx True
}

healthy() {
  for _ in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' gatekeeper 2>/dev/null)" = healthy ] \
      && return 0
    sleep 10
  done
  return 1
}

frames() {
  local fps
  for _ in $(seq 18); do
    fps=$(docker exec frigate curl -s -m 5 http://127.0.0.1:5000/api/stats 2>/dev/null \
      | jq -r '[.cameras[]?.camera_fps // 0] | min // 0' 2>/dev/null || echo 0)
    awk -v f="${fps:-0}" 'BEGIN { exit !(f >= 1) }' && return 0
    sleep 10
  done
  return 1
}

wait_once() {
  local key=$1
  shift
  [ "$(cat "$state/deploy-waiting" 2>/dev/null)" = "$key" ] && return 0
  echo "$key" > "$state/deploy-waiting"
  log "waiting: $*"
  notify "Auto-deploy is waiting" "$*" 3 pause_button
}

record() {
  jq -nc --argjson ts "$(date +%s)" --arg from "$current" --arg to "$target" \
    --arg subject "$subject" --arg result "$1" --arg detail "$2" \
    '{ts: $ts, from: $from, to: $to, subject: $subject, result: $result, detail: $detail}' \
    >> "$state/deploys.jsonl"
  log "$1 $(short "$target") $subject${2:+ ($2)}"
}

log() { echo "$(date -Is) $*"; }
short() { git rev-parse --short "$1"; }

# The checkout this script lives in changes under it; main runs from what bash has
# already read.
main "$@"
exit
