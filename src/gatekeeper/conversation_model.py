"""The conversation model, in shadow (CONVERSATION_MODEL=shadow). After each visit, a small
model trained on made-up visits (docs/09) is asked what it would have said at each turn,
told what Gatekeeper knew then. Its answers are kept next to the visit log
(conversation-model.json), shown on the Visits page and counted in the daily report.
Nothing at the door depends on them yet.

The model runs in llama.cpp's server (the conversation-model service in
docker-compose.yml), in the house: visitors' words never leave it. Each answer is checked
against the house rules (house_rules.py), as a live model's would be before it's said.

What it's told, in the form it was trained on:

    It's Tuesday, March 4, 2:17 PM.
    Camera: a person at the door (the camera gives no other details).
    They pressed the doorbell.
    Earlier: (Gatekeeper) "Hi there! What brings you by?" (Visitor) "..."
    They said: "I've got a package for you."

Gatekeeper doesn't describe the visitor yet, so the camera line says so; the model was
trained on visits like that too, to judge by the words alone.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import re
import threading
import time
import urllib.request
from collections.abc import Callable
from importlib import resources
from pathlib import Path

from gatekeeper import house_rules
from gatekeeper.visits import started

log = logging.getLogger(__name__)

# The model's instructions, exactly as it was trained with them.
PROMPT = resources.files("gatekeeper").joinpath("conversation_prompt.txt").read_text("utf-8")
KINDS = (
    "delivery",
    "solicitor",
    "civic",
    "service",
    "law_enforcement",
    "emergency",
    "guest",
    "trick_or_treat",
    "unclear",
    "nothing",
)
SIGNALS = (
    "asks_occupancy",
    "asks_entry",
    "asks_schedule",
    "evasive",
    "story_mismatch",
    "claim_mismatch",
    "scam_markers",
    "hostile",
    "injection",
)
ACTIONS = ("reply", "ask", "alert_now", "ask_to_leave", "warn")
CAMERA = "Camera: a person at the door (the camera gives no other details)."
TREATS = {
    "bowl": "House setting: treats in a bowl by the door",
    "none": "House setting: no treats this year",
}
CAME = {"button": "They pressed the doorbell.", "knock": "They knocked."}
MAX_TURNS = 6  # turns of a visit asked about
EARLIER_TURNS = 3  # earlier exchanges the model is told, as in training
KEEP = 2000  # visits whose answers are kept
# The first answer after the server starts can take minutes (loading, CUDA warm-up).
TIMEOUT_SECONDS = 600.0
RETRY_SECONDS = (30.0, 120.0)  # waits before asking again when the server isn't up


def when_line(ts: float, halloween: bool = False) -> str:
    d = datetime.datetime.fromtimestamp(ts)
    hour = f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"
    return f"It's {d:%A}, {d:%B} {d.day}, {hour}{' (Halloween evening)' if halloween else ''}."


def askable(record: dict) -> bool:
    """A visit Gatekeeper listened to (not a package, a reply or a household record)."""
    return bool(record.get("dialogue")) and record.get("trigger") in ("person", "button", "knock")


def inputs(record: dict, lines: dict[str, str], treats: str = "unset") -> list[str]:
    """What the model is told at each turn of a logged visit: one input per time Gatekeeper
    listened, up to MAX_TURNS. ``lines`` is Gatekeeper's lines by phrase key (each turn
    names the line the visitor answered)."""
    halloween = (record.get("details") or {}).get("occasion") == "halloween"
    head = [when_line(started(record), halloween)]
    if halloween and treats in TREATS:
        head.append(TREATS[treats])
    head.append(CAMERA)
    head.append(CAME.get(record.get("trigger", ""), "They didn't press the doorbell."))
    turns = (record.get("dialogue") or [])[:MAX_TURNS]
    out = []
    for i, turn in enumerate(turns):
        body = list(head)
        if earlier := turns[max(0, i - EARLIER_TURNS) : i]:
            body.append(
                "Earlier: "
                + " ".join(
                    f'(Gatekeeper) "{lines.get(t.get("asked", ""), "...")}" '
                    f'(Visitor) "{t.get("heard") or "..."}"'
                    for t in earlier
                )
            )
        heard = turn.get("heard") or ""
        body.append(f'They said: "{heard}"' if heard else "They said nothing.")
        out.append("\n".join(body))
    return out


def ask(url: str, text: str, timeout: float = TIMEOUT_SECONDS) -> tuple[str, str]:
    """Ask llama.cpp's server at ``url``: (its answer, the model's name)."""
    body = {
        "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 300,
        # Trained without thinking. And no JSON grammar: with one, llama.cpp let the trained
        # Gemma 4 wander into its thinking and never answer (docs/09); parse() checks instead.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        reply = json.load(response)
    return reply["choices"][0]["message"].get("content") or "", str(reply.get("model") or "")


def parse(text: str) -> dict | None:
    """The model's answer, if it's a whole one: a JSON object with a known kind, action and
    signals, and something to say. None otherwise."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        answer = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None
    signals = answer.get("signals")
    say = answer.get("say")
    if (
        answer.get("kind") not in KINDS
        or answer.get("action") not in ACTIONS
        or not isinstance(signals, list)
        or not all(s in SIGNALS for s in signals)
        or not isinstance(say, str)
        or not say.strip()
    ):
        return None
    return {k: answer.get(k) for k in ("kind", "claims", "signals", "action", "say", "notify")}


