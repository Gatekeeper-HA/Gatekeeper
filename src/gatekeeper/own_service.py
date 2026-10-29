"""Our own perception as what sees the door (``PERCEPTION=own``): it starts the visits,
and Frigate isn't needed (the Frigate decision, step 3).

Everything Gatekeeper asks of a perception (perception.py), from our own parts:
- **person events**: own perception's tracks (own_perception.py);
- **zones**: Gatekeeper's own (zones.py), drawn on the Zones page;
- **the latest frame**: own perception's newest frame of the low-resolution stream;
- **snapshots**: each person's best picture (the frame where the detector was surest of
  them); on the Visits page, the recording's full-resolution picture of them, made small;
- **for the delivery guess**: each person's path, with times, and full-resolution frames
  from the recordings (recording.py).

``fallback`` (Frigate, when there is one) only stands in for zones not drawn here and for
visits from before the switch, which Frigate saw.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from gatekeeper.perception import PersonEvent, Shot

log = logging.getLogger(__name__)

STALE_SECONDS = 5.0  # a newest frame older than this is no picture of the camera now
KEEP_TRACKS = 200  # people who've gone, remembered for their paths and snapshots
SMALL_SUFFIX = "-h{height}.jpg"  # a recording's picture made small for the Visits page


def is_own(event_id: str) -> bool:
    return "-own" in event_id


def scaled_jpeg(data: bytes, height: int) -> bytes | None:
    """A JPEG made ``height`` rows high (never bigger than it was)."""
    import av

    from gatekeeper.face_admin import jpeg_bytes

    try:
        with av.open(io.BytesIO(data)) as c:
            frame = next(c.decode(video=0))
            if frame.height <= height:
                return data
            width = round(frame.width * height / frame.height / 2) * 2
            return jpeg_bytes(frame.to_ndarray(format="bgr24", width=width, height=height))
    except Exception as e:
        log.debug("couldn't scale a picture: %s", e)
        return None


class OwnService:
    """Gatekeeper's perception from its own parts. ``attach`` the OwnPerception once it's
    built; it calls ``on_event`` and ``on_track``."""

    def __init__(
        self,
        zones,
        clip_dir: Path | None = None,
        *,
        fallback=None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.own = None
        self._zones = zones
        self._clip_dir = clip_dir
        self._fallback = fallback
        self._clock = clock or time.time
        self._listeners: list[Callable[[PersonEvent], None]] = []
        self._tracks: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()
        self._indexes: dict[Path, tuple[float, list[dict]]] = {}

    def attach(self, own) -> None:
        self.own = own

    # -- from own perception -------------------------------------------------------

    def on_event(self, event: PersonEvent) -> None:
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                log.exception("person event handler failed")

    def on_track(self, track) -> None:
        with self._lock:
            self._tracks[track.id] = track
            while len(self._tracks) > KEEP_TRACKS:
                self._tracks.popitem(last=False)

    # -- Perception ----------------------------------------------------------------

    def start(self, on_event: Callable[[PersonEvent], None]) -> None:
        self._listeners.append(on_event)

    def zone(self, name: str):
        return self._zones.zone(name)

    def latest_frame(self) -> Shot | None:
        newest = self.own.newest() if self.own is not None else None
        if newest is None or self._clock() - newest[0] > STALE_SECONDS:
            return None
        from gatekeeper.face_admin import jpeg_bytes

        rgb = newest[1].to_ndarray(format="rgb24")
        return Shot(rgb, jpeg_bytes(rgb[:, :, ::-1]))

    def snapshot(self, event_id: str) -> bytes | None:
        picture = self._picture(event_id)
        if picture is not None:
            return picture
        if (
            self._fallback is not None
            and not is_own(event_id)
            and not event_id.startswith("press-")
        ):
            image = self._fallback.snapshot(event_id)
            if image:
                return image
        shot = self.latest_frame()
        return shot.jpeg if shot else None

    def event_snapshot(self, event_id: str, height: int = 240) -> bytes | None:
        if not is_own(event_id):
            return self._fallback.event_snapshot(event_id, height) if self._fallback else None
        path = self._recorded_picture(event_id)
        if path is not None:
            small = path.with_name(path.stem + SMALL_SUFFIX.format(height=height))
            if small.exists():
                return small.read_bytes()
            data = scaled_jpeg(path.read_bytes(), height)
            if data:
                try:
                    small.write_bytes(data)
                except OSError:
                    pass
                return data
        picture = self._picture(event_id)
        return scaled_jpeg(picture, height) if picture else None

    def person_snapshot(self, since: float) -> bytes | None:
        """The best picture of the first person seen since ``since``."""
        for track in sorted(self._known_tracks(), key=lambda t: t.first):
            if track.first >= since - 10 and (picture := self._picture(track.id)):
                return picture
        return None

    # -- for the delivery guess ---------------------------------------------------------

    def event_path(self, event_id: str) -> list:
        """Where the person went: [((x, y), time)]."""
        if not is_own(event_id):
            return self._fallback.event_path(event_id) if self._fallback else []
        track = next((t for t in self._known_tracks() if t.id == event_id), None)
        if track is None:
            return []
        return [(tuple(p), t) for p, t in zip(track.path, track.times, strict=False)]

    def recording_frame(self, at: float) -> bytes | None:
        """A full-resolution JPEG of the camera at ``at``, from a recording."""
        from gatekeeper.recording import clip_frame

        for folder, clip in self._clips_around(at):
            if clip["start"] <= at <= clip["end"]:
                data = clip_frame(folder / clip["file"], at - clip["start"])
                if data:
                    return data
        if self._fallback is not None:
            return self._fallback.recording_frame(at)
        return None

    # -- helpers -------------------------------------------------------------------

    def _known_tracks(self) -> list:
        with self._lock:
            ended = list(self._tracks.values())
        active = list(self.own.tracker.tracks) if self.own is not None else []
        return [t for t in active if t.confirmed] + ended

    def _picture(self, event_id: str) -> bytes | None:
        if self.own is None:
            return None
        return self.own.pictures.get(event_id)

    def _clips_around(self, at: float) -> list[tuple[Path, dict]]:
        """The recordings' indexes for the day of ``at`` and the day before (a clip
        started before midnight)."""
        if self._clip_dir is None:
            return []
        out = []
        for days in (0, 1):
            day = datetime.datetime.fromtimestamp(at) - datetime.timedelta(days=days)
            folder = self._clip_dir / day.strftime("%Y-%m-%d")
            out += [(folder, clip) for clip in self._index(folder)]
        return out

    def _index(self, folder: Path) -> list[dict]:
        try:
            changed = folder.stat().st_mtime
        except OSError:
            return []
        cached = self._indexes.get(folder)
        if cached and cached[0] == changed:
            return cached[1]
        clips = []
        for path in sorted(folder.glob("*.json")):
            try:
                clips.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        self._indexes[folder] = (changed, clips)
        return clips

    def _recorded_picture(self, event_id: str) -> Path | None:
        try:
            at = float(event_id.split("-", 1)[0])
        except ValueError:
            return None
        for folder, clip in self._clips_around(at):
            for person in clip.get("people", []):
                if person.get("track") == event_id and person.get("picture"):
                    path = folder / person["picture"]
                    if path.exists():
                        return path
        return None
