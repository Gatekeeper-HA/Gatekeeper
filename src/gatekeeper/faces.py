"""Recognizing the household's faces (opt-in: ``FACE_RECOGNITION``).

Our own pipeline rather than Frigate's, so that it can use the camera's high-resolution
stream (a face is recognizable from farther away, before the greeting), runs on models
we may ship, and stores no stranger's face:

- YuNet finds faces and their five landmarks (OpenCV Zoo, MIT licence);
- each face is aligned on its landmarks to 112x112 (the ArcFace template);
- SFace turns it into 128 numbers (OpenCV Zoo, Apache-2.0), compared by cosine.

Both models run on the CPU with onnxruntime. The postprocessing is OpenCV's
(``FaceDetectorYN``, ``FaceRecognizerSF.alignCrop``), redone in numpy so the image
needn't carry OpenCV.

A face is someone's when it's close enough to the faces enrolled for them (the
``Gallery``). Only the owner enrolls people (``python -m gatekeeper.face_admin``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# The models, pinned to an OpenCV Zoo commit and checked; downloaded once into the cache.
ZOO = "https://media.githubusercontent.com/media/opencv/opencv_zoo/{commit}/models/{path}"
ZOO_COMMIT = "48b10fa505aeb51fbd127fc60a5c660040e823b7"
DETECTOR_MODEL = (
    "face_detection_yunet/face_detection_yunet_2026may.onnx",
    "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
)
EMBEDDER_MODEL = (
    "face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)

STRIDES = (8, 16, 32)
NMS_IOU = 0.3
# Where the five landmarks (eyes, nose tip, mouth corners) go in a 112x112 face.
TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float64,
)
FACE_SIZE = 112


@dataclass(frozen=True)
class Face:
    """A face in a frame: its box (x, y, width, height, in pixels), the five
    landmarks (5x2) and the detector's score."""

    box: tuple[float, float, float, float]
    landmarks: np.ndarray
    score: float

    @property
    def width(self) -> float:
        return self.box[2]

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.box
        return x + w / 2, y + h / 2

    def scaled(self, sx: float, sy: float) -> Face:
        x, y, w, h = self.box
        return Face((x * sx, y * sy, w * sx, h * sy), self.landmarks * (sx, sy), self.score)


def fetch_model(model: tuple[str, str], cache: Path) -> Path:
    """The model file (OpenCV Zoo path, SHA-256), downloaded into ``cache`` the first
    time and checked."""
    path, sha256 = model
    target = cache / Path(path).name
    if not target.exists():
        cache.mkdir(parents=True, exist_ok=True)
        url = ZOO.format(commit=ZOO_COMMIT, path=path)
        log.info("downloading %s", url)
        fd, tmp = tempfile.mkstemp(dir=cache, suffix=".part")
        os.close(fd)
        try:
            urllib.request.urlretrieve(url, tmp)
            digest = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
            if digest != sha256:
                raise ValueError(f"{url}: SHA-256 {digest}, expected {sha256}")
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return target


def _session(model: Path):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.log_severity_level = 3  # YuNet's export warns about its initializers
    return ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])


