#!/bin/bash
# Alert via ntfy when Frigate's newest recordings have no audio. Seen once: after Frigate's
# watchdog restarted its ffmpeg, recordings stayed silent until Frigate was restarted
# (`docker compose restart frigate`). Alerts at most every 6 h. Run hourly from cron:
#
#   7 * * * * /docker/stack/scripts/soak/audio-check.sh >> /tmp/gatekeeper-soak.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a
recordings="${DATA_DIR:-./data}/frigate/storage/recordings"
state="$HOME/.cache/gatekeeper-audio-alert"

dir="$recordings/$(date -u +%Y-%m-%d/%H)/front_door"
[ -d "$dir" ] || dir="$recordings/$(date -u -d '-1 hour' +%Y-%m-%d/%H)/front_door"
segments=$(ls -t "$dir" 2>/dev/null | sed -n '2,4p' || true)  # newest is still being written
[ -z "$segments" ] && exit 0

silent=0
for seg in $segments; do
  packets=$(docker run --rm -v "$(realpath "$dir")":/r:ro gatekeeper:local \
    ffprobe -v error -select_streams a:0 -count_packets -show_entries stream=nb_read_packets \
    -of csv=p=0 "/r/$seg" 2>/dev/null || echo 0)
  [ "${packets:-0}" -lt 50 ] && silent=$((silent + 1))
done
[ "$silent" -lt "$(wc -w <<<"$segments")" ] && exit 0

if [ -f "$state" ] && [ $(( $(date +%s) - $(stat -c %Y "$state") )) -lt 21600 ]; then
  exit 0
fi
mkdir -p "$(dirname "$state")" && touch "$state"
echo "$(date): recordings silent, alerting"
docker exec gatekeeper python -c '
from gatekeeper.app import build_notify_backends
from gatekeeper.config import Settings
from gatekeeper.notify import Notification
n = Notification(
    "Frigate recordings have no audio",
    "The newest recording segments are silent. Restart Frigate on the rig:\n"
    "cd /docker/stack && docker compose restart frigate",
    ["warning"], 4)
for b in build_notify_backends(Settings()):
    b.send(n, None)
'
