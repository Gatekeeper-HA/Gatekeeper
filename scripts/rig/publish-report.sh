#!/bin/bash
# Publish a daily report (its JSON and Markdown) to a private GitHub repository,
# RIG_REPORTS_REPO (an SSH URL), with a deploy key that may write to it:
# RIG_REPORTS_KEY, ~/.ssh/gatekeeper-reports by default. daily-report.sh calls it.
#
# The repository must be private: reports hold visit times and what visitors said.
#
#   scripts/rig/publish-report.sh ~/.local/state/gatekeeper/reports/2026-10-14.{json,md}
set -euo pipefail
. "$(dirname "$0")/lib.sh"
json=$(realpath "$1") md=$(realpath "$2")
: "${RIG_REPORTS_REPO:?set RIG_REPORTS_REPO in .env}"
key="${RIG_REPORTS_KEY:-$HOME/.ssh/gatekeeper-reports}"
export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes"
repo="$state/reports-repo"

[ -d "$repo/.git" ] || git clone -q "$RIG_REPORTS_REPO" "$repo"
git -C "$repo" pull -q --rebase
day=$(basename "$json" .json)
mkdir -p "$repo/reports"
cp "$json" "$repo/reports/$day.json"
cp "$md" "$repo/reports/$day.md"
git -C "$repo" add reports
if git -C "$repo" diff --cached --quiet; then
  echo "report $day unchanged"
  exit 0
fi
git -C "$repo" -c user.name="Gatekeeper rig" -c user.email="rig@gatekeeper.invalid" \
  commit -qm "Daily report $day"
if ! git -C "$repo" push -q origin HEAD; then
  notify "Daily report not published" "Pushing the $day report to the reports repository failed." 3 warning
  exit 1
fi
echo "published report $day"
