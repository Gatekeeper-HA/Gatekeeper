"""Gatekeeper's own recordings (``RECORDING``): a clip of the door while someone is there,
and a picture of each person, so that our hub needn't carry Frigate to keep them (the
Frigate decision, step 2).

Our own perception (own_perception.py) seeing someone starts a clip ``pre`` seconds
before they appeared (the video tap keeps the last few seconds); it ends ``post`` seconds
after the last of them has gone, or at ``max_seconds``. The high-resolution stream's
packets go into the MP4 as they came, so nothing is re-encoded (a few percent of a core);
the doorbell's microphone (the audio tap) is encoded as AAC. The MP4 is fragmented: a
clip cut short (Gatekeeper restarting) still plays.

Each person seen gets a picture: the frame of the clip, at full resolution, where the
detector was surest of them.

Files, in ``CLIP_DIR/<day>/``: ``<start>.mp4``, ``<start>.json`` (when, and who: each
person's track, zones and picture) and ``<start>-<n>.jpg``; deleted after
``CLIP_RETENTION_DAYS``. Clips hold visitors' faces and voices.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from gatekeeper.perception import PersonEvent

log = logging.getLogger(__name__)

AUDIO_RATE = 16000  # the audio tap's
AUDIO_CHUNK_SECONDS = 0.5  # audio is written this often...
AUDIO_LAG_SECONDS = 0.5  # ...this far behind the video, so it has arrived
FRAGMENTED = {"movflags": "frag_keyframe+empty_moov+default_base_moof"}


def track_start(event: PersonEvent) -> float:
    """When a track began: own perception's ids start with it (``<time>-own<n>``)."""
    try:
        return float(event.id.split("-", 1)[0])
    except ValueError:
        return time.time()