class Detector:
    """YuNet: faces in a BGR image, as OpenCV's ``FaceDetectorYN`` finds them."""

    def __init__(self, model: Path, threshold: float = 0.8) -> None:
        self.threshold = threshold
        self._session = _session(model)

    def detect(self, bgr: np.ndarray) -> list[Face]:
        h, w = bgr.shape[:2]
        ph, pw = -(-h // 32) * 32, -(-w // 32) * 32  # padded to a multiple of 32
        blob = np.zeros((1, 3, ph, pw), dtype=np.float32)
        blob[0, :, :h, :w] = bgr.transpose(2, 0, 1)
        out = dict(
            zip(
                (o.name for o in self._session.get_outputs()),
                self._session.run(None, {"input": blob}),
                strict=True,
            )
        )
        return decode(out, pw, ph, self.threshold)


def decode(out: dict[str, np.ndarray], pw: int, ph: int, threshold: float) -> list[Face]:
    """YuNet's outputs (per stride: cls, obj, bbox, kps on a grid) as faces, after NMS."""
    boxes, kps, scores = [], [], []
    for s in STRIDES:
        cols, rows = pw // s, ph // s
        cls = np.clip(out[f"cls_{s}"].reshape(-1), 0, 1)
        obj = np.clip(out[f"obj_{s}"].reshape(-1), 0, 1)
        score = np.sqrt(cls * obj)
        keep = np.nonzero(score >= threshold)[0]
        if not len(keep):
            continue
        r, c = (keep // cols).astype(np.float32), (keep % cols).astype(np.float32)
        b = out[f"bbox_{s}"].reshape(-1, 4)[keep]
        cx, cy = (c + b[:, 0]) * s, (r + b[:, 1]) * s
        bw, bh = np.exp(b[:, 2]) * s, np.exp(b[:, 3]) * s
        boxes.append(np.stack([cx - bw / 2, cy - bh / 2, bw, bh], axis=1))
        k = out[f"kps_{s}"].reshape(-1, 10)[keep].reshape(-1, 5, 2)
        kps.append((k + np.stack([c, r], axis=1)[:, None, :]) * s)
        scores.append(score[keep])
        del rows
    if not boxes:
        return []
    all_boxes, all_kps, all_scores = (
        np.concatenate(boxes),
        np.concatenate(kps),
        np.concatenate(scores),
    )
    return [
        Face(
            tuple(float(v) for v in all_boxes[i]),
            all_kps[i].astype(np.float64),
            float(all_scores[i]),
        )
        for i in nms(all_boxes, all_scores, NMS_IOU)
    ]


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Indexes of the boxes (x, y, w, h) kept by non-maximum suppression, best first."""
    order = list(np.argsort(-scores))
    x1, y1 = boxes[:, 0], boxes[:, 1]
    x2, y2 = x1 + boxes[:, 2], y1 + boxes[:, 3]
    area = boxes[:, 2] * boxes[:, 3]
    kept = []
    while order:
        i = order.pop(0)
        kept.append(int(i))
        if not order:
            break
        rest = np.array(order)
        w = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
        h = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
        overlap = w * h / (area[i] + area[rest] - w * h)
        order = [j for j, o in zip(order, overlap, strict=True) if o <= iou]
    return kept


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The 2x3 rotation + uniform scale + translation taking ``src`` points closest to
    ``dst`` (Umeyama), as OpenCV's ``alignCrop`` estimates it."""
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    s, d = src - src_mean, dst - dst_mean
    cov = d.T @ s / len(src)
    u, sig, vt = np.linalg.svd(cov)
    sign = np.eye(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        sign[1, 1] = -1
    rot = u @ sign @ vt
    scale = np.trace(np.diag(sig) @ sign) / s.var(axis=0).sum()
    m = np.empty((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = dst_mean - m[:, :2] @ src_mean
    return m


def align(bgr: np.ndarray, face: Face) -> np.ndarray:
    """The face warped to 112x112 on its landmarks (bilinear, black outside the frame)."""
    m = similarity_transform(face.landmarks, TEMPLATE)
    inv = np.linalg.inv(np.vstack([m, [0, 0, 1]]))[:2]
    v, u = np.mgrid[0:FACE_SIZE, 0:FACE_SIZE].astype(np.float64)
    x = inv[0, 0] * u + inv[0, 1] * v + inv[0, 2]
    y = inv[1, 0] * u + inv[1, 1] * v + inv[1, 2]
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    fx, fy = (x - x0)[..., None], (y - y0)[..., None]
    h, w = bgr.shape[:2]

    def px(yy, xx):
        inside = ((xx >= 0) & (xx < w) & (yy >= 0) & (yy < h))[..., None]
        return np.where(inside, bgr[np.clip(yy, 0, h - 1), np.clip(xx, 0, w - 1)], 0).astype(
            np.float64
        )

    top = px(y0, x0) * (1 - fx) + px(y0, x0 + 1) * fx
    bottom = px(y0 + 1, x0) * (1 - fx) + px(y0 + 1, x0 + 1) * fx
    return np.clip(top * (1 - fy) + bottom * fy + 0.5, 0, 255).astype(np.uint8)


class Embedder:
    """SFace: an aligned BGR face as a unit vector of 128 numbers."""

    def __init__(self, model: Path) -> None:
        self._session = _session(model)

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        blob = aligned_bgr[..., ::-1].transpose(2, 0, 1)[None].astype(np.float32)  # RGB
        vec = self._session.run(None, {"data": blob})[0].reshape(-1).astype(np.float64)
        return vec / np.linalg.norm(vec)


def sharpness(aligned_bgr: np.ndarray) -> float:
    """Variance of the Laplacian of the grey face: a blurred one scores low."""
    g = aligned_bgr.astype(np.float64).mean(axis=2)
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


class Gallery:
    """The people the owner enrolled: name -> their faces' embeddings, in a JSON file.

    Embeddings only, no pictures. ``forget`` and ``purge`` delete them for good."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._people: dict[str, np.ndarray] = {}
        self._mtime = None
        self.reload()

    def reload(self) -> None:
        """Read the file again if it changed (the owner enrolled someone)."""
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._people, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._people = {
            name: np.array(p["embeddings"], dtype=np.float64)
            for name, p in data.get("people", {}).items()
        }
        self._mtime = mtime

    @property
    def names(self) -> list[str]:
        return sorted(self._people)

    def match(self, emb: np.ndarray) -> tuple[str | None, float]:
        """The enrolled person closest to ``emb``, and the cosine similarity (best of
        their faces). (None, 0.0) with nobody enrolled."""
        best, score = None, 0.0
        for name, embs in self._people.items():
            s = float(np.max(embs @ emb))
            if s > score:
                best, score = name, s
        return best, score

    def enroll(self, name: str, embeddings: Sequence[np.ndarray]) -> None:
        data = self._read()
        person = data["people"].setdefault(name, {"embeddings": [], "added": time.time()})
        person["embeddings"].extend([[round(float(v), 6) for v in e] for e in embeddings])
        self._write(data)

    def forget(self, name: str) -> bool:
        data = self._read()
        gone = data["people"].pop(name, None) is not None
        self._write(data)
        return gone

    def purge(self) -> None:
        self.path.unlink(missing_ok=True)
        self.reload()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"people": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._mtime = None
        self.reload()
