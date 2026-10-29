"""The Visits page (web.py): the last few days at the door, and what each visit really was.

Each visit shows what Gatekeeper did, in words, what it heard, and the camera's picture.
The owner can say what it was (Amazon, UPS, a pizza delivery, groceries, the household,
a guest, ...). Those labels are kept in ``visit-labels.json`` next to the visit log. They
show where Gatekeeper got it wrong (the daily report compares them with what it
decided), and they're the examples for teaching it to tell visitors apart by sight.
"""

from __future__ import annotations

import datetime
import glob
import html
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

# What a visit can be, as the owner sees it: (code, what the page says).
LABELS = [
    ("amazon", "Amazon delivery"),
    ("ups", "UPS"),
    ("fedex", "FedEx"),
    ("usps", "USPS (mail carrier)"),
    ("dhl", "DHL"),
    ("courier", "Another parcel courier"),
    ("food", "Food delivery (pizza, DoorDash, Uber Eats...)"),
    ("groceries", "Groceries (Instacart, Walmart...)"),
    ("household", "Household"),
    ("guest", "Guest (family, friends)"),
    ("neighbor", "Neighbor"),
    ("service", "Service or repair"),
    ("solicitor", "Solicitor or canvasser"),
    ("official", "Police or officials"),
    ("other", "Someone else"),
    ("nobody", "Nobody there (a false alarm)"),
]
LABEL_NAMES = dict(LABELS)
EVENT_ID = re.compile(r"^[\w.:-]{1,80}$")
DAYS = 3  # shown by default; ?days= up to MAX_DAYS
MAX_DAYS = 30

# What Gatekeeper did, by outcome (visits that ran are described by what was said).
OUTCOMES = {
    "left": "Taken for the household coming out (not greeted)",
    "stepped_out": "Taken for the household stepping out (not greeted)",
    "returned": "Taken for the household coming back (not greeted)",
    "entered": "Greeted, then went in by the front door: taken for the household",
    "delivery_trip": "Another trip of a delivery (not greeted)",
    "pressed_during_visit": "Doorbell pressed during the visit (part of it, no label needed)",
    "package_delivered": "Package left at the door",
    "package_picked_up": "Package picked up",
    "talkback_failed": "Couldn't speak at the door (the speaker didn't connect)",
    "talkback_unavailable": "Couldn't speak at the door (the speaker didn't connect)",
    "talkback_busy": "Couldn't speak at the door (the speaker was busy)",
    "reply_failed": "Greeted, but the reply failed",
    "timeout": "The visit got stuck and was stopped",
    "error": "Something went wrong during the visit",
}
CLASSIFICATIONS = {
    "no_response": "no answer",
    "likely_delivery": "said it's a delivery",
    "solicitor": "a solicitor",
    "service_visit": "a service visit",
    "cooperative_other": "said why they came",
}
FLOWS = {
    "law_enforcement": "Talked with an officer",
    "emergency": "An emergency: told them the resident was alerted",
    "civic": "Took a message (a canvasser or official)",
}


