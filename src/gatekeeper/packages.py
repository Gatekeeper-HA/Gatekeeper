"""Package alerts: a package left at the door, and picked up (Phase 1).

A package only appears or goes when someone comes to the door, so the doorstep
isn't watched all the time; it's compared before and after each visit:

1. While nobody is on the porch, ``PackageWatcher`` keeps a recent frame from
   Frigate.
2. Once whoever came has been gone ``settle_seconds``, it takes another and
   compares the package zone (PACKAGE_ZONE, a Frigate zone) in the two
   (``compare``): grid cells whose brightness changed, after evening out the
   camera's exposure, grouped into patches. A change across the whole frame (a
   light switched on, the camera's night mode) isn't judged.
3. A small open image model (CLIP, on the CPU, ``PackageModel``) scores each
   patch as a package or not, before and after. A patch that looks like a
   package now, and didn't before, is a new package: "Package delivered".
4. A package whose patch changed, and no longer looks like one, after someone
   came and went: "Package picked up".

Each check is logged with its scores, for tuning. The day's numbers are in the
daily report.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from gatekeeper.frigate import Point, inside

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x0, y0, x1, y1, in pixels

# Comparing the doorstep (on Frigate's 640x480 frames).
CELL = 16  # pixels; the grid compared
MIN_CELLS = 4  # a patch smaller than this is noise (leaves, a moth)
CHANGE_LEVEL = 20.0  # gray levels a cell must change by...
CHANGE_RELATIVE = 4.0  # ...and this many times the zone's typical change
MAX_EXPOSURE_RATIO = 1.33  # beyond it (night mode, a light): not judged
MAX_SCENE_CHANGE = 10.0  # typical change outside the zone (gray levels): not judged
# Judging a patch (CLIP's package probability, 0-1). On 23 doorstep changes after
# visits on the rig with no package, the highest score was 0.37 and the highest
# rise 0.22 (a person still at the door).
DELIVERED_SCORE = 0.45  # looks like a package now...
DELIVERED_RISE = 0.25  # ...and that much more than before
GONE_SHARE = 0.4  # a package's patch with this share of its cells changed...
GONE_SCORE = 0.3  # ...that scores under this is gone


@dataclass
class Shot:
    """A frame: RGB pixels, and the JPEG they came from (to attach to a notification)."""

    rgb: np.ndarray
    jpeg: bytes


@dataclass
class Package:
    box: Box
    since: float  # when the visit that left it started
    score: float


@dataclass
class Comparison:
    judged: bool
    reason: str = ""  # why not judged
    patches: list[Box] | None = None
    changed: np.ndarray | None = None  # changed cells (rows x cols)


def decode_jpeg(data: bytes, width: int = 640, height: int = 480) -> np.ndarray | None:
    """RGB pixels of a JPEG, scaled to ``width`` x ``height`` (PyAV decodes it)."""
    import io

    import av

    try:
        with av.open(io.BytesIO(data)) as container:
            frame = next(container.decode(video=0))
            return frame.reformat(width, height, format="rgb24").to_ndarray()
    except Exception as e:
        log.warning("couldn't decode a frame: %s", e)
        return None


def frigate_frame(api: str, camera: str, timeout: float = 10.0) -> Shot | None:
    """Frigate's latest frame of ``camera`` (its detect stream, 640x480 on the rig)."""
    url = f"{api.rstrip('/')}/api/{urllib.parse.quote(camera)}/latest.jpg?h=480&quality=90"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
    except Exception as e:
        log.warning("couldn't get a frame from Frigate: %s", e)
        return None
    rgb = decode_jpeg(data)
    return None if rgb is None else Shot(rgb, data)


