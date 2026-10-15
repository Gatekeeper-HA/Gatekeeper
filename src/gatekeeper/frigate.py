"""Parsing of Frigate ``frigate/events`` MQTT payloads, and Frigate's zones."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

Point = tuple[float, float]  # x, y as fractions of the frame (0-1)


@dataclass(frozen=True)
class FrigateEvent:
    type: str  # "new", "update" or "end"
    id: str
    camera: str | None
    label: str | None
    entered_zones: tuple[str, ...] = ()
    current_zones: tuple[str, ...] = ()  # where the person is now
    # Where the person has been: the bottom centre of their box, a point each time
    # they moved a little (Frigate 0.14+). The first point is where they appeared.
    path: tuple[Point, ...] = ()


def parse_event(raw: bytes) -> FrigateEvent | None:
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

    return FrigateEvent(
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


def inside(polygon: Sequence[Point], point: Point) -> bool:
    """Is ``point`` inside ``polygon`` (ray casting)?

    Frigate's points are often on the frame's bottom edge (y = 1.0, feet out of
    view), which the ray test would leave out of a zone drawn to that edge, so
    points are nudged just inside the frame first."""
    x, y = (min(max(v, 0.0), 0.9999) for v in point)
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


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
