"""Gatekeeper's guess at who a visitor was: a delivery, or not. In shadow (``DELIVERY_GUESS``):
it's kept for each greeted visit, shown on the Visits page next to the owner's label, and
the daily report says how often they agree. Nothing at the door depends on it yet.

On the rig's labelled visits, what the person looks like wasn't enough on its own: the
household carries bags and boxes too, and at night the camera loses colour. How they move
was clearer: couriers walk up, don't answer, and go back down the walk. So the guess uses
both:

- **looks:** SigLIP (Google, Apache-2.0), zero-shot, on full-resolution crops of the person
  at the moments they were closest to the camera, scored against descriptions of couriers
  and of the household. Best of three models tried on the rig (AUC 0.80);
- **moves:** they left the way they came; they walked away when greeted (the send-off);
- **a package** left during the visit.

Only scores are kept, never the crops.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# SigLIP base, the ONNX export (quantized), pinned to the revision tested on the rig.
SIGLIP_REPO = "Xenova/siglip-base-patch16-224"
SIGLIP_REVISION = "4649052661e53c7000355844105f8a1792088239"
DELIVERY = (
    "a delivery driver in a brown UPS uniform",
    "an Amazon delivery driver wearing a blue safety vest",
    "a FedEx courier in a purple and black uniform",
    "a mail carrier in a USPS uniform",
    "a delivery person carrying a cardboard box",
    "a person carrying grocery bags",
    "a person carrying a pizza box",
    "a food delivery driver holding a bag of takeout food",
)
OTHER = (
    "a person in casual clothes at the front door of their home",
    "a person walking out of their house",
    "a person coming home",
    "a child",
    "a person walking a dog",
    "a person taking out the trash",
)
CROPS = 3  # moments a visit is looked at: the closest to the camera, 0.5 s apart or more
BACK = 0.1  # "left the way they came": ended this much higher in the frame than their lowest
# The rule, from the rig's first labels (too few to fit anything): it looks like a delivery,
# or it moved like one and looks a little like one, or a package was left.
LOOKS = 0.3
LOOKS_A_LITTLE = 0.1


def closest_moments(path: list, n: int = CROPS) -> list[tuple[tuple[float, float], float]]:
    """Up to ``n`` (point, time) of a path, lowest in the frame (closest) first, 0.5 s apart."""
    chosen: list = []
    for point, t in sorted(path, key=lambda p: -p[0][1]):
        if all(abs(t - c[1]) >= 0.5 for c in chosen):
            chosen.append((tuple(point), t))
        if len(chosen) == n:
            break
    return chosen


def moves(path: list) -> dict:
    """How the person moved: where they started and ended, their lowest (closest) point,
    and whether they left the way they came (back up the frame from their lowest)."""
    if not path:
        return {}
    ys = [p[0][1] for p in path]
    return {
        "start": [round(v, 2) for v in path[0][0]],
        "end": [round(v, 2) for v in path[-1][0]],
        "lowest": round(max(ys), 2),
        "left_the_way_they_came": max(ys) - ys[-1] >= BACK,
    }


def decide(looks: float | None, features: dict) -> str:
    """ "delivery" or "other", from the looks score and the features."""
    if features.get("package_left"):
        return "delivery"
    if looks is None:
        return "other"
    moved_like_one = features.get("left_the_way_they_came") or features.get("walked_away")
    if looks >= LOOKS or (moved_like_one and looks >= LOOKS_A_LITTLE):
        return "delivery"
    return "other"


def square(rgb: np.ndarray, size: int) -> np.ndarray:
    """Padded to a square with grey (keeping the whole person), then resized."""
    import av

    h, w = rgb.shape[:2]
    n = max(h, w)
    canvas = np.full((n, n, 3), 128, np.uint8)
    canvas[(n - h) // 2 : (n - h) // 2 + h, (n - w) // 2 : (n - w) // 2 + w] = rgb
    frame = av.VideoFrame.from_ndarray(canvas, format="rgb24")
    return frame.to_ndarray(width=size, height=size, format="rgb24", interpolation="BICUBIC")


class Looks:
    """SigLIP, zero-shot: how much a crop looks like a courier rather than the household
    (the share of its probability on the courier descriptions)."""

    def __init__(self, threads: int = 1) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        def fetch(name: str) -> str:
            return hf_hub_download(SIGLIP_REPO, name, revision=SIGLIP_REVISION)

        options = ort.SessionOptions()
        options.intra_op_num_threads = threads  # leave the CPU to the visit and Frigate
        options.log_severity_level = 3
        cpu = ["CPUExecutionProvider"]
        self._vision = ort.InferenceSession(fetch("onnx/vision_model_quantized.onnx"), options, cpu)
        text = ort.InferenceSession(fetch("onnx/text_model_quantized.onnx"), options, cpu)
        tokenizer = Tokenizer.from_file(fetch("tokenizer.json"))
        ids = np.array(
            [(e.ids + [1] * 64)[:64] for e in tokenizer.encode_batch([*DELIVERY, *OTHER])],
            dtype=np.int64,
        )
        self._texts = _unit(_output(text, {"input_ids": ids}))

    def score(self, crops: list[np.ndarray]) -> float | None:
        if not crops:
            return None
        shares = []
        for rgb in crops:
            x = square(rgb, 224).astype(np.float32) / 255
            x = ((x - 0.5) / 0.5).transpose(2, 0, 1)[None].astype(np.float32)
            logits = 100 * (self._texts @ _unit(_output(self._vision, {"pixel_values": x}))[0])
            p = np.exp(logits - logits.max())
            shares.append(float((p / p.sum())[: len(DELIVERY)].sum()))
        return round(float(np.mean(shares)), 3)


def _output(session, feeds) -> np.ndarray:
    outs = dict(zip([o.name for o in session.get_outputs()], session.run(None, feeds), strict=True))
    return outs.get("pooler_output", next(iter(outs.values())))


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


class Guesses:
    """The guesses, by visit (event id), in ``visit-guesses.json`` next to the visit log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("guesses", {})
        except (FileNotFoundError, ValueError):
            return {}

    def add(self, event_id: str, guess: dict) -> None:
        with self._lock:
            guesses = self.all()
            guesses[event_id] = guess
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"guesses": guesses}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