def person_snapshot(api: str, camera: str, since: float, timeout: float = 10.0) -> bytes | None:
    """Frigate's snapshot of the first person seen on ``camera`` since ``since``: who
    came for a package."""
    base = api.rstrip("/")
    query = urllib.parse.urlencode(
        {"camera": camera, "label": "person", "after": f"{since - 10:.0f}", "limit": 20}
    )
    try:
        with urllib.request.urlopen(f"{base}/api/events?{query}", timeout=timeout) as resp:
            events = json.load(resp)
        if not events:
            return None
        first = min(events, key=lambda e: e["start_time"])
        url = f"{base}/api/events/{urllib.parse.quote(first['id'])}/snapshot.jpg?quality=85"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.warning("no snapshot of who picked the package up: %s", e)
        return None


def _gray(rgb: np.ndarray) -> np.ndarray:
    return rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)


def _blur(a: np.ndarray, k: int = 5) -> np.ndarray:
    """Box blur (summed-area table): takes the edge off sensor noise."""
    pad = k // 2
    c = np.pad(np.pad(a, pad, mode="edge").cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _cells(a: np.ndarray) -> np.ndarray:
    h, w = a.shape[0] // CELL * CELL, a.shape[1] // CELL * CELL
    return a[:h, :w].reshape(h // CELL, CELL, w // CELL, CELL).mean(axis=(1, 3))


def zone_cells(polygon: Sequence[Point], height: int, width: int) -> np.ndarray:
    """Which grid cells have their centre in ``polygon`` (fractions of the frame)."""
    rows, cols = height // CELL, width // CELL
    return np.array(
        [
            [
                inside(polygon, ((c + 0.5) * CELL / width, (r + 0.5) * CELL / height))
                for c in range(cols)
            ]
            for r in range(rows)
        ]
    )


def _patches(changed: np.ndarray) -> list[Box]:
    """Boxes around groups of neighbouring changed cells, biggest first."""
    seen = np.zeros_like(changed)
    found = []
    for r, c in zip(*np.nonzero(changed), strict=True):
        if seen[r, c]:
            continue
        seen[r, c] = True
        stack, group = [(r, c)], []
        while stack:
            y, x = stack.pop()
            group.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                inside_grid = 0 <= ny < changed.shape[0] and 0 <= nx < changed.shape[1]
                if inside_grid and changed[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        if len(group) >= MIN_CELLS:
            ys, xs = [int(g[0]) for g in group], [int(g[1]) for g in group]
            box = (min(xs) * CELL, min(ys) * CELL, (max(xs) + 1) * CELL, (max(ys) + 1) * CELL)
            found.append((len(group), box))
    return [box for _, box in sorted(found, reverse=True)]


def compare(before: np.ndarray, after: np.ndarray, zone: np.ndarray) -> Comparison:
    """What changed in the zone's cells between two RGB frames of the same size."""
    a, b = _cells(_blur(_gray(before))), _cells(_blur(_gray(after)))
    exposure = float(np.median(a[zone])) / max(float(np.median(b[zone])), 1.0)
    if not 1 / MAX_EXPOSURE_RATIO <= exposure <= MAX_EXPOSURE_RATIO:
        return Comparison(False, f"exposure changed {exposure:.2f}x")
    diff = np.abs(a - b * exposure)
    if (~zone).any():
        scene = float(np.median(diff[~zone]))
        if scene > MAX_SCENE_CHANGE:
            return Comparison(False, f"the whole scene changed ({scene:.0f})")
    typical = float(np.median(diff[zone]))
    changed = zone & (diff > max(CHANGE_LEVEL, CHANGE_RELATIVE * typical))
    return Comparison(True, patches=_patches(changed), changed=changed)


def changed_share(changed: np.ndarray, box: Box) -> float:
    """The share of ``box``'s cells that changed."""
    x0, y0, x1, y1 = box
    cells = changed[y0 // CELL : -(-y1 // CELL), x0 // CELL : -(-x1 // CELL)]
    return float(cells.mean()) if cells.size else 0.0


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def square_crop(rgb: np.ndarray, box: Box, margin: float = 0.5, min_side: int = 96) -> np.ndarray:
    """A square around ``box`` with some of its surroundings, inside the frame."""
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    side = int(min(max(max(x1 - x0, y1 - y0) * (1 + margin), min_side), h, w))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = int(min(max(cx - side / 2, 0), w - side))
    top = int(min(max(cy - side / 2, 0), h - side))
    return rgb[top : top + side, left : left + side]


class PackageModel:
    """How much a patch of the doorstep looks like a package: CLIP ViT-B/32 (OpenAI,
    MIT license; quantized ONNX from Hugging Face, ~150 MB, downloaded once),
    comparing the patch with descriptions of packages and of other things on a
    porch. About 0.2 s per patch on the rig's CPU."""

    REPO = "Xenova/clip-vit-base-patch32"
    REVISION = "d15189d7028b43f1d3e65039190477f6af591c2a"
    PACKAGE = (
        "a photo of a cardboard box",
        "a photo of a brown shipping box",
        "a photo of a padded mailer envelope",
        "a photo of a plastic mailer bag",
        "a photo of a shopping bag",
    )
    OTHER = (
        "a photo of an empty concrete floor",
        "a photo of a doormat",
        "a photo of a door",
        "a photo of a shadow on the ground",
        "a photo of a brick wall",
        "a photo of a bush",
        "a photo of a dog",
        "a photo of a person",
        "a photo of dry leaves",
        "a photo of gravel",
        "a dark photo at night",
    )
    MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

    def __init__(self) -> None:
        self._vision = None
        self._text: np.ndarray | None = None

    def load(self) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        def fetch(name: str) -> str:
            return hf_hub_download(self.REPO, name, revision=self.REVISION)

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2  # leave the rest of the CPU to Frigate
        vision = ort.InferenceSession(fetch("onnx/vision_model_quantized.onnx"), options)
        text = ort.InferenceSession(fetch("onnx/text_model_quantized.onnx"), options)
        tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))
        encoded = tokenizer.encode_batch([*self.PACKAGE, *self.OTHER])
        n = max(len(e.ids) for e in encoded)
        ids = np.array([e.ids + [0] * (n - len(e.ids)) for e in encoded], dtype=np.int64)
        embeds = text.run(["text_embeds"], {"input_ids": ids})[0]
        self._text = embeds / np.linalg.norm(embeds, axis=1, keepdims=True)
        self._vision = vision
        log.info("package model loaded (%s)", self.REPO)

    @staticmethod
    def _resize(crop: np.ndarray) -> np.ndarray:
        import av

        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(crop), format="rgb24")
        return frame.reformat(224, 224, format="rgb24", interpolation="BICUBIC").to_ndarray()

    def score(self, rgb: np.ndarray, box: Box) -> float:
        """The probability that ``box`` in ``rgb`` shows a package, 0-1."""
        if self._vision is None:
            self.load()
        x = self._resize(square_crop(rgb, box))
        x = ((x.astype(np.float32) / 255 - self.MEAN) / self.STD).transpose(2, 0, 1)[None]
        image = self._vision.run(["image_embeds"], {"pixel_values": x})[0][0]
        logits = 100 * self._text @ (image / np.linalg.norm(image))
        p = np.exp(logits - logits.max())
        return float(p[: len(self.PACKAGE)].sum() / p.sum())


# on_change(kind ("delivered" or "picked_up"), the package, the frame after the
# visit, when the visit started)
OnChange = Callable[[str, Package, Shot, float], None]


class PackageWatcher:
    def __init__(
        self,
        *,
        present: Callable[[], bool],
        frame: Callable[[], Shot | None],
        zone: Callable[[], Sequence[Point] | None],
        score: Callable[[np.ndarray, Box], float],
        on_change: OnChange,
        state_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 5.0,
        settle_seconds: float = 15.0,
        refresh_seconds: float = 60.0,
        retry_seconds: float = 60.0,
        give_up_seconds: float = 600.0,
    ) -> None:
        self._present = present
        self._frame = frame
        self._zone = zone
        self._score = score
        self._on_change = on_change
        self._state_file = state_file
        self._clock = clock
        self._sleep = sleep
        self.interval = interval
        self.settle_seconds = settle_seconds
        self.refresh_seconds = refresh_seconds
        # A porch light on a timer, or the camera's night mode switching while
        # someone was there: compare again a little later, for a while.
        self.retry_seconds = retry_seconds
        self.give_up_seconds = give_up_seconds
        self._retry_at = 0.0
        self._unjudged = ""  # why the last comparison wasn't judged
        self._before: Shot | None = None  # the doorstep with nobody there
        self._before_at = 0.0
        self._came: float | None = None  # someone is (or was just) there, since
        self._seen = 0.0
        self.packages: list[Package] = self._load()

    def run_forever(self) -> None:
        while True:
            try:
                self.step()
            except Exception:
                log.exception("package check failed")
            self._sleep(self.interval)

    def step(self) -> None:
        now = self._clock()
        if self._present():
            if self._came is None:
                self._came = now
            self._seen = now
            return
        if self._came is not None:
            if now - self._seen < self.settle_seconds or now < self._retry_at:
                return
            after = self._frame()
            if after is None:
                return  # try again next time
            if self._before is not None and not self._judge(self._before, after, self._came):
                # One "not checked" line per visit (the daily report counts them).
                if now - self._seen < self.give_up_seconds:
                    self._retry_at = now + self.retry_seconds
                    log.info(
                        "doorstep not checked yet (%s): trying again in %.0f s",
                        self._unjudged,
                        self.retry_seconds,
                    )
                    return
                log.info("doorstep not checked: the lighting didn't settle (%s)", self._unjudged)
            self._came, self._retry_at = None, 0.0
            self._before, self._before_at = after, now
        elif self._before is None or now - self._before_at >= self.refresh_seconds:
            shot = self._frame()
            if shot is not None:
                self._before, self._before_at = shot, now

    def _judge(self, before: Shot, after: Shot, came: float) -> bool:
        """Compare; False if the frames couldn't be compared (the lighting changed)."""
        polygon = self._zone()
        if polygon is None:
            log.info("doorstep not checked: the package zone isn't known yet")
            return True
        if before.rgb.shape != after.rgb.shape:
            return True
        comparison = compare(before.rgb, after.rgb, zone_cells(polygon, *before.rgb.shape[:2]))
        if not comparison.judged:
            self._unjudged = comparison.reason  # step() logs it: trying again, or giving up
            return False
        log.info("doorstep checked: %d change(s)", len(comparison.patches))
        for package in list(self.packages):
            share = changed_share(comparison.changed, package.box)
            if share < GONE_SHARE:
                continue
            score = self._score(after.rgb, package.box)
            log.info(
                "package at %s: %.0f%% changed, package score %.2f",
                list(package.box),
                100 * share,
                score,
            )
            if score < GONE_SCORE:
                self.packages.remove(package)
                self._save()
                self._on_change("picked_up", package, after, came)
        for box in comparison.patches:
            if any(_overlaps(box, p.box) for p in self.packages):
                continue
            was, now = self._score(before.rgb, box), self._score(after.rgb, box)
            log.info("doorstep change at %s: package score %.2f (before %.2f)", list(box), now, was)
            if now >= DELIVERED_SCORE and now - was >= DELIVERED_RISE:
                package = Package(box, came, round(now, 2))
                self.packages.append(package)
                self._save()
                self._on_change("delivered", package, after, came)
        return True

    def _load(self) -> list[Package]:
        if self._state_file is None or not self._state_file.exists():
            return []
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return [Package(tuple(p["box"]), p["since"], p["score"]) for p in data]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("couldn't read %s: %s", self._state_file, e)
            return []

    def _save(self) -> None:
        if self._state_file is None:
            return
        data = [{**asdict(p), "box": list(p.box)} for p in self.packages]
        self._state_file.write_text(json.dumps(data), encoding="utf-8")
