"""Hearing a knock at the door (``KNOCK_LISTENER``), in shadow first: logged, nothing at the
door yet. Some visitors knock instead of ringing, and some aren't seen: a food courier on
the rig walked up along the house, out of the camera's view, knocked, and was never on
camera.

The audio tap hears the door all the time. A cheap check finds sharp sounds (the audio's
change from sample to sample, well above the last 30 s); only then does the sound
classifier (AST, fine-tuned on AudioSet, BSD-3-Clause, the ONNX export) look at the 2 s
around them for its "Knock" class. On the rig's test, the owner's 4 sets of knocks scored
0.59-0.92, and in the 15 minutes around them nothing else scored over 0.05: the door
opening, closing and slamming, footsteps, voices, a passing car, Gatekeeper's greeting.

The classifier needs a GPU: AST always looks at 10 s, which takes ~100 ms on an NVIDIA
GPU but ~12 s on a CPU core. A small hub needs a smaller model.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from gatekeeper.vad import SAMPLE_RATE

log = logging.getLogger(__name__)

AST_REPO = "Xenova/ast-finetuned-audioset-10-10-0.4593"
AST_REVISION = "249a1fbf0286b40e7f1ed687a8ae396997bf7dc6"
FRAME = SAMPLE_RATE // 100  # the sharpness check's step: 10 ms
SHARP = 6.0  # a sharp sound: this many times the background's median
BACKGROUND_SECONDS = 30.0
WINDOW = 2.0  # seconds the classifier looks at, from just before the first sharp sound
LEAD = 0.3
SAME_KNOCK_SECONDS = 15.0  # knocks this close are one knock event
STATS_SECONDS = 3600.0  # how often it logs how much it checked (the daily report reads it)


def sharpness(samples: np.ndarray) -> np.ndarray:
    """How sharp the sound is in each 10 ms: the RMS of the sample-to-sample change."""
    x = samples.astype(np.float32) / 32768
    d = np.diff(x, prepend=x[:1])
    n = len(d) // FRAME
    return np.sqrt((d[: n * FRAME].reshape(n, FRAME) ** 2).mean(1)) if n else np.zeros(0)


class Classifier:
    """AST's "Knock" probability for a 2 s clip (int16 at 16 kHz). On CUDA when onnxruntime
    has it (the GPU image), else the CPU (slow)."""

    def __init__(self, device: str = "auto", threads: int = 2) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import ASTFeatureExtractor

        def fetch(name: str) -> str:
            return hf_hub_download(AST_REPO, name, revision=AST_REVISION)

        self._features = ASTFeatureExtractor.from_pretrained(AST_REPO, revision=AST_REVISION)
        labels = json.loads(Path(fetch("config.json")).read_text())["id2label"]
        self._knock = int(next(i for i, label in labels.items() if label == "Knock"))
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.log_severity_level = 3
        providers: list = ["CPUExecutionProvider"]
        if device != "cpu" and "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")
        self._session = ort.InferenceSession(fetch("onnx/model.onnx"), options, providers=providers)
        self.device = "cuda" if self._session.get_providers()[0].startswith("CUDA") else "cpu"
        if self.device == "cpu":
            log.warning("knock classifier on the CPU: each check takes seconds")

    def knock(self, samples: np.ndarray) -> float:
        x = samples.astype(np.float32) / 32768
        x = x * (0.1 / (np.sqrt((x**2).mean()) + 1e-9))  # the night audio is very quiet
        feats = self._features(x, sampling_rate=SAMPLE_RATE, return_tensors="np")
        name = self._session.get_inputs()[0].name
        logits = self._session.run(None, {name: feats["input_values"].astype(np.float32)})[0][0]
        return float(1 / (1 + np.exp(-logits[self._knock])))


class KnockListener:
    """Listens on the audio tap (``slice``/``wait`` on time.monotonic()); calls
    ``on_knock(score, at)`` (``at`` is time.time()) once per knock event."""

    def __init__(
        self,
        tap,
        classifier: Callable[[], object],
        *,
        threshold: float = 0.5,
        on_knock: Callable[[float, float], None] = lambda score, at: None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.tap = tap
        self._make_classifier, self._classifier = classifier, None
        self.threshold = threshold
        self._on_knock = on_knock
        self._monotonic, self._wall = monotonic, wall
        self._background: deque[float] = deque(maxlen=int(BACKGROUND_SECONDS * 100))
        self._done_to: float | None = None  # audio looked at up to here (monotonic)
        self._pending: float | None = None  # a window to classify, from here
        self._busy_to = 0.0  # classified up to here: one window at a time
        self._last_knock = -1e9
        self.checks = self.knocks = 0
        self._stats_from: float | None = None  # this hour's checks and top score, from here
        self._stats_checks, self._stats_top = 0, 0.0
        self._stopping = False

    def run_forever(self) -> None:
        while not self._stopping:
            self.tap.wait(0.5)
            try:
                self.step()
            except Exception:
                log.exception("knock listener failed; carrying on")
                time.sleep(1.0)

    def stop(self) -> None:
        self._stopping = True

    def step(self) -> None:
        """Look at the audio that arrived since last time; classify a window once it's in."""
        now = self._monotonic()
        if self._done_to is None or now - self._done_to > BACKGROUND_SECONDS:
            self._done_to = now  # start (or after a gap): from here on
            return
        new = self.tap.slice(self._done_to, now)
        frames = sharpness(new)
        if len(frames):
            base = float(np.median(self._background)) if self._background else None
            for n, value in enumerate(frames):
                at = self._done_to + n / 100
                if base and value > SHARP * base and self._pending is None and at > self._busy_to:
                    self._pending = at - LEAD
                self._background.append(float(value))
            self._done_to += len(frames) / 100
        if self._pending is not None and now >= self._pending + WINDOW:
            start, self._pending = self._pending, None
            self._busy_to = start + WINDOW
            self._classify(start, now)
        self.log_stats(now)

    def log_stats(self, now: float) -> None:
        """Every ``STATS_SECONDS``: how many sounds went to the classifier, and the top
        score, so a quiet day reads apart from a listener that isn't checking."""
        if self._stats_from is None:
            self._stats_from = now
        if now - self._stats_from < STATS_SECONDS:
            return
        log.info(
            "knock listener stats: %d check(s) in %.0f s, top score %.2f",
            self._stats_checks,
            now - self._stats_from,
            self._stats_top,
        )
        self._stats_from, self._stats_checks, self._stats_top = now, 0, 0.0

    def _classify(self, start: float, now: float) -> None:
        samples = self.tap.slice(start, start + WINDOW)
        if len(samples) < SAMPLE_RATE:
            return
        if self._classifier is None:
            self._classifier = self._make_classifier()
        score = self._classifier.knock(samples)
        self.checks += 1
        self._stats_checks += 1
        self._stats_top = max(self._stats_top, score)
        if score < self.threshold:
            return
        at = self._wall() - (now - start)
        if at - self._last_knock > SAME_KNOCK_SECONDS:
            self.knocks += 1
            try:
                self._on_knock(score, at)
            except Exception:
                log.exception("knock callback failed")
        self._last_knock = at


class Knocks:
    """The knocks heard, one JSON line each (``knocks.jsonl`` next to the visit log)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def add(self, record: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")

    def since(self, t: float) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("at", 0) >= t:
                out.append(record)
        return out
