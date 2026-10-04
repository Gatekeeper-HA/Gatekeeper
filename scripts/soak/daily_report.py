"""Daily soak report against the Phase 0 exit criteria, sent via ntfy.

Runs inside the gatekeeper container, which has the visit and notification logs,
Frigate's API and the ntfy settings. daily-report.sh passes in what only the host can see
(restarts, watchdog exits, recording audio) as JSON in SOAK_HOST.

Exit criteria (docs/05-roadmap.md): every porch visit and doorbell press notified,
0 hangs, fewer than 2 false triggers a day. Plus: Frigate's recordings keep their audio.
"""

import collections
import datetime
import glob
import json
import os
import time
import urllib.request

from gatekeeper.app import build_notify_backends
from gatekeeper.config import Settings
from gatekeeper.notify import Notification

HOURS = 24
s = Settings()
host = json.loads(os.environ.get("SOAK_HOST", "{}"))
now = time.time()
since = now - HOURS * 3600


def local(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")


def read_log(path, after):
    rows = []
    for name in sorted(glob.glob(str(path.with_name(f"{path.stem}*{path.suffix}")))):
        with open(name, encoding="utf-8") as f:
            rows += [r for r in map(json.loads, f) if r.get("ts", 0) >= after]
    return rows


def started(visit) -> float:
    """Visit start, from the Frigate event id (or press-<ts>)."""
    return float(visit["event_id"].removeprefix("press-").split("-")[0])


visits = read_log(s.event_log_file, since)
notifications = read_log(s.notification_log_file, since - 600)
outcomes = collections.Counter(v.get("outcome") for v in visits)
triggers = collections.Counter(v.get("trigger", "person") for v in visits)
labels = collections.Counter(v.get("classification") or "pressed" for v in visits)

# Every visit notified? (Notification logging started with 0.2.0-rc.1.)
tracked_since = min((n["ts"] for n in read_log(s.notification_log_file, 0)), default=now)
ok = {n["event_id"]: n for n in notifications if n["ok"]}
tracked = [v for v in visits if v["ts"] >= tracked_since]
unnotified = [v for v in tracked if v["event_id"] not in ok]
failed = [n for n in notifications if not n["ok"]]
slowest = max((n["seconds"] for n in ok.values()), default=0.0)

# Every porch arrival handled? A Frigate porch event is covered if it started during a
# visit or its cooldown (merged into it).
query = f"cameras={s.camera_name}&labels=person&after={since:.0f}&limit=500"
if s.trigger_zone_list:
    query += "&zones=" + ",".join(s.trigger_zone_list)
with urllib.request.urlopen(f"{s.frigate_api}/api/events?{query}", timeout=10) as resp:
    arrivals = json.load(resp)
windows = [
    (started(v) - 5, v["ts"] + s.cooldown_seconds)
    for v in read_log(s.event_log_file, since - 3600)
]
missed = [e for e in arrivals if not any(a <= e["start_time"] <= b for a, b in windows)]

hangs = outcomes.get("timeout", 0) + host.get("hang_exits", 0) + host.get("restarts", 0)
silent_hours = host.get("silent_audio_hours", [])
problems = []
if missed:
    problems.append(f"{len(missed)} porch arrival(s) not greeted")
if unnotified:
    problems.append(f"{len(unnotified)} visit(s) not notified")
if slowest > 3:
    problems.append(f"slowest notification {slowest:.1f} s")
if hangs:
    problems.append(f"{hangs} hang/restart(s)")
if outcomes.get("error", 0):
    problems.append(f"{outcomes['error']} visit error(s)")
if silent_hours:
    problems.append(f"no recording audio in {len(silent_hours)} hour(s)")

no_answer = [
    v for v in visits
    if v.get("classification") == "no_response" and v.get("trigger") != "button"
]
by_label = ", ".join(f"{k} {n}" for k, n in labels.most_common()) or "none"
lines = [
    f"Last {HOURS} h, to {local(now)}",
    f"Visits: {len(visits)} (person {triggers.get('person', 0)}, "
    f"button {triggers.get('button', 0)}): {by_label}",
    "Outcomes: " + (", ".join(f"{k} {n}" for k, n in outcomes.most_common()) or "-"),
    f"Porch arrivals: {len(arrivals)}, not greeted {len(missed)}"
    + (" (" + ", ".join(local(e["start_time"]) for e in missed[:5]) + ")" if missed else ""),
    f"Notified: {len(tracked) - len(unnotified)}/{len(tracked)} visits, "
    f"slowest {slowest:.2f} s, {len(failed)} failed attempt(s)"
    + (f" (tracked since {local(tracked_since)})" if tracked_since > since else ""),
    f"Hangs: {hangs} (timeouts {outcomes.get('timeout', 0)}, watchdog exits "
    f"{host.get('hang_exits', 0)}, restarts {host.get('restarts', 0)})",
    f"No-answer person visits: {len(no_answer)}"
    + (" - all real people? " + ", ".join(local(v["ts"]) for v in no_answer[-6:])
       if no_answer else ""),
    "Recording audio: " + ("OK" if not silent_hours else "missing at " + ", ".join(silent_hours)),
    f"Gatekeeper: {host.get('health', '?')}, up since {host.get('started', '?')} UTC",
]
title = "Gatekeeper daily report: " + ("all clear" if not problems else "; ".join(problems))
notification = Notification(
    title=title,
    message="\n".join(lines),
    tags=["white_check_mark"] if not problems else ["warning"],
    priority=2 if not problems else 4,
)
print(title)
print(notification.message)
for backend in build_notify_backends(s):
    backend.send(notification, None)
