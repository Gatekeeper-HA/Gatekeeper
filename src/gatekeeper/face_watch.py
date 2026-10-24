"""Watching for the household's faces while someone is on camera (``FACE_RECOGNITION``).

While Frigate sees anyone (``SessionTracker.people``), a few frames a second from the
video tap, starting just before they appeared, go through faces.py: faces are found
on a 640x480 copy (cheaper), each is aligned from the full-resolution frame and
matched against the gallery. A face is put down to the person whose Frigate track is
nearest, and reported (``on_face``); the tracker decides what it means.

Kept for the owner to enroll (web.py, face_admin.py), for ``FACE_CANDIDATE_DAYS``: the
faces of someone who went in by the door, came back soon after leaving, or was
recognized, and of whoever walks up while the owner asked to capture faces at the door
(``capture``). Each with a sharper crop from the full frame, for the page. Nobody
else's face is stored.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gatekeeper.faces import align, sharpness

log = logging.getLogger(__name__)

DETECT_WIDTH, DETECT_HEIGHT = 640, 480
MIN_SCORE = 0.85  # detector confidence
MIN_SHARPNESS = 30.0  # of the aligned face (faces.sharpness): blurred ones are skipped
FRAME_EVERY = 0.25  # seconds between frames looked at
LEAD_SECONDS = 1.0  # decode from this long before the first person appeared
IDLE_SECONDS = 2.0  # stop decoding once nobody's been on camera this long
MAX_X_GAP = 0.2  # a face this far (of the frame's width) from everyone's track is no one's
KEEP_PER_EVENT = 8  # candidate faces kept per person event: the widest
FORGET_SECONDS = 600  # an event's faces are dropped from memory after this long
CAPTURE_SECONDS = 120.0  # how long "capture at the door" lasts
DISPLAY_MARGIN = 1.6  # the page's picture: the face box, this much wider...
DISPLAY_MAX = 320  # ...at most this many pixels a side

# (event id, when Frigate first reported them, where they are now: x of their track
# as a fraction of the width, or None)
People = Callable[[], list[tuple[str, float, float | None]]]


@dataclass
class Seen:
    """A face, put down to a person event, and who it is (``name``: None unless the
    gallery's best match ``score`` reached the threshold)."""

    at: float
    event_id: str
    name: str | None
    score: float
    width: float
    embedding: np.ndarray
    aligned: np.ndarray
    display: bytes = b""  # a JPEG of the face from the full frame, for the page


class FaceWatcher:
    def __init__(
        self,
        tap,
        *,
        detector,
        embedder,
        gallery,
        people: People,
        on_face: Callable[[Seen], None],
        threshold: float,
        min_width: float,
        candidate_dir: Path,
        candidate_days: float,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        encode: Callable[[np.ndarray], bytes] | None = None,
    ) -> None:
        self._tap = tap
        self._detector = detector
        self._embedder = embedder
        self.gallery = gallery
        self._people = people
        self._on_face = on_face
        self.threshold = threshold
        self.min_width = min_width
        self.candidate_dir = candidate_dir
        self.candidate_days = candidate_days
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._faces: dict[str, list[Seen]] = {}
        self._marked: dict[str, str] = {}  # events to keep once the watch ends: outcome
        self._watching = False
        self._capture_until = 0.0
        if encode is None:
            from gatekeeper.face_admin import jpeg_bytes as encode
        self._encode = encode

    def set_models(self, detector, embedder) -> None:
        """The models, once they're loaded (they download on first use)."""
        self._detector, self._embedder = detector, embedder

    def run_forever(self) -> None:
        self.prune()
        while True:
            try:
                if not self._watch_once():
                    self._sleep(0.2)
            except Exception:
                log.exception("face watch failed")
                self._sleep(1.0)

    def _watch_once(self) -> bool:
        """Look at frames while anyone's on camera. False if nobody is."""
        people = self._people()
        if not people:
            return False
        since = min(created for _, created, _ in people) - LEAD_SECONDS
        self.gallery.reload()
        idle = [None]

        def stop() -> bool:
            if self._people():
                idle[0] = None
                return False
            now = self._clock()
            idle[0] = idle[0] or now
            return now - idle[0] > IDLE_SECONDS

        started, frames, seen = self._clock(), 0, []
        with self._lock:
            self._watching = True
        try:
            for at, frame in self._tap.frames(since, FRAME_EVERY, stop):
                frames += 1
                seen += self.look(at, frame)
        finally:
            with self._lock:
                self._watching = False
                marked, self._marked = self._marked, {}
            for event_id, outcome in marked.items():
                self._save(event_id, outcome)
        matched = Counter(s.name for s in seen if s.name)
        best = max((s.score for s in seen), default=0.0)
        log.info(
            "face watch: %.0f s, %d frame(s), %d face(s), best match %.2f%s",
            self._clock() - started,
            frames,
            len(seen),
            best,
            "".join(f"; {name} x{n}" for name, n in matched.items()),
        )
        self.prune()
        return True

    def look(self, at: float, frame) -> list[Seen]:
        """The faces in one frame (an av.VideoFrame), matched and reported."""
        small = frame.to_ndarray(width=DETECT_WIDTH, height=DETECT_HEIGHT, format="bgr24")
        sx, sy = frame.width / DETECT_WIDTH, frame.height / DETECT_HEIGHT
        found = [f.scaled(sx, sy) for f in self._detector.detect(small) if f.score >= MIN_SCORE]
        found = [f for f in found if f.width >= self.min_width]
        if not found:
            return []
        full = frame.to_ndarray(format="bgr24")
        people = self._people()
        capturing = self.capturing() > 0
        out = []
        for face in found:
            event_id = owner(face.center[0] / frame.width, people)
            if event_id is None:
                continue
            aligned = align(full, face)
            if sharpness(aligned) < MIN_SHARPNESS:
                continue
            emb = self._embedder.embed(aligned)
            name, score = self.gallery.match(emb)
            seen = Seen(
                at,
                event_id,
                name if score >= self.threshold else None,
                score,
                face.width,
                emb,
                aligned,
                self._display(full, face),
            )
            self._remember(seen)
            if capturing:
                with self._lock:
                    self._marked.setdefault(event_id, "capture")
            try:
                self._on_face(seen)
            except Exception:
                log.exception("face callback failed")
            out.append(seen)
        return out

    def _remember(self, seen: Seen) -> None:
        with self._lock:
            kept = self._faces.setdefault(seen.event_id, [])
            kept.append(seen)
            kept.sort(key=lambda s: -s.width)
            del kept[KEEP_PER_EVENT:]

    def _display(self, full: np.ndarray, face) -> bytes:
        """A JPEG of the face and a little around it, from the full frame."""
        try:
            return self._encode(display_crop(full, face))
        except Exception as e:
            log.debug("no picture of the face: %s", e)
            return b""

    def capture(self, seconds: float = CAPTURE_SECONDS) -> None:
        """Keep the faces of whoever walks up in the next ``seconds`` (the owner asked,
        to enroll someone the camera hasn't caught going inside)."""
        self._capture_until = self._clock() + seconds
        log.info("capturing faces at the door for %.0f s", seconds)

    def capturing(self) -> float:
        """Seconds of capturing left (0: not capturing)."""
        return max(0.0, self._capture_until - self._clock())

    def keep(self, event_id: str, outcome: str) -> Path | None:
        """Keep the faces of person event ``event_id`` as candidates for enrollment
        (they went in by the door, came back, or were recognized). While someone is
        still on camera that's once the watch ends, so faces seen after the decision
        count too (returns None); otherwise now. None if there were no faces."""
        with self._lock:
            if self._watching:
                self._marked[event_id] = outcome
                return None
        return self._save(event_id, outcome)

    def _save(self, event_id: str, outcome: str) -> Path | None:
        with self._lock:
            faces = self._faces.pop(event_id, [])
        if not faces:
            return None
        self.candidate_dir.mkdir(parents=True, exist_ok=True)
        path = self.candidate_dir / f"{event_id}.npz"
        with path.open("wb") as f:
            np.savez_compressed(
                f,
                embeddings=np.array([s.embedding for s in faces], dtype=np.float32),
                faces=np.array([s.aligned for s in faces], dtype=np.uint8),
                at=np.array([s.at for s in faces]),
                outcome=np.array(outcome),
                names=np.array([s.name or "" for s in faces]),
                display=np.frombuffer(b"".join(s.display for s in faces), dtype=np.uint8),
                display_ends=np.cumsum([len(s.display) for s in faces]),
            )
        path.chmod(0o600)
        log.info("kept %d face(s) of %s for enrollment", len(faces), event_id)
        return path

    def prune(self) -> None:
        """Forget faces in memory after FORGET_SECONDS, and candidates after
        ``candidate_days``."""
        now = self._clock()
        with self._lock:
            for event_id in [e for e, f in self._faces.items() if now - f[0].at > FORGET_SECONDS]:
                del self._faces[event_id]
        if self.candidate_days and self.candidate_dir.exists():
            for path in self.candidate_dir.glob("*.npz"):
                if now - path.stat().st_mtime > self.candidate_days * 86400:
                    path.unlink(missing_ok=True)


def display_crop(full: np.ndarray, face) -> np.ndarray:
    """The face box made DISPLAY_MARGIN wider, square, inside the frame, at most
    DISPLAY_MAX a side (every other pixel, or every third, if bigger), even-sized."""
    h, w = full.shape[:2]
    cx, cy = face.center
    side = min(int(DISPLAY_MARGIN * max(face.box[2], face.box[3])), h, w)
    x0 = int(min(max(cx - side / 2, 0), w - side))
    y0 = int(min(max(cy - side / 2, 0), h - side))
    crop = full[y0 : y0 + side, x0 : x0 + side]
    step = -(-side // DISPLAY_MAX)
    crop = crop[::step, ::step]
    even = crop.shape[0] // 2 * 2
    return np.ascontiguousarray(crop[:even, :even])


def owner(x: float, people: list[tuple[str, float, float | None]]) -> str | None:
    """Whose face is at ``x`` (fraction of the width): the only person on camera, or
    the one whose track is nearest, if within MAX_X_GAP."""
    if len(people) == 1:
        return people[0][0]
    near = [
        (abs(px - x), eid) for eid, _, px in people if px is not None and abs(px - x) <= MAX_X_GAP
    ]
    return min(near)[1] if near else None
