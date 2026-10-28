"""Gatekeeper's own zones: the porch, the front door and the doorstep, drawn on the camera's
picture on the Zones page (``zones/`` on the web page) and kept in ``zones.json`` next to
the visit log.

A zone not drawn here is Frigate's (read from Frigate's config), so a setup with Frigate
works as before; once drawn, it's Gatekeeper's, and Frigate's no longer matters to it.
Our own perception, the household-at-the-door check and the doorstep check use these
shapes. Which of Frigate's events start a visit is still decided by Frigate's own zones
until our own perception starts the visits (the Frigate decision, step 3): on a hub
without Frigate, these are the only zones.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path

from gatekeeper.perception import Point, ZoneShape, inside

log = logging.getLogger(__name__)

MAX_POINTS = 24
POINT = re.compile(r"^\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*$")


class ZoneStore:
    """The zones drawn for each camera: ``{"cameras": {camera: {zone: [[x, y], ...]}}}``,
    as fractions of the frame (0-1, from the top left)."""

    def __init__(self, path: Path, camera: str) -> None:
        self.path = path
        self.camera = camera
        self._lock = threading.Lock()
        self._zones: dict[str, list[Point]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, points in (data.get("cameras", {}).get(camera) or {}).items():
                self._zones[name] = [(float(x), float(y)) for x, y in points]
        except FileNotFoundError:
            pass
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("couldn't read %s (%s); using Frigate's zones", path.name, e)

    def get(self, name: str) -> list[Point] | None:
        return self._zones.get(name)

    def put(self, name: str, polygon: list[Point] | None) -> None:
        """Keep ``polygon`` as zone ``name``; None forgets it (back to Frigate's)."""
        with self._lock:
            if polygon is None:
                self._zones.pop(name, None)
            else:
                self._zones[name] = list(polygon)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                data = {}
            cameras = data.setdefault("cameras", {})
            cameras[self.camera] = {
                n: [[round(x, 4), round(y, 4)] for x, y in p] for n, p in self._zones.items()
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.path)


class Zone:
    """A zone by name: Gatekeeper's own if drawn, else ``fallback``'s (Frigate's), read
    afresh each time, so a zone drawn on the page applies at once."""

    def __init__(self, name: str, store: ZoneStore, fallback: ZoneShape | None = None) -> None:
        self.name = name
        self._store = store
        self._fallback = fallback

    @property
    def source(self) -> str | None:
        """ "gatekeeper", "frigate" (the fallback), or None while it isn't known."""
        if self._store.get(self.name) is not None:
            return "gatekeeper"
        if self._fallback is not None and self._fallback.polygon is not None:
            return "frigate"
        return None

    @property
    def polygon(self) -> list[Point] | None:
        own = self._store.get(self.name)
        if own is not None:
            return own
        return self._fallback.polygon if self._fallback is not None else None

    def contains(self, point: Point) -> bool | None:
        polygon = self.polygon
        return None if polygon is None else inside(polygon, point)


class Zones:
    """The zones Gatekeeper uses, one ``Zone`` per name. ``fallback(name)``: the zone from
    elsewhere (FrigatePerception.zone), asked once per name."""

    def __init__(
        self, store: ZoneStore, fallback: Callable[[str], ZoneShape] | None = None
    ) -> None:
        self.store = store
        self._fallback = fallback
        self._zones: dict[str, Zone] = {}
        self._lock = threading.Lock()

    def zone(self, name: str) -> Zone:
        with self._lock:
            if name not in self._zones:
                fallback = self._fallback(name) if self._fallback else None
                self._zones[name] = Zone(name, self.store, fallback)
            return self._zones[name]


def parse_points(text: str) -> list[Point] | None:
    """``"x,y x,y ..."`` (fractions of the frame) as a polygon; None for a blank one.
    ValueError if it isn't one."""
    parts = text.replace(";", " ").split()
    if not parts:
        return None
    points = []
    for part in parts:
        m = POINT.match(part)
        if not m:
            raise ValueError(f"{part!r} isn't a point: write x,y as fractions of the frame")
        x, y = float(m[1]), float(m[2])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError(f"{part!r} is outside the frame: x and y go from 0 to 1")
        points.append((x, y))
    if len(points) < 3:
        raise ValueError("a zone needs at least 3 points")
    if len(points) > MAX_POINTS:
        raise ValueError(f"a zone has at most {MAX_POINTS} points")
    return points


def format_points(polygon: list[Point] | None) -> str:
    return " ".join(f"{x:.3f},{y:.3f}" for x, y in polygon or [])


COLOURS = ["#ffb300", "#00b0ff", "#76ff03", "#ff4081", "#e040fb"]

# Drawing on the picture: pick a zone, click its corners in order. Without scripts the
# outlines still show, and the points can be typed.
SCRIPT = """
const svg = document.getElementById('shapes');
const inputs = [...document.querySelectorAll('input[data-zone]')];
function pick() { return document.querySelector('input[name=draw]:checked')?.value; }
function points(text) {
  return text.trim().split(/[\\s;]+/).filter(Boolean).map(p => p.split(',').map(Number))
    .filter(p => p.length === 2 && p.every(v => v >= 0 && v <= 1));
}
function redraw() {
  for (const input of inputs) {
    const shape = document.getElementById('shape-' + input.dataset.zone);
    const text = input.value.trim() || input.placeholder;  // blank: Frigate's
    shape.setAttribute('points', points(text).map(p => p.join(',')).join(' '));
    shape.classList.toggle('picked', input.dataset.zone === pick());
  }
}
document.getElementById('frame').addEventListener('click', e => {
  const zone = pick(); if (!zone) return;
  const r = e.currentTarget.getBoundingClientRect();
  const x = ((e.clientX - r.left) / r.width).toFixed(3);
  const y = ((e.clientY - r.top) / r.height).toFixed(3);
  const input = inputs.find(i => i.dataset.zone === zone);
  input.value = (input.value.trim() + ' ' + x + ',' + y).trim(); redraw();
});
for (const b of document.querySelectorAll('button[data-undo]')) b.addEventListener('click', () => {
  const input = inputs.find(i => i.dataset.zone === b.dataset.undo);
  input.value = input.value.trim().split(/\\s+/).slice(0, -1).join(' '); redraw();
});
for (const b of document.querySelectorAll('button[data-clear]')) b.addEventListener('click', () => {
  inputs.find(i => i.dataset.zone === b.dataset.clear).value = ''; redraw();
});
for (const el of [...inputs, ...document.querySelectorAll('input[name=draw]')])
  el.addEventListener('input', redraw);
for (const b of document.querySelectorAll('.drawonly')) b.hidden = false;
redraw();
"""

ZONE_STYLE = """
.frame { position: relative; max-width: 640px; cursor: crosshair; }
.frame img { width: 100%; display: block; border-radius: 6px; }
.frame svg { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
.frame polygon { fill-opacity: 0.2; stroke-width: 2; vector-effect: non-scaling-stroke; }
.frame polygon.picked { fill-opacity: 0.35; stroke-width: 4; }
.zone { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 8px 12px;
  margin: 8px 0; } .zone input[type=text] { width: 100%; box-sizing: border-box; }
.swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; }
@media (prefers-color-scheme: dark) { .zone { background: #222226; border-color: #3a3a40; } }
"""


class ZonesPage:
    """The Zones page. ``purposes``: the zones Gatekeeper uses, by name, with what each
    is for; ``frame``: the camera's latest picture (JPEG) to draw on."""

    token = ""  # set by web.serve: one token for every page's forms

    def __init__(
        self,
        zones: Zones,
        purposes: dict[str, str],
        frame: Callable[[], bytes | None],
    ) -> None:
        self.zones = zones
        self.purposes = purposes
        self._frame = frame

    def frame_jpeg(self) -> bytes | None:
        return self._frame()

    def save(self, form: dict[str, str]) -> str:
        """Each ``zone:<name>`` field: its points, or blank for Frigate's. Checked before
        anything is kept; ValueError says what's wrong."""
        changes = {}
        for name in self.purposes:
            if f"zone:{name}" not in form:
                continue
            try:
                polygon = parse_points(form[f"zone:{name}"])
            except ValueError as e:
                raise ValueError(f"The {name} zone: {e}") from None
            if polygon != self.zones.store.get(name):
                changes[name] = polygon
        for name, polygon in changes.items():
            self.zones.store.put(name, polygon)
            if polygon is None:
                log.info("web: zone %r back to Frigate's", name)
            else:
                log.info("web: zone %r drawn: %s", name, format_points(polygon))
        return f"Saved {len(changes)} zone(s)." if changes else "Nothing changed."

    def render(self, message: str = "", error: str = "") -> str:
        from gatekeeper.web import STYLE, nav

        e = html.escape
        out = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Zones</title><style>{STYLE}{ZONE_STYLE}</style></head><body>",
            nav("zones"),
            "<h1>Zones</h1>",
        ]
        if message:
            out.append(f"<p class=done role=status>{e(message)}</p>")
        if error:
            out.append(f"<p class=error role=alert>{e(error)}</p>")
        out.append(
            "<p class=note>Where Gatekeeper looks. Pick a zone, then click its corners on "
            "the picture in order (or type the points: x,y from the top left, as fractions "
            "of the picture). A zone left blank is Frigate's. Which of Frigate's events "
            "start a visit is still decided by Frigate's own zones, so keep the two alike "
            "until Gatekeeper's own perception starts the visits.</p>"
        )
        out.append('<div class=frame id=frame><img src="frame.jpg" alt="The camera now">')
        out.append('<svg viewBox="0 0 1 1" preserveAspectRatio="none" id=shapes>')
        for n, name in enumerate(self.purposes):
            colour = COLOURS[n % len(COLOURS)]
            points = " ".join(f"{x},{y}" for x, y in self.zones.zone(name).polygon or [])
            out.append(
                f'<polygon id="shape-{e(name)}" points="{points}" fill="{colour}" '
                f'stroke="{colour}"/>'
            )
        out.append("</svg></div>")
        out.append(
            f'<form method=post action="save"><input type=hidden name=token value="{self.token}">'
        )
        for n, (name, purpose) in enumerate(self.purposes.items()):
            zone = self.zones.zone(name)
            own = self.zones.store.get(name)
            source = {
                "gatekeeper": "drawn here",
                "frigate": "Frigate's (blank: keep using it)",
                None: "not known yet",
            }[zone.source]
            colour = COLOURS[n % len(COLOURS)]
            out.append(
                f'<div class=zone><label><input type=radio name=draw value="{e(name)}"'
                f"{' checked' if n == 0 else ''}> <span class=swatch "
                f'style="background:{colour}"></span> <strong>{e(name)}</strong></label>: '
                f"{e(purpose)}. <span class=note>Now {source}.</span><br>"
                f'<input type=text name="zone:{e(name)}" data-zone="{e(name)}" '
                f'value="{format_points(own)}" placeholder="{format_points(zone.polygon)}" '
                f'aria-label="{e(name)} zone points"> '
                f'<button type=button class=drawonly data-undo="{e(name)}" hidden>'
                "Undo last point</button>"
                f'<button type=button class=drawonly data-clear="{e(name)}" hidden>'
                "Clear (Frigate's)</button></div>"
            )
        out.append(
            "<div class=savebar><button type=submit>Save zones</button></div></form>"
            f"<script>{SCRIPT}</script></body></html>"
        )
        return "\n".join(out)