class Labels:
    """The owner's labels, by visit (event id): ``{"label": code, "at": time}``."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("labels", {})
        except (FileNotFoundError, ValueError):
            return {}

    def set(self, event_id: str, label: str) -> None:
        """Label a visit; an empty label clears it."""
        if not EVENT_ID.match(event_id):
            raise ValueError("That isn't a visit.")
        if label and label not in LABEL_NAMES:
            raise ValueError("Pick what the visit was from the list.")
        with self._lock:
            labels = self.all()
            if label:
                labels[event_id] = {"label": label, "at": round(self._clock(), 3)}
            else:
                labels.pop(event_id, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"labels": labels}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


def read_visits(event_log: Path, since: float) -> list[dict]:
    """Visit records from the visit log and its daily files, from ``since`` on."""
    rows = []
    for name in sorted(glob.glob(str(event_log.with_name(f"{event_log.stem}*{event_log.suffix}")))):
        try:
            with open(name, encoding="utf-8") as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("ts", 0) >= since:
                        rows.append(record)
        except OSError:
            continue
    return rows


def started(record: dict) -> float:
    """When the visit started: its event id is the time the person was first seen."""
    eid = str(record.get("event_id", ""))
    try:
        return float(
            eid.removeprefix("press-").removeprefix("package-").removeprefix("knock-").split("-")[0]
        )
    except ValueError:
        return float(record.get("ts", 0))


def labelable(record: dict) -> bool:
    """A visit by someone (a person seen, or a doorbell press), not a package or a reply,
    nor a press during a visit (it's part of that visit)."""
    outcome = str(record.get("outcome", ""))
    return (
        record.get("trigger", "person") in ("person", "button")
        and not outcome.startswith("package_")
        and outcome != "pressed_during_visit"
    )


def describe(record: dict) -> str:
    """What Gatekeeper did, in words."""
    outcome = record.get("outcome", "")
    details = record.get("details") or {}
    pressed = record.get("trigger") == "button"
    if outcome == "household":
        how = "stopped the greeting" if details.get("greeted") else "not greeted"
        return f"Recognized as {details.get('name') or 'someone enrolled'} ({how})"
    if outcome == "knock":
        where = "" if details.get("on_camera") else ", nobody on camera"
        during = ", during a visit" if details.get("in_visit") else ""
        return f"A knock at the door ({details.get('score', 0):.2f}){where}{during}"
    if outcome == "quick_reply":
        return f"You replied: {details.get('reply', '?')} ({details.get('result', '?')})"
    if outcome in OUTCOMES:
        return OUTCOMES[outcome]
    if record.get("flow") in FLOWS:
        return FLOWS[record["flow"]]
    said = CLASSIFICATIONS.get(record.get("classification") or "", "")
    what = "Doorbell pressed: greeted" if pressed else "Greeted"
    if details.get("walked_away"):
        return f"{what}, they walked away: a send-off"
    return f"{what}, {said}" if said else what


class VisitsPage:
    """What the Visits page shows and does, apart from HTTP (testable on its own).
    ``snapshot(event_id)`` is the camera's picture of a person event, or None."""

    def __init__(
        self,
        event_log: Path,
        labels: Labels,
        *,
        snapshot: Callable[[str], bytes | None],
        snapshot_dir: Path,
        guesses=None,
        knocks=None,
        clips=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.event_log = event_log
        self.labels = labels
        self._snapshot = snapshot
        self.snapshot_dir = snapshot_dir
        self.guesses = guesses  # Gatekeeper's guesses (visitor_type.Guesses), if it makes them
        self.knocks = knocks  # the knocks heard (knock.Knocks), if it listens
        self.clips = clips  # Gatekeeper's own recordings (recording.Clips), if it records
        self._clock = clock
        self.token = ""  # set by web.serve (shared with the faces page)

    def visits(self, days: int = DAYS) -> list[dict]:
        """The visits of the last ``days`` days, newest first, each with its label."""
        since = self._clock() - days * 86400
        labels = self.labels.all()
        rows = [r for r in read_visits(self.event_log, since) if r.get("outcome") != "quick_reply"]
        for k in self.knocks.since(since) if self.knocks else []:
            rows.append(
                {
                    "event_id": f"knock-{k['at']:.3f}",
                    "ts": k["at"],
                    "trigger": "knock",
                    "outcome": "knock",
                    "details": k,
                }
            )
        for r in rows:
            r["label"] = (labels.get(str(r.get("event_id"))) or {}).get("label", "")
        return sorted(rows, key=started, reverse=True)

    def save(self, chosen: dict[str, str]) -> str:
        """Save the page's labels at once (``{event id: label}``, "" for none): only those
        that changed."""
        current = {eid: x.get("label", "") for eid, x in self.labels.all().items()}
        changed = {eid: label for eid, label in chosen.items() if current.get(eid, "") != label}
        for eid, label in changed.items():
            self.labels.set(eid, label)
        if not changed:
            return "Nothing changed."
        return f"Saved {len(changed)} label(s)."

    def clips_of(self, record: dict) -> list[tuple[str, float]]:
        """A visit's clips: (their path on the page, seconds), oldest first."""
        if self.clips is None or str(record.get("event_id", "")).startswith("knock-"):
            return []
        start, end = started(record), float(record.get("ts") or started(record))
        return [
            (f"clip/{folder.name}/{clip['file']}", clip["end"] - clip["start"])
            for folder, clip in self.clips.overlapping(start - 5, end + 5)
        ]

    def clip_file(self, day: str, name: str) -> Path | None:
        return self.clips.file(day, name) if self.clips is not None else None

    def image(self, event_id: str) -> bytes | None:
        """The picture for a visit: the photo saved with a package record, else the
        camera's snapshot of the person event."""
        if not EVENT_ID.match(event_id):
            return None
        if event_id.startswith("press-"):
            path = self._press_photo(event_id)
            return path.read_bytes() if path.is_file() else None
        if event_id.startswith("package-"):
            for kind in ("delivered", "picked_up"):
                path = self.snapshot_dir / f"{event_id}-{kind}.jpg"
                if path.is_file():
                    return path.read_bytes()
            return None
        return self._snapshot(event_id)

    def _press_photo(self, event_id: str) -> Path:
        """The camera's frame sent with a doorbell press's notification (app.py keeps it):
        a press before anyone was seen has no snapshot of its own."""
        return self.snapshot_dir / f"{event_id}.jpg"

    # -- the page ----------------------------------------------------------------

    def render(self, message: str = "", error: str = "", days: int = DAYS) -> str:
        from gatekeeper.web import STYLE, nav

        e = html.escape
        rows = self.visits(days)
        labelled = sum(1 for r in rows if r["label"])
        guesses = self.guesses.all() if self.guesses else {}
        out = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Visits</title><style>{STYLE}</style></head><body>",
            nav("visits"),
            "<h1>Visits</h1>",
        ]
        if message:
            out.append(f"<p class=done role=status>{e(message)}</p>")
        if error:
            out.append(f"<p class=error role=alert>{e(error)}</p>")
        out.append(
            f"<p class=note>The last {days} day(s) at the door, newest first. Say what each "
            "visit really was: it shows where Gatekeeper got it wrong, and teaches it to tell "
            f"visitors apart. {labelled} of {len(rows)} labelled. "
            f'Show <a href="./?days={min(days * 2, MAX_DAYS)}">more days</a>.</p>'
        )
        if not rows:
            out.append("<p class=note>Nobody yet.</p>")
        # One form for the whole page: change any visits, then save them together.
        out.append(
            '<form method=post action="labels">'
            f'<input type=hidden name=token value="{self.token}">'
            f'<input type=hidden name=days value="{days}">'
        )
        day = None
        for r in rows:
            at = started(r)
            if _day(at) != day:
                day = _day(at)
                out.append(f"<h2>{e(day)}</h2>")
            eid = str(r.get("event_id", ""))
            out.append("<div class=visit>")
            if eid.startswith("knock-"):
                pass  # heard, not seen
            elif not eid.startswith("press-") or self._press_photo(eid).is_file():
                src = f"visit/{urllib.parse.quote(eid)}.jpg"
                out.append(f'<img src="{src}" alt="" loading=lazy>')
            out.append(f"<div><strong>{e(_time(at))}</strong> {e(describe(r))}")
            if guesses and (g := guesses.get(eid)):
                out.append(f"<br><span class=note>{e(guess_text(g))}</span>")
            if r.get("transcript"):
                out.append(f"<br><span class=note>Heard: &ldquo;{e(r['transcript'])}&rdquo;</span>")
            clips = self.clips_of(r)
            if clips:
                links = ", ".join(
                    f'<a href="{e(href)}">{_length(seconds)}</a>' for href, seconds in clips
                )
                out.append(f"<br>Clip{'s' if len(clips) > 1 else ''}: {links}")
            if labelable(r):
                options = ['<option value="">What was it?</option>'] + [
                    f'<option value="{code}"{" selected" if r["label"] == code else ""}>'
                    f"{e(name)}</option>"
                    for code, name in LABELS
                ]
                out.append(
                    f'<div><select name="label:{e(eid)}" aria-label="What was it?">'
                    f"{''.join(options)}</select></div>"
                )
            out.append("</div></div>")
        if rows:
            out.append(
                "<div class=savebar><button type=submit>Save labels</button> "
                "<span class=note>Saves every visit you changed.</span></div>"
            )
        out.append("</form></body></html>")
        return "".join(out)


def guess_text(guess: dict) -> str:
    """Gatekeeper's guess at a visitor, in words, with why."""
    f = guess.get("features") or {}
    why = [
        f"looks {guess['looks']:.2f}" if guess.get("looks") is not None else "no good picture",
        *(["left the way they came"] if f.get("left_the_way_they_came") else []),
        *(["walked away when greeted"] if f.get("walked_away") else []),
        *(["a package was left"] if f.get("package_left") else []),
    ]
    what = "a delivery" if guess.get("guess") == "delivery" else "not a delivery"
    return f"Gatekeeper's guess: {what} ({', '.join(why)})"


def _length(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%A %d %B")


def _time(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _when(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%a %H:%M")
