"""Gatekeeper's own perception (step 2 of the Frigate decision), in shadow mode first.

It reads the camera's low-resolution stream itself, finds people with YOLOX (Apache-2.0;
chosen by testing on the rig's footage, see the "Choosing Gatekeeper's person detector"
decision doc), tracks them, and puts each track in the zones it walks through. The result
is the same ``PersonEvent`` stream Frigate gives (perception.py), so it can take
Frigate's place on our hub.

To save work, which a small hub needs, a cheap motion check looks at each frame first,
and the detector only runs on those where something moved, or while someone is tracked.

``OWN_PERCEPTION=shadow``: it runs next to Frigate and only logs each track
(``own perception: person track …``); the daily report compares them with Frigate's
arrivals. Frigate still drives the visits.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from gatekeeper.faces import download
from gatekeeper.perception import PersonEvent, Point, inside
from gatekeeper.video_tap import open_rtsp

log = logging.getLogger(__name__)

# YOLOX-tiny (Megvii, Apache-2.0), the official ONNX export, pinned and checked.
YOLOX_TINY = (
    "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx",
    "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
)
NMS_IOU = 0.5
# On an NVIDIA GPU: a cheap first choice of convolution algorithms (the exhaustive search
# takes seconds at startup for a model this small), and a cap on the memory it takes from
# Frigate and Whisper.
CUDA_OPTIONS = {
    "cudnn_conv_algo_search": "HEURISTIC",
    "arena_extend_strategy": "kSameAsRequested",
    "gpu_mem_limit": str(512 * 1024 * 1024),
}
MOTION_SIZE = (160, 120)  # the motion check works on a small grey copy
MOTION_LEVEL = 25  # grey levels a pixel must change by...
MOTION_SHARE = 0.002  # ...on this share of the frame: something moved
MOTION_LEARN = 0.05  # how fast the background follows the scene (lighting, shadows)
MAX_BACKOFF_SECONDS = 30.0
STATS_SECONDS = 3600.0  # how often it logs how much it looked (the daily report reads it)


def fetch_model(model: tuple[str, str], cache: Path) -> Path:
    """The model file (URL, SHA-256), downloaded into ``cache`` the first time and checked."""
    url, sha256 = model
    return download(url, sha256, cache)


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the frame
    score: float

    @property
    def foot(self) -> Point:
        """The bottom centre of the box: where the person stands (as Frigate's paths)."""
        return ((self.box[0] + self.box[2]) / 2, self.box[3])


class Yolox:
    """YOLOX on onnxruntime: people in a BGR frame. The official export takes the image
    letterboxed into a square (top-left, padded with grey 114), BGR 0-255, and gives a
    grid of boxes per stride, with objectness and class scores already through a sigmoid.

    ``device``: "cuda" or "auto" use an NVIDIA GPU when onnxruntime has CUDA (the GPU
    image) and it works; ``self.device`` is where it runs."""

    def __init__(
        self, model: Path, threshold: float = 0.25, threads: int = 2, device: str = "cpu"
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3
        providers: list = ["CPUExecutionProvider"]
        if device != "cpu" and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, ("CUDAExecutionProvider", CUDA_OPTIONS))
        self._session = ort.InferenceSession(str(model), options, providers=providers)
        on_gpu = self._session.get_providers()[0] == "CUDAExecutionProvider"
        self.device = "cuda" if on_gpu else "cpu"
        if device == "cuda" and not on_gpu:
            log.warning("own perception: the detector couldn't run on the GPU; it runs on the CPU")
        self.size = self._session.get_inputs()[0].shape[2]
        self.threshold = threshold
        self._grid, self._stride = grid(self.size)

    def fit(self, width: int, height: int) -> tuple[int, int]:
        """The size a ``width`` x ``height`` frame is scaled to before it's padded."""
        r = min(self.size / height, self.size / width)
        return int(width * r), int(height * r)

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        h, w = bgr.shape[:2]
        image, r = letterbox(bgr, self.size)
        out = self._session.run(None, {"images": image[None]})[0][0]
        return decode(out, self._grid, self._stride, r, w, h, self.threshold)


def grid(size: int) -> tuple[np.ndarray, np.ndarray]:
    """Each output row's grid cell and stride (8, 16, 32) for a ``size`` square input."""
    grids, strides = [], []
    for s in (8, 16, 32):
        n = size // s
        yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        grids.append(np.stack([xv, yv], 2).reshape(-1, 2))
        strides.append(np.full((n * n, 1), s))
    return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)


