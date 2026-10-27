"""Frigate as Gatekeeper's perception (``FrigatePerception``): person events from its
``frigate/events`` MQTT topic, zones from its config, frames and snapshots from its API.

This is the only module that knows about Frigate (perception.py says why). Frigate
stays supported for installs that already run it (the Home Assistant add-on); our own
hub will have its own perception behind the same interface.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from gatekeeper.perception import PersonEvent, Point, Shot, decode_jpeg, inside

log = logging.getLogger(__name__)


class FrigatePerception:
    """Perception by Frigate. ``subscribe(topic, handler)`` hands an MQTT topic's
    payloads to ``handler`` (app.py's MQTT client)."""

    def __init__(
        self,
        api: str,
        camera: str,
        *,
        topic: str = "frigate/events",
        subscribe: Callable[[str, Callable[[bytes], None]], None],
        spawn: Callable[[Callable[[], None]], None] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api = api.rstrip("/")
        self.camera = camera
        self.topic = topic
        self._subscribe = subscribe
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.timeout = timeout

    def start(self, on_event: Callable[[PersonEvent], None]) -> None:
        def handle(payload: bytes) -> None:
            event = parse_event(payload)
            if event:
                on_event(event)

        self._subscribe(self.topic, handle)

    def zone(self, name: str) -> Zone:
        zone = Zone(name, self.camera, lambda: fetch_config(self.api))
        self._spawn(zone.load_until_known)  # Frigate may start after us
        return zone

    def latest_frame(self) -> Shot | None:
        """Frigate's latest frame (its detect stream, 640x480 on the rig)."""
        url = f"{self.api}/api/{urllib.parse.quote(self.camera)}/latest.jpg?h=480&quality=90"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = resp.read()
        except Exception as e:
            log.warning("couldn't get a frame from Frigate: %s", e)
            return None
        rgb = decode_jpeg(data)
        return None if rgb is None else Shot(rgb, data)

    def snapshot(self, event_id: str, timeout: float = 3.0) -> bytes | None:
        """The best snapshot of Frigate event ``event_id`` so far (it works while the
        event is in progress), else the camera's latest frame (a doorbell press before
        Frigate saw anyone: "press-<time>")."""
        q = urllib.parse.quote
        if not event_id.startswith("press-"):
            image = _fetch(f"{self.api}/api/events/{q(event_id)}/snapshot.jpg?quality=85", timeout)
            if image:
                return image
        return _fetch(f"{self.api}/api/{q(self.camera)}/latest.jpg?h=720&quality=85", timeout)

    def event_snapshot(self, event_id: str, height: int = 240) -> bytes | None:
        """Frigate's snapshot of event ``event_id`` with its box, or None (expired)."""
        q = urllib.parse.quote
        url = f"{self.api}/api/events/{q(event_id)}/snapshot.jpg?bbox=1&h={height}&quality=80"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404: expired, as old visits' are (not worth a warning)
                log.warning("no snapshot of %s: %s", event_id, e)
        except (urllib.error.URLError, OSError) as e:
            log.warning("no snapshot of %s: %s", event_id, e)
        return None

    def event_path(self, event_id: str) -> list:
        """Where the person of event ``event_id`` went: [((x, y), time)], or [] if Frigate
        doesn't have it (any more)."""
        url = f"{self.api}/api/events/{urllib.parse.quote(event_id)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                data = json.load(resp).get("data") or {}
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug("no path for %s: %s", event_id, e)
            return []
        return [((x, y), t) for (x, y), t in data.get("path_data") or []]

    def recording_frame(self, at: float) -> bytes | None:
        """A full-resolution JPEG of the camera at ``at`` from Frigate's recordings, or
        None (no recording then)."""
        url = f"{self.api}/api/{urllib.parse.quote(self.camera)}/recordings/{at:.3f}/snapshot.jpg"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as e:
            log.debug("no recording frame at %.3f: %s", at, e)
            return None

    def person_snapshot(self, since: float) -> bytes | None:
        """Frigate's snapshot of the first person seen since ``since``."""
        query = urllib.parse.urlencode(
            {"camera": self.camera, "label": "person", "after": f"{since - 10:.0f}", "limit": 20}
        )
        try:
            url = f"{self.api}/api/events?{query}"
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                events = json.load(r)
            if not events:
                return None
            first = min(events, key=lambda e: e["start_time"])
            url = f"{self.api}/api/events/{urllib.parse.quote(first['id'])}/snapshot.jpg?quality=85"
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.read()
        except Exception as e:
            log.warning("no snapshot of the first person since then: %s", e)
            return None


def _fetch(url: str, timeout: float) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError) as e:
        log.warning("no snapshot from %s: %s", url, e)
        return None


def parse_event(raw: bytes) -> PersonEvent | None:
    """Return the event described by a Frigate payload, or None if it is unusable.

    Frigate sends ``{"type": ..., "before": {...}, "after": {...}}``; the
    ``after`` snapshot is preferred, falling back to ``before``.
    """
    try:
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    event = payload.get("after") or payload.get("before") or {}
    if not isinstance(event, dict) or not event.get("id"):
        return None

    return PersonEvent(
        type=payload.get("type", ""),
        id=event["id"],
        camera=event.get("camera"),
        label=event.get("label"),
        entered_zones=tuple(event.get("entered_zones") or ()),
        current_zones=tuple(event.get("current_zones") or ()),
        path=_path(event.get("path_data")),
    )


def _path(data) -> tuple[Point, ...]:
    """Frigate's ``path_data``: ``[[[x, y], frame_time], ...]``."""
    points = []
    for item in data or ():
        try:
            (x, y), _t = item
            points.append((float(x), float(y)))
        except (TypeError, ValueError):
            continue
    return tuple(points)


def zone_polygon(config: dict, camera: str, zone: str) -> list[Point] | None:
    """A zone's outline from Frigate's config (``GET /api/config``), as fractions of
    the frame. Frigate stores coordinates as ``"x1,y1,x2,y2,..."`` (or a list of
    ``"x,y"``), as fractions since 0.14, or in pixels of the detect resolution."""
    try:
        cam = config["cameras"][camera]
        coordinates = cam["zones"][zone]["coordinates"]
    except (KeyError, TypeError):
        return None
    if isinstance(coordinates, str):
        values = coordinates.split(",")
    else:
        values = [v for pair in coordinates for v in str(pair).split(",")]
    try:
        numbers = [float(v) for v in values]
    except ValueError:
        return None
    if len(numbers) < 6 or len(numbers) % 2:
        return None
    if max(numbers) > 1:  # pixels
        width = float(cam.get("detect", {}).get("width") or 0)
        height = float(cam.get("detect", {}).get("height") or 0)
        if not width or not height:
            return None
        numbers = [n / (width if i % 2 == 0 else height) for i, n in enumerate(numbers)]
    return list(zip(numbers[0::2], numbers[1::2], strict=True))


def fetch_config(api: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(f"{api.rstrip('/')}/api/config", timeout=timeout) as resp:
        return json.load(resp)


class Zone:
    """A Frigate zone's outline, read from Frigate's config by ``load_until_known``
    on its own thread (Frigate may start after us). ``contains`` never waits."""

    def __init__(self, name: str, camera: str, load: Callable[[], dict]) -> None:
        self.name = name
        self.camera = camera
        self._load = load
        self._polygon: list[Point] | None = None

    def load(self, level: int = logging.WARNING) -> bool:
        try:
            polygon = zone_polygon(self._load(), self.camera, self.name)
        except Exception as e:
            log.log(level, "couldn't read Frigate's config for zone %r: %s", self.name, e)
            return False
        if polygon is None:
            log.warning("Frigate has no zone %r on camera %s", self.name, self.camera)
            return False
        self._polygon = polygon
        log.info("zone %r on %s: %s", self.name, self.camera, polygon)
        return True

    def load_until_known(
        self,
        retry_seconds: float = 10.0,
        quiet_seconds: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Retry until Frigate answers. A deploy restarts Frigate together with us, so
        not reaching it is only worth a warning after ``quiet_seconds``."""
        start = clock()
        while not self.load(logging.INFO if clock() - start < quiet_seconds else logging.WARNING):
            sleep(retry_seconds)

    @property
    def polygon(self) -> list[Point] | None:
        """The zone's outline, or None while it's unknown."""
        return self._polygon

    def contains(self, point: Point) -> bool | None:
        """Is ``point`` in the zone? None while the zone is unknown."""
        if self._polygon is None:
            return None
        return inside(self._polygon, point)
