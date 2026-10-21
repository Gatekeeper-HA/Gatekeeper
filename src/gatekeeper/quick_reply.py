"""Quick replies: the resident answers the visitor from the notification.

The doorbell notification offers buttons (QUICK_REPLIES: "I'm coming", "Not now",
"No thanks"); pressing one makes Gatekeeper say that line at the door
(``Interaction.quick_reply``). Two ways in:

- **ntfy:** each button posts ``<event id> <reply> <signature>`` to the reply topic
  (NTFY_REPLY_TOPIC), which ``NtfyReplies`` follows. The signature is an HMAC with
  a key made at startup, so only a notification's buttons work, though anyone may
  post to the topic (the phone posts without logging in).
- **MQTT (Home Assistant):** ``<reply>``, or ``{"reply": ..., "event_id": ...}``,
  on ``gatekeeper/<camera>/quick_reply``. Discovery gives Home Assistant a button
  per reply; without an event id it answers the latest visit.

A reply is said only for a visit notified in the last QUICK_REPLY_WINDOW_SECONDS,
once per reply and visit, and, if a visit is running, after it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

# Button labels (ntfy) and Home Assistant button names.
LABELS = {"coming": "I'm coming", "later": "Not now", "no_thanks": "No thanks"}
# ntfy shows at most 3 buttons.
MAX_BUTTONS = 3

# say(event_id, reply, seconds since the visit was notified (None: never, or
# forgotten), seconds left to say it (<= 0: too late))
SayFn = Callable[[str, str, float | None, float], None]


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="quick-reply", daemon=True).start()


class QuickReplies:
    def __init__(
        self,
        replies: Sequence[str],
        window_seconds: float,
        say: SayFn,
        *,
        clock: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
        key: bytes | None = None,
    ) -> None:
        self.replies = tuple(replies)[:MAX_BUTTONS]
        self.window_seconds = window_seconds
        self._say = say
        self._clock = clock
        self._spawn = spawn
        self._key = key or secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._notified: dict[str, float] = {}  # event id -> when it was first notified
        self._said: set[tuple[str, str]] = set()

    def sign(self, event_id: str, reply: str) -> str:
        message = f"{event_id} {reply}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:16]

    def buttons(self, visit) -> list[tuple[str, str]]:
        """A visit's notification buttons: (label, what it posts to the reply topic).
        None mid-conversation (the interim alert while an officer is questioned), or
        for a package alert or the household (no one to answer)."""
        if visit.stage == "alert" or visit.trigger == "package":
            return []
        if visit.outcome in ("entered", "household"):
            return []
        return [
            (LABELS[r], f"{visit.event_id} {r} {self.sign(visit.event_id, r)}")
            for r in self.replies
        ]

    def notified(self, visit) -> None:
        """An ``Interaction.on_notify`` callback: the window starts now."""
        now = self._clock()
        with self._lock:
            self._notified.setdefault(visit.event_id, now)
            # Forget visits (and their replies) long past the window.
            old = {e for e, t in self._notified.items() if now - t > 2 * self.window_seconds}
            for event_id in old:
                del self._notified[event_id]
            self._said = {s for s in self._said if s[0] not in old}

    def from_ntfy(self, text: str) -> None:
        """A message on the reply topic: a button's ``<event id> <reply> <signature>``."""
        parts = text.split()
        if len(parts) != 3:
            log.warning("ignoring a quick reply that isn't from a notification button")
            return
        event_id, reply, signature = parts
        if not hmac.compare_digest(signature, self.sign(event_id, reply)):
            log.warning("ignoring a quick reply with a bad signature (from before a restart?)")
            return
        self.request(event_id, reply)

    def from_mqtt(self, payload: bytes) -> None:
        """``<reply>`` or ``{"reply": ..., "event_id": ...}``; without an event id,
        the latest visit."""
        text = payload.decode("utf-8", "replace").strip()
        event_id = None
        if text.startswith("{"):
            try:
                data = json.loads(text)
                text, event_id = str(data.get("reply", "")), data.get("event_id")
            except (ValueError, AttributeError):
                log.warning("ignoring a quick reply that isn't valid JSON")
                return
        if event_id is None:
            with self._lock:
                event_id = max(self._notified, key=self._notified.get, default=None)
            if event_id is None:
                log.info("quick reply %r: no visit to answer", text)
                return
        self.request(str(event_id), text)

    def request(self, event_id: str, reply: str) -> bool:
        """Say ``reply`` for visit ``event_id`` (on a thread of its own). False if
        it's not a reply on offer, or was already said for this visit."""
        if reply not in self.replies:
            log.warning("ignoring unknown quick reply %r", reply)
            return False
        now = self._clock()
        with self._lock:
            if (event_id, reply) in self._said:
                log.info("quick reply %r for %s already said", reply, event_id)
                return False
            self._said.add((event_id, reply))
            notified = self._notified.get(event_id)
        after = None if notified is None else now - notified
        left = 0.0 if notified is None else notified + self.window_seconds - now
        self._spawn(lambda: self._say(event_id, reply, after, left))
        return True


class NtfyReplies:
    """Follows the ntfy reply topic and hands each message to ``on_message``."""

    def __init__(
        self,
        url: str,
        topic: str,
        token: str,
        on_message: Callable[[str], None],
        *,
        open_url: Callable[..., object] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        read_timeout: float = 120.0,  # ntfy sends a keepalive every 45 s
    ) -> None:
        self.endpoint = f"{url.rstrip('/')}/{urllib.parse.quote(topic)}/json"
        self.topic = topic
        self._token = token
        self._on_message = on_message
        self._open = open_url
        self._sleep = sleep
        self._read_timeout = read_timeout
        self._since: str | None = None  # the last message id: don't miss any on a reconnect
        self._last_error: str | None = None

    def follow_once(self) -> None:
        """One connection: read messages until it drops (raises)."""
        url = self.endpoint
        if self._since:
            url += f"?since={urllib.parse.quote(self._since)}"
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        request = urllib.request.Request(url, headers=headers)
        with self._open(request, timeout=self._read_timeout) as stream:
            log.info("following quick replies on ntfy topic %s", self.topic)
            self._last_error = None
            for line in stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("event") != "message":
                    continue  # open, keepalive
                self._since = message.get("id", self._since)
                try:
                    self._on_message(message.get("message", ""))
                except Exception:
                    log.exception("quick reply failed")

    def run_forever(self) -> None:
        delay = 5.0
        while True:
            started = time.monotonic()
            try:
                self.follow_once()
            except Exception as e:
                # Once per kind of error: a server that refuses the topic (no
                # access rule) would otherwise warn every few minutes, forever.
                level = logging.DEBUG if str(e) == self._last_error else logging.WARNING
                log.log(level, "quick replies: can't follow ntfy topic %s (%s)", self.topic, e)
                self._last_error = str(e)
            if time.monotonic() - started > 60:
                delay = 5.0  # it was up for a while: retry soon
            self._sleep(delay)
            delay = min(delay * 2, 300.0)