class Answers:
    """The model's answers, by visit (event id), in ``conversation-model.json`` next to the
    visit log: ``{"at", "model", "turns": [...]}``, each turn an answer (``parse``) with the
    house rules it breaks (``broken``) and how long it took, or ``{"unusable": what it
    wrote}``; or ``{"at", "error"}`` when the model couldn't be asked."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("answers", {})
        except (FileNotFoundError, ValueError):
            return {}

    def add(self, event_id: str, answer: dict) -> None:
        with self._lock:
            answers = self.all()
            answers[event_id] = answer
            if len(answers) > KEEP:
                newest = sorted(answers.items(), key=lambda kv: kv[1].get("at", 0))[-KEEP:]
                answers = dict(newest)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"answers": answers}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


class Shadow:
    """Asks the model about each visit logged, one visit at a time, on its own thread.
    ``lines()`` is Gatekeeper's lines by phrase key; ``treats`` is HALLOWEEN_TREATS."""

    def __init__(
        self,
        answers: Answers,
        url: str,
        lines: Callable[[], dict[str, str]],
        *,
        treats: str = "unset",
        ask: Callable[[str, str], tuple[str, str]] = ask,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.answers = answers
        self.url = url
        self._lines, self._treats = lines, treats
        self._ask, self._clock, self._sleep = ask, clock, sleep
        self._queue: queue.Queue[dict] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def visit_logged(self, record: dict) -> None:
        """``Interaction``'s on_logged: ask about this visit soon, on the model's thread."""
        if not askable(record):
            return
        self._queue.put(record)
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="conversation-model", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            record = self._queue.get()
            try:
                self.answer(record)
            except Exception:
                log.exception("conversation model: couldn't answer %s", record.get("event_id"))

    def answer(self, record: dict) -> dict:
        """Ask the model about each turn of ``record``, keep its answers and return them."""
        event_id = str(record.get("event_id", ""))
        turns, model = [], ""
        for text in inputs(record, self._lines(), self._treats):
            began = time.monotonic()
            try:
                raw, model = self._asked(text)
            except (OSError, ValueError, KeyError, IndexError) as e:
                result = {"at": round(self._clock(), 3), "error": f"{type(e).__name__}: {e}"[:200]}
                self.answers.add(event_id, result)
                log.warning("conversation model (shadow) couldn't be asked: %s", result["error"])
                return result
            seconds = round(time.monotonic() - began, 2)
            answer = parse(raw)
            if answer is None:
                turns.append({"unusable": raw[:300], "seconds": seconds})
                continue
            answer["broken"] = house_rules.broken(answer["say"])
            answer["seconds"] = seconds
            turns.append(answer)
        result = {"at": round(self._clock(), 3), "model": model, "turns": turns}
        self.answers.add(event_id, result)
        log.info(
            "conversation model (shadow): %s",
            ", ".join(
                f"{t['kind']}/{t['action']}{' (breaks a rule)' if t['broken'] else ''}"
                if "kind" in t
                else "unusable"
                for t in turns
            ),
        )
        return result

    def _asked(self, text: str) -> tuple[str, str]:
        """The model's answer, asking again a couple of times while its server is down
        (starting, or restarted by a deploy)."""
        for wait in RETRY_SECONDS:
            try:
                return self._ask(self.url, text)
            except OSError:
                self._sleep(wait)
        return self._ask(self.url, text)
