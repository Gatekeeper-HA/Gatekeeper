#!/bin/bash
# Alert via ntfy when Frigate's newest recordings have no audio. Seen once: after Frigate's
# watchdog restarted its ffmpeg, recordings stayed silent until Frigate was restarted
# (`docker compose restart frigate`). Alerts at most every 6 h. Run hourly from cron:
#
#   7 * * * * /docker/stack/scripts/rig/audio-check.sh >> ~/.local/state/gatekeeper/rig.log 2>&1
set -euo pipefail
. "$(dirname "$0")/lib.sh"
recordings="${DATA_DIR:-./data}/frigate/storage/recordings"
alerted="$state/audio-alert"

dir="$recordings/$(date -u +%Y-%m-%d/%H)/front_door"
[ -d "$dir" ] || dir="$recordings/$(date -u -d '-1 hour' +%Y-%m-%d/%H)/front_door"
segments=$(ls -t "$dir" 2>/dev/null | sed -n '2,4p' || true)  # newest is still being written
[ -z "$segments" ] && exit 0

image=$(gatekeeper_image)
silent=0
for seg in $segments; do
  packets=$(docker run --rm -v "$(realpath "$dir")":/r:ro "$image" \
    ffprobe -v error -select_streams a:0 -count_packets -show_entries stream=nb_read_packets \
    -of csv=p=0 "/r/$seg" 2>/dev/null || echo 0)
  [ "${packets:-0}" -lt 50 ] && silent=$((silent + 1))
done
[ "$silent" -lt "$(wc -w <<<"$segments")" ] && exit 0

if [ -f "$alerted" ] && [ $(( $(date +%s) - $(stat -c %Y "$alerted") )) -lt 21600 ]; then
  exit 0
fi
touch "$alerted"
echo "$(date -Is) recordings silent, alerting"
notify "Frigate recordings have no audio" \
  "The newest recording segments are silent. Restart Frigate on the rig:
cd $stack && docker compose restart frigate" 4 warning
