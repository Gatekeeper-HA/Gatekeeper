"""The Visits page (web.py): the last few days at the door, and what each visit really was.

Each visit shows what Gatekeeper did, in words, what it heard, and the camera's picture.
The owner can say what it was (Amazon, UPS, a pizza delivery, groceries, the household,
a guest, ...), and more about it if they like (what they were doing, what they carried and
wore). Those labels are kept in ``visit-labels.json`` next to the visit log. They
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
# More about a visit, all optional (a label's "More about it"): what they were doing, how
# many came, what they carried and wore, and a note. The household's comings and goings
# are what a delivery looks like to the camera (bags, a backpack, a lunch bag), and what
# they carried and wore is what the conversation model's camera line says: these check it.
DOING = [
    ("coming_home", "Coming home (from work, errands)"),
    ("leaving", "Leaving"),
    ("dog", "Taking the dog out or in"),
    ("mail", "Getting a package or the mail"),
    ("trash", "Taking out the trash or recycling"),
    ("outside", "Out to the yard or the car for a moment"),
    ("visiting", "Visiting or dropping by"),
    ("other", "Something else"),
]
PEOPLE = [("1", "1"), ("2", "2"), ("3+", "3 or more")]
CARRYING = [
    ("nothing", "Nothing"),
    ("box", "A box or package"),
    ("shopping_bags", "Grocery or shopping bags"),
    ("food", "Food (a pizza box, takeout)"),
    ("backpack", "A backpack"),
    ("purse", "A purse or handbag"),
    ("lunch_bag", "A lunch bag or cooler"),
    ("drink", "A drink or water bottle"),
    ("dog", "A dog on a leash"),
    ("other", "Something else"),
]
WEARING = [
    ("uniform", "A uniform or work clothes"),
    ("vest", "A safety vest"),
    ("hood", "A hood or hat"),
    ("face_covered", "A covered face (a mask, a scarf)"),
]
DETAILS = ("doing", "people", "carrying", "wearing", "note")  # in a label, after "label"
NOTE_LENGTH = 200
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
    "owner_took_over": "You talked to them from the Live page (Gatekeeper stepped out)",
    "owner_talking": "Not greeted: you were talking at the door",
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
    "trick_or_treat": "trick-or-treaters",
}
FLOWS = {
    "law_enforcement": "Talked with an officer",
    "emergency": "An emergency: told them the resident was alerted",
    "civic": "Took a message (a canvasser or official)",
}


def checked_details(details: dict) -> dict:
    """The parts of a label's "More about it" that say something, checked: ``doing`` and
    ``people`` (one of DOING, PEOPLE), ``carrying`` and ``wearing`` (some of CARRYING,
    WEARING, in their order) and ``note`` (a line of text)."""
    out: dict = {}
    for key, choices in (("doing", DOING), ("people", PEOPLE)):
        if value := details.get(key):
            if value not in dict(choices):
                raise ValueError("Pick from the lists.")
            out[key] = value
    for key, choices in (("carrying", CARRYING), ("wearing", WEARING)):
        values = {v for v in details.get(key) or [] if v}
        if not values <= set(dict(choices)):
            raise ValueError("Pick from the lists.")
        if values:
            out[key] = [code for code, _ in choices if code in values]
    if note := " ".join(str(details.get("note") or "").split())[:NOTE_LENGTH]:
        out["note"] = note
    return out


def label_details(label: dict) -> dict:
    """A kept label's "More about it" (``checked_details``)."""
    return {k: label[k] for k in DETAILS if k in label}


def form_details(form: dict[str, list[str]], event_id: str) -> dict:
    """A visit's "More about it" from the page's form (``parse_qs``: each field a list)."""

    def one(key: str) -> str:
        return (form.get(f"{key}:{event_id}") or [""])[0]

    return {
        "doing": one("doing"),
        "people": one("people"),
        "carrying": form.get(f"carrying:{event_id}", []),
        "wearing": form.get(f"wearing:{event_id}", []),
        "note": one("note"),
    }


def details_form(event_id: str, details: dict) -> str:
    """A visit's "More about it" (``checked_details``), folded away unless filled in."""
    e = html.escape
    eid = e(event_id)

    def select(key: str, choices: list, prompt: str) -> str:
        options = [f'<option value="">{e(prompt)}</option>'] + [
            f'<option value="{code}"{" selected" if details.get(key) == code else ""}>'
            f"{e(name)}</option>"
            for code, name in choices
        ]
        return f'<select name="{key}:{eid}" aria-label="{e(prompt)}">{"".join(options)}</select>'

    def boxes(key: str, choices: list, legend: str) -> str:
        ticked = set(details.get(key) or [])
        return (
            f"<fieldset><legend>{legend}</legend>"
            + "".join(
                f'<label><input type=checkbox name="{key}:{eid}" value="{code}"'
                f"{' checked' if code in ticked else ''}> {e(name)}</label>"
                for code, name in choices
            )
            + "</fieldset>"
        )

    return (
        f"<details class=more{' open' if details else ''}><summary>More about it</summary>"
        + select("doing", DOING, "Doing what?")
        + select("people", PEOPLE, "How many people?")
        + boxes("carrying", CARRYING, "Carrying")
        + boxes("wearing", WEARING, "Wearing")
        + f'<label>Note <input type=text name="note:{eid}" maxlength={NOTE_LENGTH} '
        f'value="{e(details.get("note", ""))}"></label></details>'
    )


