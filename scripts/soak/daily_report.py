"""Daily soak report against the Phase 0 exit criteria, sent via ntfy.

Runs inside the gatekeeper container (it has the visit logs, Frigate's API and the ntfy
settings); daily-report.sh passes in what only the host can see as JSON in SOAK_HOST.

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


# Visits Gatekeeper logged.
visits = []
for path in sorted(glob.glob(str(s.log_dir / "events*.jsonl"))):
    with open(path, encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            if record.get("ts", 0) >= since:
                visits.append(record)
outcomes = collections.Counter(v.get("outcome") for v in visits)
triggers = collections.Counter(v.get("trigger", "person") for v in visits)
labels = collections.Counter(v.get("classification") or "-" for v in visits)
presses = [v for v in visits if v.get("trigger") == "button"]

# Frigate person events in the porch zone that Gatekeeper neither visited nor merged.
handled = {v["event_id"] for v in visits} | set(host.get("merged", []))
query = f"cameras={s.camera_name}&labels=person&after={since:.0f}&limit=500"
zones = s.trigger_zone_list
if zones:
    query += "&zones=" + ",".join(zones)
with urllib.request.urlopen(f"{s.frigate_api}/api/events?{query}", timeout=10) as resp:
    porch_events = json.load(resp)
missed = [e for e in porch_events if e["id"] not in handled]

notified = host.get("notified", 0)
problems = []
if missed:
    problems.append(f"{len(missed)} porch arrival(s) not greeted")
if host.get("notify_failed", 0):
    problems.append(f"{host['notify_failed']} notification(s) failed")
if notified < len(visits):
    problems.append(f"{len(visits) - notified} visit(s) without a notification")
hangs = outcomes.get("timeout", 0) + host.get("hang_exits", 0) + host.get("restarts_today", 0)
if hangs:
    problems.append(f"{hangs} hang/restart(s)")
if outcomes.get("error", 0):
    problems.append(f"{outcomes['error']} visit error(s)")
silent_hours = host.get("silent_audio_hours", [])
if silent_hours:
    problems.append(f"recording audio missing in {len(silent_hours)} hour(s)")

no_answer = [v for v in visits if v.get("classification") == "no_response"
             and v.get("trigger") != "button"]
lines = [
    f"Last {HOURS} h, to {local(now)}",
    f"Visits: {len(visits)} (person {triggers.get('person', 0)}, button "
    f"{triggers.get('button', 0)})",
    "  " + ", ".join(f"{k} {n}" for k, n in labels.most_common()) if visits else "  none",
    "Outcomes: " + (", ".join(f"{k} {n}" for k, n in outcomes.most_common()) or "-"),
    f"Doorbell presses: {len(presses)}",
    f"Notifications: {notified} sent, {host.get('notify_failed', 0)} failed",
    f"Porch arrivals not greeted: {len(missed)}"
    + (" (" + ", ".join(local(e["start_time"]) for e in missed[:5]) + ")" if missed else ""),
    f"Hangs: {hangs} (timeouts {outcomes.get('timeout', 0)}, watchdog exits "
    f"{host.get('hang_exits', 0)}, restarts {host.get('restarts_today', 0)})",
    f"No-answer person visits: {len(no_answer)}"
    + (" - real visitors? " + ", ".join(local(v["ts"]) for v in no_answer[:6]) if no_answer
       else ""),
    "Recording audio: " + ("OK" if not silent_hours else "missing at " + ", ".join(silent_hours)),
    f"Gatekeeper: {host.get('health', '?')}, up since {host.get('started', '?')}",
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