def letterbox(bgr: np.ndarray, size: int) -> tuple[np.ndarray, float]:
    """The frame scaled to fit a ``size`` square, top-left, padded with 114; as CHW float."""
    h, w = bgr.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(h * r), int(w * r)
    ys = (np.arange(nh) / r).astype(int).clip(0, h - 1)
    xs = (np.arange(nw) / r).astype(int).clip(0, w - 1)
    out = np.full((size, size, 3), 114, np.float32)
    out[:nh, :nw] = bgr[ys][:, xs]
    return out.transpose(2, 0, 1), r


def decode(
    out: np.ndarray,
    grid_xy: np.ndarray,
    stride: np.ndarray,
    r: float,
    w: int,
    h: int,
    threshold: float,
) -> list[Detection]:
    """YOLOX's raw rows (cx, cy, w, h, objectness, 80 class scores) as people, after NMS."""
    scores = out[:, 4] * out[:, 5]  # objectness x person (COCO class 0)
    keep = np.nonzero(scores >= threshold)[0]
    if not len(keep):
        return []
    xy = (out[keep, :2] + grid_xy[keep]) * stride[keep]
    wh = np.exp(out[keep, 2:4]) * stride[keep]
    boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / r / np.array([w, h, w, h])
    boxes = boxes.clip(0, 1)
    return [
        Detection(tuple(float(v) for v in boxes[i]), float(scores[keep][i]))
        for i in nms(boxes, scores[keep], NMS_IOU)
    ]


def iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU of each box in ``a`` (n x 4) with each in ``b`` (m x 4): n x m."""
    x0 = np.maximum(a[:, None, 0], b[None, :, 0])
    y0 = np.maximum(a[:, None, 1], b[None, :, 1])
    x1 = np.minimum(a[:, None, 2], b[None, :, 2])
    y1 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x1 - x0, 0, None) * np.clip(y1 - y0, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-9)


def centres(boxes: np.ndarray) -> np.ndarray:
    return np.stack([(boxes[:, 0] + boxes[:, 2]) / 2, (boxes[:, 1] + boxes[:, 3]) / 2], 1)


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    order = list(np.argsort(-scores))
    kept = []
    while order:
        i = order.pop(0)
        kept.append(int(i))
        if order:
            overlap = iou(boxes[[i]], boxes[order])[0]
            order = [j for j, o in zip(order, overlap, strict=True) if o < threshold]
    return kept


class Motion:
    """Did anything move? A running grey background, compared with each new frame."""

    def __init__(self) -> None:
        self._background: np.ndarray | None = None

    def update(self, bgr: np.ndarray) -> bool:
        h, w = bgr.shape[:2]
        gw, gh = MOTION_SIZE
        ys = (np.arange(gh) * h / gh).astype(int)
        xs = (np.arange(gw) * w / gw).astype(int)
        grey = bgr[ys][:, xs].astype(np.float32).mean(axis=2)
        if self._background is None:
            self._background = grey
            return False
        changed = np.abs(grey - self._background) > MOTION_LEVEL
        self._background += MOTION_LEARN * (grey - self._background)
        return bool(changed.mean() >= MOTION_SHARE)


@dataclass
class Track:
    id: str
    box: tuple[float, float, float, float]
    first: float  # time.time() first seen
    last: float  # last matched
    hits: int = 1
    confirmed: bool = False
    best: float = 0.0  # best detection score
    path: list[Point] = field(default_factory=list)
    entered: list[str] = field(default_factory=list)  # zones, in the order entered
    current: tuple[str, ...] = ()
    inside: dict[str, float] = field(default_factory=dict)  # in each zone since (in a row)


class Tracker:
    """People across frames, as tracks: a detection joins the track whose last box it
    overlaps most (IoU), or else the nearest one close enough. A track needs ``min_hits``
    detections before it's reported (one stray box isn't a person: unless it's seen again
    within ``max_age_unconfirmed`` it's dropped), and ends after ``max_age`` seconds
    without one (Frigate's default: 25 frames at 5 fps). A track is
    in a zone once its feet have been in it for ``zone_seconds``, frame after frame
    (Frigate's "inertia", 3 frames at 5 fps; in seconds, so it's the same at any frame
    rate): someone close to the camera is cut off at the bottom of the frame, and for a
    moment seems to stand in front of the door. Each track gives person events as
    Frigate's do: "new", "update" (it moved, or changed zones), "end"."""

    def __init__(
        self,
        camera: str,
        zones: Callable[[], dict[str, list[Point] | None]] = dict,
        *,
        min_iou: float = 0.2,
        min_hits: int = 2,
        max_age: float = 5.0,
        max_age_unconfirmed: float = 1.0,
        min_move: float = 0.02,
        zone_seconds: float = 0.3,
    ) -> None:
        self.camera = camera
        self._zones = zones
        self.min_iou = min_iou
        self.min_hits = min_hits
        self.max_age = max_age
        self.max_age_unconfirmed = max_age_unconfirmed
        self.min_move = min_move
        self.zone_seconds = zone_seconds
        self.tracks: list[Track] = []
        self.ended: list[Track] = []  # confirmed tracks that ended, for the caller
        self._n = 0

    @property
    def active(self) -> bool:
        return bool(self.tracks)

    def update(self, detections: list[Detection], now: float) -> list[PersonEvent]:
        events: list[PersonEvent] = []
        unmatched = set(range(len(detections)))
        for i, j in self._match(detections):
            unmatched.discard(j)
            events += self._extend(self.tracks[i], detections[j], now)
        for j in sorted(unmatched):
            self._n += 1
            d = detections[j]
            track = Track(f"{now:.6f}-own{self._n}", d.box, now, now, best=d.score, path=[d.foot])
            self._zone(track, now)
            self.tracks.append(track)
            if self.min_hits <= 1:
                track.confirmed = True
                events.append(self._event("new", track))
        for track in [t for t in self.tracks if now - t.last > self._max_age(t)]:
            self.tracks.remove(track)
            if track.confirmed:
                events.append(self._event("end", track))
                self.ended.append(track)
        return events

    def end_all(self) -> list[PersonEvent]:
        """End every track now (Gatekeeper is stopping); those confirmed are reported."""
        events = []
        for track in self.tracks:
            if track.confirmed:
                events.append(self._event("end", track))
                self.ended.append(track)
        self.tracks = []
        return events

    def _max_age(self, track: Track) -> float:
        return self.max_age if track.confirmed else self.max_age_unconfirmed

    def _match(self, detections: list[Detection]) -> list[tuple[int, int]]:
        """(track, detection) pairs: by overlap first; then, for someone who moved more
        than their box between two frames (walking fast, a few frames a second), the
        nearest detection within half their height."""
        if not self.tracks or not detections:
            return []
        tracks = np.array([t.box for t in self.tracks])
        boxes = np.array([d.box for d in detections])
        overlap = iou(tracks, boxes)
        distance = np.linalg.norm(centres(tracks)[:, None] - centres(boxes)[None], axis=2)
        reach = (tracks[:, 3] - tracks[:, 1])[:, None] / 2
        pairs: list[tuple[int, int]] = []
        for cost, ok in ((-overlap, overlap >= self.min_iou), (distance, distance < reach)):
            for i, j in sorted(zip(*np.nonzero(ok), strict=True), key=lambda ij: cost[ij]):
                if all(i != a and j != b for a, b in pairs):
                    pairs.append((int(i), int(j)))
        return pairs

    def _extend(self, track: Track, d: Detection, now: float) -> list[PersonEvent]:
        track.box, track.last, track.hits = d.box, now, track.hits + 1
        track.best = max(track.best, d.score)
        moved = abs(d.foot[0] - track.path[-1][0]) + abs(d.foot[1] - track.path[-1][1])
        if moved >= self.min_move:
            track.path.append(d.foot)
        zones_before = (tuple(track.entered), track.current)
        self._zone(track, now)
        if not track.confirmed:
            if track.hits >= self.min_hits:
                track.confirmed = True
                return [self._event("new", track)]
            return []
        if moved >= self.min_move or (tuple(track.entered), track.current) != zones_before:
            return [self._event("update", track)]
        return []

    def _zone(self, track: Track, now: float) -> None:
        foot = ((track.box[0] + track.box[2]) / 2, track.box[3])
        for name, polygon in self._zones().items():
            if polygon and inside(polygon, foot):
                track.inside.setdefault(name, now)
            else:
                track.inside.pop(name, None)
        current = tuple(n for n, since in track.inside.items() if now - since >= self.zone_seconds)
        track.current = current
        track.entered += [z for z in current if z not in track.entered]

    def _event(self, kind: str, t: Track) -> PersonEvent:
        return PersonEvent(
            kind, t.id, self.camera, "person", tuple(t.entered), t.current, tuple(t.path)
        )


def summary(track: Track) -> str:
    """The shadow log's line for a track that ended (the daily report reads it)."""
    zones = ",".join(track.entered) or "-"
    (sx, sy), (ex, ey) = track.path[0], track.path[-1]
    return (
        f"own perception: person track {track.id} start={track.first:.1f} end={track.last:.1f} "
        f"zones={zones} path=({sx:.2f},{sy:.2f})->({ex:.2f},{ey:.2f}) best={track.best:.2f} "
        f"hits={track.hits}"
    )


class OwnPerception:
    """The low-resolution stream through the motion check, the detector and the tracker.

    Two threads: ``read_forever`` decodes the stream and keeps only its newest frame;
    ``look_forever`` takes the newest frame a few times a second. On a slow CPU frames
    are skipped rather than queued, so what it sees stays live and the times right.
    ``on_event`` gets each person event, ``on_track`` each track that ended."""

    def __init__(
        self,
        rtsp_url: str,
        detector,
        tracker: Tracker,
        *,
        fps: float = 5.0,
        on_event: Callable[[PersonEvent], None] = lambda event: None,
        on_track: Callable[[Track], None] = lambda track: log.info(summary(track)),
        opener: Callable[[str], Any] = open_rtsp,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.detector = detector
        self.tracker = tracker
        self.every = 1.0 / fps
        self._on_event = on_event
        self._on_track = on_track
        self._opener = opener
        self._clock = clock
        self._sleep = sleep
        self._motion = Motion()
        self._cond = threading.Condition()
        self._frame: tuple[float, Any] | None = None  # the newest (time, av.VideoFrame)
        self._looking = threading.Lock()  # the tracker: one frame (or the stop) at a time
        self._stopping = False
        self.frames = self.detected = 0  # frames looked at; of those, through the detector
        self._detect_seconds: list[float] = []
        self._stats_from: float | None = None

    def start(self, start_thread: Callable[..., None]) -> None:
        start_thread("own-perception-read", self.read_forever)
        start_thread("own-perception", self.look_forever)

    def stop(self) -> None:
        """Stop, and report the tracks still open: someone at the door while Gatekeeper
        restarts was still seen."""
        self._stopping = True
        with self._cond:
            self._cond.notify_all()
        if self._looking.acquire(timeout=2.0):
            try:
                self._handle(self.tracker.end_all())
            finally:
                self._looking.release()

    # -- reading ---------------------------------------------------------------

    def read_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            started = self._clock()
            try:
                self._read_once()
            except Exception as e:
                log.warning("own perception: %s", e)
            if self._stopping:
                break
            if self._clock() - started > 60:
                backoff = 1.0
            log.warning("own perception: stream ended; reconnecting in %.0f s", backoff)
            self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _read_once(self) -> None:
        container = self._opener(self.rtsp_url)
        try:
            container.streams.video[0].codec_context.thread_type = "AUTO"
            log.info("own perception reading %s", self.rtsp_url.split("@")[-1])
            for frame in container.decode(video=0):
                if self._stopping:
                    break
                with self._cond:
                    self._frame = (self._clock(), frame)
                    self._cond.notify_all()
        finally:
            container.close()

    # -- looking ---------------------------------------------------------------

    def look_forever(self) -> None:
        last = 0.0
        while not self._stopping:
            with self._cond:
                if self._frame is None or self._frame[0] <= last:
                    self._cond.wait(1.0)
                fresh = self._frame if self._frame and self._frame[0] > last else None
            self.log_stats(self._clock())
            if fresh is None:
                self.end_stale()
                continue
            at, frame = fresh
            last, picked = at, self._clock()
            try:
                # Scaled by the decoder's scaler to what the detector takes.
                width, height = self.detector.fit(frame.width, frame.height)
                bgr = frame.to_ndarray(
                    width=width, height=height, format="bgr24", interpolation="BILINEAR"
                )
                self.look(bgr, at)
            except Exception:
                log.exception("own perception: couldn't look at a frame")
            wait = picked + self.every - self._clock()
            if wait > 0:
                self._sleep(wait)

    def look(self, bgr: np.ndarray, now: float) -> list[PersonEvent]:
        """One frame: the motion check; the detector if something moved or someone is
        being tracked."""
        with self._looking:
            self.frames += 1
            moved = self._motion.update(bgr)
            if not moved and not self.tracker.active:
                return []
            self.detected += 1
            started = time.perf_counter()
            found = self.detector.detect(bgr)
            self._detect_seconds.append(time.perf_counter() - started)
            return self._handle(self.tracker.update(found, now))

    def log_stats(self, now: float) -> None:
        """Every ``STATS_SECONDS``: how many frames it looked at, how many went through the
        detector, and how long that took."""
        if self._stats_from is None:
            self._stats_from = now
        if now - self._stats_from < STATS_SECONDS:
            return
        ms = 1000 * float(np.median(self._detect_seconds)) if self._detect_seconds else 0.0
        log.info(
            "own perception stats: %d frames in %.0f s, %d through the detector "
            "(median %.0f ms on %s), %d track(s) now",
            self.frames,
            now - self._stats_from,
            self.detected,
            ms,
            getattr(self.detector, "device", "cpu"),
            len(self.tracker.tracks),
        )
        self.frames = self.detected = 0
        self._detect_seconds.clear()
        self._stats_from = now

    def end_stale(self) -> list[PersonEvent]:
        """End the tracks of people not seen for a while when no frames come (the
        stream dropped)."""
        with self._looking:
            if not self.tracker.active:
                return []
            return self._handle(self.tracker.update([], self._clock()))

    def _handle(self, events: list[PersonEvent]) -> list[PersonEvent]:
        for event in events:
            try:
                self._on_event(event)
            except Exception:
                log.exception("own perception: event callback failed")
        for track in self.tracker.ended:
            try:
                self._on_track(track)
            except Exception:
                log.exception("own perception: track callback failed")
        self.tracker.ended.clear()
        return events
