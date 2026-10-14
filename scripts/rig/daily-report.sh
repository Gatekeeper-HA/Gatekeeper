#!/bin/bash
# Daily report: the last 24 h at the door, checked against what Gatekeeper should do
# (src/gatekeeper/report.py, run inside the gatekeeper container so it matches the deployed
# code). Sends the summary via ntfy, keeps the JSON and Markdown in
# ~/.local/state/gatekeeper/reports/, and publishes them when RIG_REPORTS_REPO is set
# (publish-report.sh). Run daily from cron:
#
#   0 1 * * * /docker/stack/scripts/rig/daily-report.sh >> ~/.local/state/gatekeeper/rig.log 2>&1
set -euo pipefail
. "$(dirname "$0")/lib.sh"
exec 9>"$state/deploy.lock"
flock -w 1800 9 || true  # let a running deploy finish first
recordings="${DATA_DIR:-./data}/frigate/storage/recordings"
mkdir -p "$state/reports"
out="$state/reports/$(date +%F)"  # local date: .env sets TZ
echo "== $(date -Is) daily report =="

if [ "$(docker inspect -f '{{.State.Running}}' gatekeeper 2>/dev/null)" != true ]; then
  notify "Gatekeeper daily report: Gatekeeper isn't running" \
    "No report: the gatekeeper container isn't running on the rig." 4 warning
  jq -n --arg t "$(date -Is)" '{schema: 2, generated: $t, error: "gatekeeper not running"}' \
    > "$out.json"
  printf '# Gatekeeper daily report: Gatekeeper isn'"'"'t running\n' > "$out.md"
  [ -z "${RIG_REPORTS_REPO:-}" ] || "$stack/scripts/rig/publish-report.sh" "$out.json" "$out.md"
  exit 1
fi

# Watchdog exits survive container restarts (same container, same log).
hang_exits=$(docker logs --since 24h gatekeeper 2>&1 | grep -c "internal hang" || true)
started=$(docker inspect -f '{{.State.StartedAt}}' gatekeeper | cut -c1-16)
health=$(docker inspect -f '{{.State.Health.Status}}' gatekeeper)

# Restarts since the last report: Docker's RestartCount is per container (a deploy
# recreates it and starts again from 0), so compare with what the last run saw.
count=$(docker inspect -f '{{.RestartCount}}' gatekeeper)
created=$(docker inspect -f '{{.Created}}' gatekeeper)
statef="$state/restarts"
prev_created=""; prev_count=0
[ -f "$statef" ] && read -r prev_created prev_count < "$statef"
if [ "$prev_created" = "$created" ]; then restarts=$((count - prev_count)); else restarts=$count; fi
echo "$created $count" > "$statef"

# Recording audio: the first segment of each hour in the last 24 h (hours with no
# motion may have no segments, which is fine).
image=$(gatekeeper_image)
silent='[]'
for h in $(seq 0 23); do
  dir="$recordings/$(date -u -d "-$h hour" +%Y-%m-%d/%H)/front_door"
  first=$(ls "$dir" 2>/dev/null | head -1 || true)
  [ -z "$first" ] && continue
  packets=$(docker run --rm -v "$(realpath "$dir")":/r:ro "$image" \
    ffprobe -v error -select_streams a:0 -count_packets -show_entries stream=nb_read_packets \
    -of csv=p=0 "/r/$first" 2>/dev/null || echo 0)
  if [ "${packets:-0}" -lt 50 ]; then
    silent=$(jq -c --arg t "$(date -d "-$h hour" '+%a %H:00')" '. + [$t]' <<<"$silent")
  fi
done

# Deploys in the last 24 h (deploy.sh).
since=$(( $(date +%s) - 86400 ))
deploys=$(jq -sc --argjson since "$since" '[.[] | select(.ts >= $since)]' \
  "$state/deploys.jsonl" 2>/dev/null || echo '[]')

host=$(jq -nc --argjson hang_exits "$hang_exits" --argjson restarts "$restarts" \
  --argjson silent "$silent" --arg started "$started" --arg health "$health" \
  --arg commit "$(git rev-parse HEAD)" --arg subject "$(git log -1 --format=%s)" \
  --arg image "$image" --argjson deploys "$deploys" \
  '{hang_exits: $hang_exits, restarts: $restarts, silent_audio_hours: $silent,
    started: $started, health: $health, commit: $commit, subject: $subject, image: $image,
    deploys: $deploys}')

if {
  echo "### whisper"
  docker logs gatekeeper 2>&1 | grep -E "loading Whisper model|couldn't run on the GPU" || true
  echo "### gatekeeper"
  docker logs --since 24h gatekeeper 2>&1
  echo "### frigate"
  docker logs --since 24h frigate 2>&1
} | docker exec -i -e REPORT_HOST="$host" gatekeeper python -m gatekeeper.report --logs - \
    --notify > "$out.json" 2> "$out.err"; then
  docker exec -i gatekeeper python -m gatekeeper.report --render markdown < "$out.json" > "$out.md"
  docker exec -i gatekeeper python -m gatekeeper.report --render text < "$out.json"
else
  # The report itself broke: say so, and publish the error for the reviewer.
  cat "$out.err"
  notify "Gatekeeper daily report failed" "$(tail -n 5 "$out.err")" 4 warning
  jq -n --arg t "$(date -Is)" --arg err "$(tail -n 40 "$out.err")" --argjson host "$host" \
    '{schema: 2, generated: $t, error: "the report failed", traceback: $err, host: $host}' \
    > "$out.json"
  { echo "# Gatekeeper daily report failed"; echo; echo '```'; tail -n 40 "$out.err"; echo '```'; } \
    > "$out.md"
fi

[ -z "${RIG_REPORTS_REPO:-}" ] || "$stack/scripts/rig/publish-report.sh" "$out.json" "$out.md"