class DeliveryGuesser:
    """Guesses each greeted visit in the background, once the person has gone (the
    recording is complete). ``path(event_id)`` is the person's path, [((x, y), time)];
    ``frame(time)`` a full-resolution JPEG of that moment; ``detect`` finds people (YOLOX);
    ``package_left(event_id)`` whether a package was left in that visit."""

    def __init__(
        self,
        guesses: Guesses,
        *,
        path: Callable[[str], list],
        frame: Callable[[float], bytes | None],
        detect,
        looks: Callable[[], Looks] = Looks,
        package_left: Callable[[str], bool] = lambda event_id: False,
        delay: float = 60.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.guesses = guesses
        self._path, self._frame, self._detect = path, frame, detect
        self._make_looks, self._looks = looks, None
        self._package_left = package_left
        self.delay = delay
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()  # one guess at a time

    def visit_logged(self, record: dict) -> None:
        """``Interaction``'s on_logged: guess this visit later, on its own thread."""
        event_id = str(record.get("event_id", ""))
        if record.get("trigger") != "person" or event_id.startswith(("press-", "package-")):
            return
        threading.Thread(
            target=self._later, args=(record,), name="delivery-guess", daemon=True
        ).start()

    def _later(self, record: dict) -> None:
        self._sleep(self.delay)
        try:
            self.guess(record)
        except Exception:
            log.exception("delivery guess failed for %s", record.get("event_id"))

    def guess(self, record: dict) -> dict:
        event_id = str(record["event_id"])
        with self._lock:
            path = self._path(event_id) or []
            features = moves(path)
            features["walked_away"] = bool((record.get("details") or {}).get("walked_away"))
            features["package_left"] = bool(self._package_left(event_id))
            features["answered"] = bool(record.get("transcript"))
            crops = self.crops(path)
            if self._looks is None and crops:
                self._looks = self._make_looks()
            looks = self._looks.score(crops) if crops else None
            guess = {
                "guess": decide(looks, features),
                "looks": looks,
                "crops": len(crops),
                "features": features,
                "at": round(self._clock(), 3),
            }
        self.guesses.add(event_id, guess)
        log.info(
            "delivery guess for %s: %s (looks %s, %s)", event_id, guess["guess"], looks, features
        )
        return guess

    def crops(self, path: list) -> list[np.ndarray]:
        """Full-resolution crops of the person at their closest moments (as RGB)."""
        import av

        out = []
        for (x, y), t in closest_moments(path):
            data = self._frame(t)
            if not data:
                continue
            try:
                frame = next(av.open(io.BytesIO(data)).decode(video=0))
            except Exception:
                continue
            w, h = self._detect.fit(frame.width, frame.height)
            found = self._detect.detect(frame.to_ndarray(width=w, height=h, format="bgr24"))
            if not found:
                continue
            d = min(found, key=lambda d: abs(d.foot[0] - x) + abs(d.foot[1] - y))
            if abs(d.foot[0] - x) + abs(d.foot[1] - y) > 0.25:  # not the person on the path
                continue
            full = frame.to_ndarray(format="rgb24")
            x0, y0, x1, y1 = d.box
            mx, my = (x1 - x0) * 0.1, (y1 - y0) * 0.1
            H, W = full.shape[:2]
            a, b = int(max(0, x0 - mx) * W), int(max(0, y0 - my) * H)
            c, e = int(min(1, x1 + mx) * W), int(min(1, y1 + my) * H)
            out.append(full[b:e, a:c])
        return out
