#!/bin/bash
# Keep Frigate's recordings' audio. After a short camera dropout, Frigate's watchdog
# restarts its ffmpeg, and the recordings can stay silent until Frigate itself restarts
# (it happened twice on the rig). Run hourly from cron, this checks the newest segments; when they're
# silent it restarts Frigate and checks again:
# - the audio is back: a quiet notification;
# - still silent (the camera's own stream is broken, as on 10-16): an alert, at most every
#   6 h, to look at the camera.
# Frigate is restarted at most every 6 h, once no visit is in progress, and never during a
# deploy or the daily report (their lock).
#
#   7 * * * * /docker/stack/scripts/rig/audio-check.sh >> ~/.local/state/gatekeeper/rig.log 2>&1
set -euo pipefail
. "$(dirname "$0")/lib.sh"
recordings="${DATA_DIR:-./data}/frigate/storage/recordings"
alerted="$state/audio-alert"
restarted="$state/audio-restart"
every=21600  # 6 h

# silent: do the newest segments (not the one still being written) all lack audio?
silent() {
  local dir segments seg packets n=0 quiet=0
  dir="$recordings/$(date -u +%Y-%m-%d/%H)/front_door"
  [ -d "$dir" ] || dir="$recordings/$(date -u -d '-1 hour' +%Y-%m-%d/%H)/front_door"
  segments=$(ls -t "$dir" 2>/dev/null | sed -n '2,4p' || true)
  [ -z "$segments" ] && return 1
  for seg in $segments; do
    n=$((n + 1))
    packets=$(docker run --rm -v "$(realpath "$dir")":/r:ro "$(gatekeeper_image)" \
      ffprobe -v error -select_streams a:0 -count_packets -show_entries stream=nb_read_packets \
      -of csv=p=0 "/r/$seg" 2>/dev/null || echo 0)
    [ "${packets:-0}" -lt 50 ] && quiet=$((quiet + 1))
  done
  [ "$quiet" -eq "$n" ]
}

# older FILE SECONDS: is FILE missing, or last touched more than SECONDS ago?
older() {
  [ ! -f "$1" ] || [ $(( $(date +%s) - $(stat -c %Y "$1") )) -ge "$2" ]
}

frigate_healthy() {
  for _ in $(seq 30); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' frigate 2>/dev/null)" = healthy ] \
      && return 0
    sleep 10
  done
  return 1
}

silent || exit 0

if older "$restarted" "$every"; then
  for _ in $(seq 60); do visit_in_progress || break; sleep 10; done
  exec 9>"$state/deploy.lock"
  if flock -w 900 9; then
    touch "$restarted"
    echo "$(date -Is) recordings silent, restarting Frigate"
    docker compose restart frigate
    exec 9>&-
    # Healthy, then enough new segments to judge (they're 10 s each).
    if frigate_healthy && sleep 45 && ! silent; then
      echo "$(date -Is) recordings have audio again"
      notify "Restarted Frigate: recordings had no audio" \
        "The newest recordings were silent. Restarting Frigate brought the audio back." \
        2 wrench
      exit 0
    fi
  else
    echo "$(date -Is) recordings silent; a deploy or the report held the lock for 15 min"
  fi
fi

older "$alerted" "$every" || exit 0
touch "$alerted"
echo "$(date -Is) recordings silent, alerting"
notify "Frigate recordings have no audio" \
  "The newest recording segments are silent, and restarting Frigate didn't help: the
camera's stream may be broken. Power-cycle the doorbell, then on the rig:
cd $stack && docker compose restart go2rtc frigate gatekeeper" 4 warning
