"""Parsing of Frigate ``frigate/events`` MQTT payloads."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class FrigateEvent:
    type: str  # "new", "update" or "end"
    id: str
    camera: str | None
    label: str | None
    entered_zones: tuple[str, ...] = ()
    current_zones: tuple[str, ...] = ()  # where the person is now


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
    )
