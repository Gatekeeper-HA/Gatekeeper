#!/bin/bash
# Daily soak report, sent via ntfy. Gathers what only the host can see (container
# restarts, watchdog exits, Frigate's recording files), then runs daily_report.py in the
# gatekeeper container. Run it from cron, e.g. at 20:00 Central (01:00 UTC):
#
#   0 1 * * * /docker/stack/scripts/soak/daily-report.sh >> /tmp/gatekeeper-soak.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/../.."
here=$(pwd)
set -a; . ./.env; set +a
recordings="${DATA_DIR:-./data}/frigate/storage/recordings"

# Watchdog exits survive container restarts (same container, same log).
hang_exits=$(docker logs --since 24h gatekeeper 2>&1 | grep -c "internal hang" || true)
started=$(docker inspect -f '{{.State.StartedAt}}' gatekeeper | cut -c1-16)
health=$(docker inspect -f '{{.State.Health.Status}}' gatekeeper)

# Restarts since the last report: Docker's RestartCount is per container (a deploy
# recreates it and starts again from 0), so compare with what the last run saw.
count=$(docker inspect -f '{{.RestartCount}}' gatekeeper)
created=$(docker inspect -f '{{.Created}}' gatekeeper)
statef="$HOME/.cache/gatekeeper-restarts"
prev_created=""; prev_count=0
[ -f "$statef" ] && read -r prev_created prev_count < "$statef"
if [ "$prev_created" = "$created" ]; then restarts=$((count - prev_count)); else restarts=$count; fi
mkdir -p "$(dirname "$statef")" && echo "$created $count" > "$statef"

# Recording audio: the first segment of each hour in the last 24 h (hours with no
# motion may have no segments, which is fine).
silent='[]'
for h in $(seq 0 23); do
  dir="$recordings/$(date -u -d "-$h hour" +%Y-%m-%d/%H)/front_door"
  first=$(ls "$dir" 2>/dev/null | head -1 || true)
  [ -z "$first" ] && continue
  packets=$(docker run --rm -v "$(realpath "$dir")":/r:ro gatekeeper:local \
    ffprobe -v error -select_streams a:0 -count_packets -show_entries stream=nb_read_packets \
    -of csv=p=0 "/r/$first" 2>/dev/null || echo 0)
  if [ "${packets:-0}" -lt 50 ]; then
    silent=$(jq -c --arg t "$(date -d "-$h hour" '+%a %H:00')" '. + [$t]' <<<"$silent")
  fi
done

host=$(jq -nc --argjson hang_exits "$hang_exits" --argjson restarts "$restarts" \
  --argjson silent "$silent" --arg started "$started" --arg health "$health" \
  '{hang_exits: $hang_exits, restarts: $restarts, silent_audio_hours: $silent,
    started: $started, health: $health}')

echo "== $(date) =="
docker exec -i -e SOAK_HOST="$host" gatekeeper python - < "$here/scripts/soak/daily_report.py"
