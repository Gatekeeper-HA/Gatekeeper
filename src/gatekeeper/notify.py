"""Telling the resident who is at the door.

The Notifier runs as an ``Interaction.on_notify`` callback, once per visit:
right after the visitor's answer is classified (before the reply plays), or
when the visit fails. Sending happens on a background thread so the visit
isn't delayed; the Frigate snapshot of the visitor is attached when available.

Backends: ntfy (self-hosted or ntfy.sh). Home Assistant notify can be added as
another backend with the same ``send(notification, image)`` method.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from email.header import Header
from typing import Protocol

from gatekeeper.interaction import Visit

log = logging.getLogger(__name__)

# classification -> (title, ntfy tags, ntfy priority 1-5)
STYLES = {
    "likely_delivery": ("Delivery at the {place}", ["package"], 3),
    "solicitor": ("Solicitor at the {place}", ["no_entry_sign"], 2),
    "service_visit": ("Service visit at the {place}", ["wrench"], 4),
    "cooperative_other": ("Visitor at the {place}", ["wave"], 4),
    "no_response": ("Someone at the {place}", ["bust_in_silhouette"], 3),
}

OUTCOME_TEXT = {
    "talkback_busy": "another visit was in progress",
    "talkback_failed": "the doorbell speaker didn't connect",
    "talkback_unavailable": "talkback isn't available",
    "timeout": "the visit timed out",
    "error": "an error occurred",
}


@dataclass
class Notification:
    title: str
    message: str
    tags: list[str] = field(default_factory=list)
    priority: int = 3


def compose(visit: Visit) -> Notification:
    place = visit.camera.replace("_", " ")
    if visit.outcome == "pressed_during_visit":
        return Notification(
            title=f"Doorbell pressed at the {place}",
            message="Pressed during or just after a visit Gatekeeper handled.",
            tags=["bell"],
            priority=4,
        )
    if visit.classification is None:
        reason = OUTCOME_TEXT.get(visit.outcome, visit.outcome)
        return Notification(
            title=f"Someone at the {place}",
            message=f"Gatekeeper couldn't talk to them: {reason}.",
            tags=["warning"],
            priority=4,
        )
    title, tags, priority = STYLES.get(visit.classification, STYLES["cooperative_other"])
    said = f'Said: "{visit.transcript}"' if visit.transcript else "No answer."
    if visit.trigger == "button":
        tags = ["bell", *tags]
    return Notification(
        title=title.format(place=place),
        message=f"{said}\nReplied: {visit.response}",
        tags=list(tags),
        priority=priority,
    )


class Backend(Protocol):
    name: str

    def send(self, notification: Notification, image: bytes | None) -> None: ...


def _header(value: str) -> str:
    """HTTP header value; non-ASCII text is RFC 2047-encoded (ntfy decodes it)."""
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()


class NtfyBackend:
    name = "ntfy"

    def __init__(self, url: str, topic: str, token: str = "", timeout: float = 5.0) -> None:
        self.endpoint = f"{url.rstrip('/')}/{urllib.parse.quote(topic)}"
        self.token = token
        self.timeout = timeout

    def request(self, n: Notification, image: bytes | None) -> urllib.request.Request:
        headers = {
            "Title": _header(n.title),
            "Tags": ",".join(n.tags),
            "Priority": str(n.priority),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if image:
            # The body is the attachment; ntfy reads the message from this
            # header and turns literal "\n" into newlines.
            headers["Message"] = _header(n.message.replace("\n", "\\n"))
            headers["Filename"] = "visitor.jpg"
            return urllib.request.Request(self.endpoint, image, headers, method="PUT")
        return urllib.request.Request(
            self.endpoint, n.message.encode("utf-8"), headers, method="POST"
        )

    def send(self, notification: Notification, image: bytes | None) -> None:
        with urllib.request.urlopen(self.request(notification, image), timeout=self.timeout):
            pass


def _fetch(url: str, timeout: float) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        log.warning("no snapshot from %s: %s", url, e)
        return None


def frigate_snapshot(api: str, visit: Visit, timeout: float = 3.0) -> bytes | None:
    """The best snapshot of the visit's Frigate event so far (works while it's in
    progress), else the camera's latest frame (e.g. a doorbell press before
    Frigate saw anyone)."""
    base = api.rstrip("/")
    q = urllib.parse.quote
    if not visit.event_id.startswith("press-"):
        image = _fetch(f"{base}/api/events/{q(visit.event_id)}/snapshot.jpg?quality=85", timeout)
        if image:
            return image
    return _fetch(f"{base}/api/{q(visit.camera)}/latest.jpg?h=720&quality=85", timeout)


class Notifier:
    def __init__(
        self,
        backends: Sequence[Backend],
        snapshot: Callable[[Visit], bytes | None] = lambda visit: None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backends = list(backends)
        self._snapshot = snapshot
        self._clock = clock
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify")

    def __call__(self, visit: Visit) -> None:
        if self.backends:
            self._executor.submit(self.send, replace(visit))

    def send(self, visit: Visit) -> None:
        started = self._clock()
        notification = compose(visit)
        image = self._snapshot(visit)
        for backend in self.backends:
            try:
                backend.send(notification, image)
                log.info(
                    "notified via %s in %.2f s: %s%s",
                    backend.name,
                    self._clock() - started,
                    notification.title,
                    " (with snapshot)" if image else "",
                )
            except Exception as e:
                log.error("notification via %s failed: %s", backend.name, e)
