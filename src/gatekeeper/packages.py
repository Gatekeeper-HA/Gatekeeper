"""Package alerts: a package left at the door, and picked up (Phase 1).

A package only appears or goes when someone comes to the door, so the doorstep
isn't watched all the time; it's compared before and after each visit:

1. While nobody is on the porch, ``PackageWatcher`` keeps a recent frame from the
   camera (the perception's latest frame).
2. Once whoever came has been gone ``settle_seconds``, it takes another and
   compares the package zone (PACKAGE_ZONE, a perception zone) in the two
   (``compare``): grid cells whose brightness changed, after evening out the
   camera's exposure, grouped into patches. A change across the whole frame (a
   light switched on, the camera's night mode) isn't judged.
3. A small open image model (CLIP, on the CPU, ``PackageModel``) scores each
   patch as a package or not, before and after. A patch that looks like a
   package now, and didn't before, is a new package: "Package delivered".
4. A package whose patch changed, and no longer looks like one, after someone
   came and went: "Package picked up".

When the light changed during a visit (a light switched on, the camera's night mode
or a doorbell's white LED) the frame from before can't be compared. The doorstep is
then compared with the last frame from before the visit in the same light
(``Baselines``: one per kind of light seen lately, learned as they come, nothing
assumed about the camera or the porch). If there's none yet, the visit waits, up to
``PENDING_SECONDS``, for its light to come back.

Each check is logged with its scores, for tuning. The day's numbers are in the
daily report.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from gatekeeper.perception import Point, Shot, decode_jpeg, inside  # noqa: F401 (re-exported)

log = logging.getLogger(__name__)

Box = tuple[int, int, int, int]  # x0, y0, x1, y1, in pixels

# Comparing the doorstep (on frames ~480 rows high: 640x480 on the rig).
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
# Kinds of light (Baselines): frames are filed by the camera's mode, colour or
# monochrome (infrared night mode), and by brightness, in steps of MAX_EXPOSURE_RATIO.
MONO_CHROMA = 4.0  # a mean |red - blue| under this: a monochrome frame
BASELINE_MAX_AGE = 2 * 86400  # older doorstep frames aren't compared with
PENDING_SECONDS = 12 * 3600  # how long a visit not checked waits for its light
MAX_PENDING = 5


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
        # On the CPU even where onnxruntime has CUDA (the GPU image): it's a few checks a
        # day, and a GPU build won't guess the provider.
        cpu = ["CPUExecutionProvider"]
        vision = ort.InferenceSession(fetch("onnx/vision_model_quantized.onnx"), options, cpu)
        text = ort.InferenceSession(fetch("onnx/text_model_quantized.onnx"), options, cpu)
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


def light(rgb: np.ndarray) -> tuple[str, int]:
    """The kind of light a frame was taken in: the camera's mode ("colour", or "mono"
    for infrared night mode) and its brightness, in steps that compare() can even out."""
    small = rgb[::8, ::8].astype(np.float32)
    chroma = float(np.abs(small[..., 0] - small[..., 2]).mean())
    level = max(float(np.median(_gray(small))), 1.0)
    return ("mono" if chroma < MONO_CHROMA else "colour"), round(
        math.log(level) / math.log(MAX_EXPOSURE_RATIO)
    )


def alike(a: tuple[str, int], b: tuple[str, int]) -> bool:
    """Are two kinds of light close enough to compare frames in (same mode, about as
    bright)? compare() still has the last word."""
    return a[0] == b[0] and abs(a[1] - b[1]) <= 1


class Baselines:
    """The doorstep with nobody there, in each kind of light it was seen in lately
    (``light``): daylight, dusk, a light switched on, the camera's night mode. Nothing
    about the camera or the porch is assumed; they're learned as they come. Kept as
    JPEGs in ``directory`` (if given), so a restart doesn't lose them."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        decode: Callable[[bytes], np.ndarray | None] = decode_jpeg,
        max_age: float = BASELINE_MAX_AGE,
    ) -> None:
        self._dir = directory
        self._clock = clock
        self.max_age = max_age
        self._shots: dict[tuple[str, int], tuple[float, Shot]] = {}
        if directory is not None and directory.exists():
            for path in directory.glob("*.jpg"):
                data = path.read_bytes()
                rgb = decode(data)
                if rgb is not None:
                    self._shots[light(rgb)] = (path.stat().st_mtime, Shot(rgb, data))
        self._prune()

    def add(self, shot: Shot, at: float) -> None:
        key = light(shot.rgb)
        self._shots[key] = (at, shot)
        self._prune()
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{key[0]}{key[1]:+d}.jpg"
            path.write_bytes(shot.jpeg)
            os.utime(path, (at, at))

    def like(self, shot: Shot, before: float) -> list[tuple[float, Shot]]:
        """Frames taken before ``before`` in light like ``shot``'s, the most alike (then
        the latest) first."""
        key = light(shot.rgb)
        found = [
            (abs(k[1] - key[1]), -at, at, s)
            for k, (at, s) in self._shots.items()
            if alike(k, key) and at < before
        ]
        return [(at, s) for *_, at, s in sorted(found, key=lambda f: f[:2])]

    def before(self, t: float) -> list[tuple[float, Shot]]:
        """Every frame taken before ``t`` (kept by a visit waiting for its light)."""
        return [(at, s) for at, s in self._shots.values() if at < t]

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, (at, _) in self._shots.items() if now - at > self.max_age]:
            del self._shots[key]
            if self._dir is not None:
                (self._dir / f"{key[0]}{key[1]:+d}.jpg").unlink(missing_ok=True)


