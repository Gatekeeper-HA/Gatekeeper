"""What Gatekeeper needs from whatever watches the door: its perception.

Gatekeeper decides what a visit means and what to do about it; seeing the people is
the perception's job. Today that's Frigate (``frigate.FrigatePerception``). On our own
hub it will be our own service, and anything that provides these can replace it
(design doc 03, principle 2):

- **person events** (``PersonEvent``): someone appeared, moved, entered zones, left;
- **zones** by name (``ZoneShape``): is a point in one, and its outline;
- **the camera's latest frame** (``Shot``), for the doorstep check (packages.py);
- **snapshots**: the best one of a person event, and of the first person seen since a
  time (who came for a package).

New features use these, not Frigate directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

log = logging.getLogger(__name__)

Point = tuple[float, float]  # x, y as fractions of the frame (0-1)


@dataclass(frozen=True)
class PersonEvent:
    """A person on camera: ``type`` "new" (first seen), "update" or "end" (gone)."""

    type: str
    id: str
    camera: str | None
    label: str | None
    entered_zones: tuple[str, ...] = ()
    current_zones: tuple[str, ...] = ()  # where the person is now
    # Where the person has been: the bottom centre of their box, a point each time
    # they moved a little. The first point is where they appeared.
    path: tuple[Point, ...] = ()


@dataclass
class Shot:
    """A frame: RGB pixels, and the JPEG they came from (to attach to a notification)."""

    rgb: np.ndarray
    jpeg: bytes


@runtime_checkable
class ZoneShape(Protocol):
    """A named area of the camera's view."""

    @property
    def polygon(self) -> list[Point] | None:
        """Its outline, or None while it isn't known (yet)."""

    def contains(self, point: Point) -> bool | None:
        """Is ``point`` in it? None while it isn't known."""


@runtime_checkable
class Perception(Protocol):
    def start(self, on_event: Callable[[PersonEvent], None]) -> None:
        """Call ``on_event`` with each person event from now on."""

    def zone(self, name: str) -> ZoneShape:
        """The zone called ``name`` (learned in the background if need be)."""

    def latest_frame(self) -> Shot | None:
        """The camera's latest frame, ~480 rows high, or None if it can't be had."""

    def snapshot(self, event_id: str) -> bytes | None:
        """A JPEG of person event ``event_id`` (the best so far, while it lasts), or of
        the camera now if there's no such event (a doorbell press before anyone was
        seen)."""

    def event_snapshot(self, event_id: str, height: int = 240) -> bytes | None:
        """A JPEG of person event ``event_id``, about ``height`` rows high, or None if
        there's none any more (the Visits page: never the camera now instead)."""

    def person_snapshot(self, since: float) -> bytes | None:
        """A JPEG of the first person seen since ``since``."""


def inside(polygon: Sequence[Point], point: Point) -> bool:
    """Is ``point`` inside ``polygon`` (ray casting)?

    Tracked points are often on the frame's bottom edge (y = 1.0, feet out of view),
    which the ray test would leave out of a zone drawn to that edge, so points are
    nudged just inside the frame first."""
    x, y = (min(max(v, 0.0), 0.9999) for v in point)
    hit = False
    for (x1, y1), (x2, y2) in zip(polygon, [*polygon[1:], polygon[0]], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def decode_jpeg(data: bytes, height: int = 480) -> np.ndarray | None:
    """RGB pixels of a JPEG, scaled to ``height`` rows, keeping its shape (640 wide for
    a 4:3 camera, 854 for 16:9). PyAV decodes it."""
    import io

    import av

    try:
        with av.open(io.BytesIO(data)) as container:
            frame = next(container.decode(video=0))
            width = round(frame.width * height / frame.height / 2) * 2
            return frame.reformat(width, height, format="rgb24").to_ndarray()
    except Exception as e:
        log.warning("couldn't decode a frame: %s", e)
        return None