class Labels:
    """The owner's labels, by visit (event id): ``{"label": code, "at": time}``, with
    more about the visit (``checked_details``) when the owner said it."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("labels", {})
        except (FileNotFoundError, ValueError):
            return {}

    def set(self, event_id: str, label: str, details: dict | None = None) -> None:
        """Label a visit, with more about it (``checked_details``) if given; an empty label
        clears it all."""
        if not EVENT_ID.match(event_id):
            raise ValueError("That isn't a visit.")
        if label and label not in LABEL_NAMES:
            raise ValueError("Pick what the visit was from the list.")
        more = checked_details(details or {})
        with self._lock:
            labels = self.all()
            if label:
                labels[event_id] = {"label": label, **more, "at": round(self._clock(), 3)}
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
        record.get("trigger", "person") in ("person", "button", "knock")
        and not outcome.startswith("package_")
        and outcome not in ("pressed_during_visit", "knock")  # a knock heard: not a visit
    )


def describe(record: dict) -> str:
    """What Gatekeeper did, in words."""
    outcome = record.get("outcome", "")
    details = record.get("details") or {}
    pressed = record.get("trigger") == "button"
    knocked = record.get("trigger") == "knock"
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
    what = "Doorbell pressed: greeted" if pressed else "Knocked: greeted" if knocked else "Greeted"
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
        answers=None,
        knocks=None,
        clips=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.event_log = event_log
        self.labels = labels
        self._snapshot = snapshot
        self.snapshot_dir = snapshot_dir
        self.guesses = guesses  # Gatekeeper's guesses (visitor_type.Guesses), if it makes them
        # The conversation model's answers (conversation_model.Answers), if it's in shadow.
        self.answers = answers
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
            label = labels.get(str(r.get("event_id"))) or {}
            r["label"] = label.get("label", "")
            r["label_details"] = label_details(label)  # not "details": the visit's own
        return sorted(rows, key=started, reverse=True)

    def save(self, chosen: dict[str, str], details: dict[str, dict] | None = None) -> str:
        """Save the page's labels at once (``{event id: label}``, "" for none, and more
        about each, ``{event id: details}``): only those that changed."""
        current = self.labels.all()
        changed = 0
        for eid, label in chosen.items():
            more = checked_details((details or {}).get(eid) or {}) if label else {}
            kept = current.get(eid) or {}
            if kept.get("label", "") != label or label_details(kept) != more:
                self.labels.set(eid, label, more)
                changed += 1
        if not changed:
            return "Nothing changed."
        return f"Saved {changed} label(s)."

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
        answers = self.answers.all() if self.answers else {}
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
            if answers and (a := answers.get(eid)):
                out.append(f"<br><span class=note>{e(answer_text(a))}</span>")
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
                out.append(details_form(eid, r["label_details"]))
            out.append("</div></div>")
        if rows:
            out.append(
                "<div class=savebar><button type=submit>Save labels</button> "
                "<span class=note>Saves every visit you changed.</span></div>"
            )
        out.append("</form></body></html>")
        return "".join(out)


def answer_text(answer: dict) -> str:
    """What the conversation model would have said (in shadow), turn by turn, in words, and
    what it saw in the visit's picture (with its vision file)."""
    if answer.get("error"):
        return "Conversation model: couldn't be asked."
    said = []
    for turn in answer.get("turns") or []:
        if "kind" not in turn:
            said.append("(no usable answer)")
            continue
        broke = sorted({b.split(":")[0] for b in turn.get("broken") or []})
        said.append(
            f"\u201c{turn['say']}\u201d ({turn['kind'].replace('_', ' ')}, "
            f"{turn['action'].replace('_', ' ')}"
            f"{', breaks a rule: ' + ', '.join(broke) if broke else ''})"
        )
    text = "Conversation model would have said: " + " Then: ".join(said or ["nothing"])
    if answer.get("camera"):
        text += f". It saw: {answer['camera'].removeprefix('Camera: ')}"
    elif "camera" in answer or answer.get("camera_error"):
        text += ". It couldn't describe the picture."
    return text


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