@dataclass
class _Pending:
    """A visit whose doorstep couldn't be compared: it waits for the light it had before."""

    came: float
    baselines: list[tuple[float, Shot]]  # the doorstep before the visit, by kind of light


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
        baselines: Baselines | None = None,
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
        self.baselines = baselines or Baselines(clock=clock)
        self._pending: list[_Pending] = []
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
            if self._before is not None and not self._check(after, self._came):
                # One "not checked" line per visit, at the end (the daily report counts them).
                if now - self._seen < self.give_up_seconds:
                    self._retry_at = now + self.retry_seconds
                    log.info(
                        "doorstep not checked yet (%s): trying again in %.0f s",
                        self._unjudged,
                        self.retry_seconds,
                    )
                    return
                self._wait_for_light(self._came)
            self._came, self._retry_at = None, 0.0
            self._before, self._before_at = after, now
            self.baselines.add(after, now)
        elif self._before is None or now - self._before_at >= self.refresh_seconds:
            shot = self._frame()
            if shot is not None:
                self._before, self._before_at = shot, now
                self.baselines.add(shot, now)
                self._recheck(shot, now)

    def _check(self, after: Shot, came: float) -> bool:
        """Compare the doorstep after a visit with the one before; if the light changed,
        with the last frame from before the visit in the same light. False if neither
        could be compared."""
        if self._judge(self._before, after, came):
            return True
        reason = self._unjudged
        for at, shot in self.baselines.like(after, came):
            if self._judge(shot, after, came, against=f"the doorstep at {_hhmm(at)}"):
                return True
        self._unjudged = reason
        return False

    def _wait_for_light(self, came: float) -> None:
        """The light didn't settle, and the doorstep was never seen in this light before:
        keep the frames from before the visit, and compare once the light is like one of
        them again (``_recheck``)."""
        self._pending.append(_Pending(came, self.baselines.before(came)))
        log.info(
            "doorstep not checked yet (%s, a light it wasn't seen in before): "
            "waiting up to %.0f h for the light from before the visit",
            self._unjudged,
            PENDING_SECONDS / 3600,
        )
        while len(self._pending) > MAX_PENDING:
            self._give_up(self._pending.pop(0))

    def _recheck(self, shot: Shot, now: float) -> None:
        """Compare the doorstep now with visits that are still waiting for their light."""
        for pending in list(self._pending):
            if now - pending.came > PENDING_SECONDS:
                self._pending.remove(pending)
                self._give_up(pending)
                continue
            key = light(shot.rgb)
            for at, base in pending.baselines:
                if not alike(light(base.rgb), key):
                    continue
                visit = f"the doorstep at {_hhmm(at)}, before a visit at {_hhmm(pending.came)}"
                if self._judge(base, shot, pending.came, against=visit):
                    self._pending.remove(pending)
                    break

    def _give_up(self, pending: _Pending) -> None:
        log.info(
            "doorstep not checked: the lighting didn't come back (a visit at %s)",
            _hhmm(pending.came),
        )

    def _judge(self, before: Shot, after: Shot, came: float, against: str = "") -> bool:
        """Compare; False if the frames couldn't be compared (the lighting changed).
        ``against``: what ``before`` is, when it isn't the doorstep just before the visit."""
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
        log.info(
            "doorstep checked: %d change(s)%s",
            len(comparison.patches),
            f" (against {against}, in the same light)" if against else "",
        )
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


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))