class Recorder:
    """Clips of the door while someone is there. Fed by own perception: ``on_event`` (its
    person events) and ``on_track`` (each track that ended); ``run_forever`` on its own
    thread writes the clips."""

    def __init__(
        self,
        video,
        audio,
        clip_dir: Path,
        camera: str,
        *,
        pre: float = 3.0,
        post: float = 2.0,
        max_seconds: float = 300.0,
        snapshots: bool = True,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.video = video
        self.audio = audio
        self.clip_dir = clip_dir
        self.camera = camera
        self.pre, self.post, self.max_seconds = pre, post, max_seconds
        self.snapshots = snapshots
        self._clock, self._mono = clock, mono
        self._cond = threading.Condition()
        self._present: dict[str, float] = {}  # track id -> when it began
        self._last_gone = 0.0
        self._ended: list[Any] = []  # tracks that ended since the clip began
        self._last_end = 0.0  # the last clip's end: the next doesn't overlap it
        self._stopping = False
        self._idle = threading.Event()  # not writing a clip
        self._idle.set()
        self.clips = 0

    # -- what own perception sees ------------------------------------------------

    def on_event(self, event: PersonEvent) -> None:
        with self._cond:
            if event.type == "end":
                if self._present.pop(event.id, None) is not None:
                    self._last_gone = self._clock()
            else:
                self._present.setdefault(event.id, track_start(event))
            self._cond.notify_all()

    def on_track(self, track) -> None:
        with self._cond:
            self._ended.append(track)

    def stop(self, wait: float = 3.0) -> None:
        """Finish the clip being written, waiting up to ``wait`` s (Gatekeeper is stopping)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        self._idle.wait(wait)

    # -- writing ---------------------------------------------------------------

    def run_forever(self) -> None:
        while not self._stopping:
            with self._cond:
                while not self._present and not self._stopping:
                    self._cond.wait(1.0)
                if self._stopping:
                    return
                since = max(min(self._present.values()) - self.pre, self._last_end)
            self._idle.clear()
            try:
                self.record(since)
            except Exception:
                log.exception("recording failed")
                time.sleep(5.0)
            finally:
                self._idle.set()

    def _done(self, started: float) -> bool:
        with self._cond:
            if self._stopping:
                return True
            now = self._clock()
            if now - started >= self.max_seconds:
                return True
            return not self._present and now - self._last_gone >= self.post

    def record(self, since: float) -> Path | None:
        """Write one clip, from the keyframe at or before ``since`` until nobody's there
        (or the stream reconnects); its path, or None if no video came."""
        with self._cond:
            self._ended = [t for t in self._ended if t.last >= since]
        started = self._clock()
        out = path = video = sound = first_dts = base = None
        clip: dict[str, Any] = {}
        wall0 = audio_to = 0.0
        audio_pts = 0
        try:
            for packet, stream in self.video.packets(since, lambda: self._done(started)):
                if packet.data.dts is None:
                    continue
                if out is None:
                    if not packet.keyframe:
                        continue
                    path, out, video, sound = self._open(packet, stream)
                    first_dts, base = packet.data.dts, stream.time_base
                    wall0 = packet.at  # the wall-clock time of clip time 0 (refined below)
                    clip = {"start": packet.at, "file": path.name}
                t = float((packet.data.dts - first_dts) * base)
                wall0 = min(wall0, packet.at - t)  # arrivals are only ever late
                out.mux(self._copy(packet.data, first_dts, video))
                clip["end"] = wall0 + t
                if sound is not None:
                    while packet.at - wall0 - AUDIO_LAG_SECONDS >= audio_to + AUDIO_CHUNK_SECONDS:
                        audio_pts = self._audio(out, sound, wall0, audio_to, audio_pts)
                        audio_to += AUDIO_CHUNK_SECONDS
        finally:
            if out is not None:
                if sound is not None:
                    end = clip["end"] - wall0
                    while audio_to < end:
                        audio_pts = self._audio(out, sound, wall0, audio_to, audio_pts)
                        audio_to += AUDIO_CHUNK_SECONDS
                    for p in sound.encode(None):
                        out.mux(p)
                out.close()
                clip["start"] = wall0
                clip["audio"] = sound is not None
                self._last_end = clip["end"]
                self._finish(path, clip)
        return path

    def _open(self, packet, stream):
        import av

        day = datetime.datetime.fromtimestamp(packet.at).strftime("%Y-%m-%d")
        folder = self.clip_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{packet.at:.3f}.mp4"
        out = av.open(str(path), "w", format="mp4", options=FRAGMENTED)
        video = out.add_stream_from_template(stream)
        sound = None
        if self.audio is not None and self.audio.healthy():
            sound = out.add_stream("aac", rate=AUDIO_RATE, layout="mono")
            sound.bit_rate = 32000
        log.info("recording %s", path.name)
        return path, out, video, sound

    @staticmethod
    def _copy(packet, first_dts: int, stream):
        """A copy of ``packet`` (the video tap's packets are shared) for the clip, its
        times counted from the clip's first."""
        import av

        copy = av.Packet(bytes(packet))
        copy.dts = packet.dts - first_dts
        copy.pts = packet.pts - first_dts if packet.pts is not None else copy.dts
        copy.time_base = packet.time_base
        copy.is_keyframe = packet.is_keyframe
        copy.stream = stream
        return copy

    def _audio(self, out, sound, wall0: float, t: float, pts: int) -> int:
        """Encode the microphone's audio for clip seconds [t, t + chunk); silence where
        the audio tap has none, so it stays in step with the video. The next pts."""
        import av

        n = round(AUDIO_CHUNK_SECONDS * AUDIO_RATE)
        to_mono = self._mono() - self._clock()
        start = wall0 + t + to_mono
        samples = self.audio.slice(start, start + AUDIO_CHUNK_SECONDS)[:n]
        if len(samples) < n:
            samples = np.concatenate([samples, np.zeros(n - len(samples), np.int16)])
        frame = av.AudioFrame.from_ndarray(samples[None, :], format="s16", layout="mono")
        frame.sample_rate, frame.pts, frame.time_base = AUDIO_RATE, pts, Fraction(1, AUDIO_RATE)
        for p in sound.encode(frame):
            out.mux(p)
        return pts + n

    def _finish(self, path: Path, clip: dict) -> None:
        """The clip's index, and a picture of each person who was in it."""
        with self._cond:
            ended = [t for t in self._ended if t.last >= clip["start"] and t.first <= clip["end"]]
            stopping = self._stopping
        people = []
        for n, track in enumerate(sorted(ended, key=lambda t: t.first), 1):
            person = {
                "track": track.id,
                "first": round(track.first, 3),
                "last": round(track.last, 3),
                "zones": list(track.entered),
                "best": round(track.best, 2),
                "best_at": round(track.best_at, 3),
            }
            if self.snapshots and not stopping:
                picture = path.with_name(f"{path.stem}-{n}.jpg")
                if snapshot(path, track.best_at - clip["start"], picture):
                    person["picture"] = picture.name
            people.append(person)
        clip.update(camera=self.camera, people=people)
        clip["start"], clip["end"] = round(clip["start"], 3), round(clip["end"], 3)
        path.with_suffix(".json").write_text(json.dumps(clip, indent=1), encoding="utf-8")
        self.clips += 1
        log.info(
            "recorded %s: %.0f s, %d person(s), %.1f MB",
            path.name,
            clip["end"] - clip["start"],
            len(people),
            path.stat().st_size / 1e6,
        )


def snapshot(clip: Path, at: float, picture: Path) -> bool:
    """The clip's frame at ``at`` seconds into it, kept as ``picture``."""
    data = clip_frame(clip, at)
    if data is None:
        return False
    picture.write_bytes(data)
    return True


def clip_frame(clip: Path, at: float) -> bytes | None:
    """The clip's frame at ``at`` seconds into it, as a full-resolution JPEG."""
    import av

    from gatekeeper.face_admin import jpeg_bytes

    try:
        with av.open(str(clip)) as c:
            v = c.streams.video[0]
            c.seek(max(0, int((at - 0.1) / v.time_base)), stream=v, backward=True)
            for frame in c.decode(v):
                if frame.pts is not None and float(frame.pts * v.time_base) >= at:
                    return jpeg_bytes(frame.to_ndarray(format="bgr24"))
    except Exception as e:
        log.warning("couldn't take a picture from %s: %s", clip.name, e)
    return None
