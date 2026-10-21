import hashlib
import math

import numpy as np
import pytest

from gatekeeper import faces
from gatekeeper.faces import TEMPLATE, Detector, Embedder, Face, Gallery, align, decode, nms


def yunet_outputs(pw, ph, hits=()):
    """YuNet's outputs for a padded pw x ph image, with ``hits``: (stride, row, col,
    score, bbox offsets (dx, dy, log w, log h), landmark offsets (10))."""
    out = {}
    for s in faces.STRIDES:
        n = (pw // s) * (ph // s)
        out[f"cls_{s}"] = np.zeros((1, n, 1), np.float32)
        out[f"obj_{s}"] = np.zeros((1, n, 1), np.float32)
        out[f"bbox_{s}"] = np.zeros((1, n, 4), np.float32)
        out[f"kps_{s}"] = np.zeros((1, n, 10), np.float32)
    for s, r, c, score, bbox, kps in hits:
        idx = r * (pw // s) + c
        out[f"cls_{s}"][0, idx, 0] = score
        out[f"obj_{s}"][0, idx, 0] = score
        out[f"bbox_{s}"][0, idx] = bbox
        out[f"kps_{s}"][0, idx] = kps
    return out


def test_yunet_grid_outputs_decode_to_boxes_and_landmarks():
    kps = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    out = yunet_outputs(64, 64, [(8, 2, 3, 0.9, [0.5, 0.5, math.log(2), math.log(3)], kps)])
    (face,) = decode(out, 64, 64, threshold=0.8)
    # Centre ((3 + 0.5) * 8, (2 + 0.5) * 8) = (28, 20); 16 x 24.
    assert face.box == pytest.approx((20.0, 8.0, 16.0, 24.0))
    assert face.landmarks[0] == pytest.approx(((3 + 0.1) * 8, (2 + 0.2) * 8))
    assert face.score == pytest.approx(0.9)  # sqrt(cls * obj)


def test_faces_under_the_threshold_are_dropped():
    out = yunet_outputs(64, 64, [(16, 1, 1, 0.5, [0, 0, 0, 0], [0] * 10)])
    assert decode(out, 64, 64, threshold=0.8) == []


def test_nms_keeps_the_best_of_overlapping_boxes():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 10, 10]], dtype=float)
    assert nms(boxes, np.array([0.8, 0.9, 0.7]), 0.3) == [1, 2]


def test_the_similarity_transform_is_recovered():
    a = math.radians(20)
    rot = 1.7 * np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    src = TEMPLATE @ rot.T + (300, 120)
    m = faces.similarity_transform(src, TEMPLATE)
    back = src @ m[:, :2].T + m[:, 2]
    assert back == pytest.approx(TEMPLATE, abs=1e-6)


def test_a_face_already_in_template_position_aligns_to_the_same_pixels():
    img = (np.arange(200 * 200 * 3) % 251).astype(np.uint8).reshape(200, 200, 3)
    face = Face((30, 40, 50, 60), TEMPLATE.copy(), 0.9)
    assert np.array_equal(align(img, face), img[:112, :112])


def test_scaling_a_face_scales_its_box_and_landmarks():
    face = Face((10, 20, 30, 40), np.ones((5, 2)), 0.9).scaled(4, 2)
    assert face.box == (40, 40, 120, 80)
    assert face.landmarks[0].tolist() == [4, 2]
    assert face.width == 120 and face.center == (100, 80)


class FakeSession:
    def __init__(self, outputs):
        self.outputs, self.inputs = outputs, []

    def get_outputs(self):
        return [type("Out", (), {"name": k})() for k in self.outputs]

    def run(self, _, feeds):
        self.inputs.append(feeds)
        return list(self.outputs.values())


def test_the_detector_pads_the_image_to_a_multiple_of_32():
    detector = Detector.__new__(Detector)
    detector.threshold = 0.8
    detector._session = FakeSession(yunet_outputs(64, 32))
    assert detector.detect(np.zeros((30, 50, 3), np.uint8)) == []
    (blob,) = [f["input"] for f in detector._session.inputs]
    assert blob.shape == (1, 3, 32, 64)


def test_the_embedder_gives_a_unit_vector_from_rgb():
    embedder = Embedder.__new__(Embedder)
    embedder._session = FakeSession({"fc1": np.full((1, 128), 3.0, np.float32)})
    face = np.zeros((112, 112, 3), np.uint8)
    face[..., 0] = 255  # blue, in BGR
    vec = embedder.embed(face)
    assert np.linalg.norm(vec) == pytest.approx(1.0)
    blob = embedder._session.inputs[0]["data"]
    assert blob[0, 2].max() == 255 and blob[0, 0].max() == 0  # channels swapped to RGB


def unit(*values):
    v = np.array(values + (0.0,) * (128 - len(values)))
    return v / np.linalg.norm(v)


def test_the_gallery_matches_the_closest_enrolled_person(tmp_path):
    g = Gallery(tmp_path / "gallery.json")
    assert g.match(unit(1)) == (None, 0.0)
    g.enroll("Alex", [unit(1), unit(1, 1)])
    g.enroll("Sam", [unit(0, 0, 1)])
    name, score = g.match(unit(1, 0.1))
    assert name == "Alex" and score > 0.99
    assert g.names == ["Alex", "Sam"]
    assert g.forget("Alex") and not g.forget("Alex")
    assert g.match(unit(1))[0] is None
    g.purge()
    assert g.names == [] and not g.path.exists()


def test_the_gallery_rereads_its_file_when_it_changes(tmp_path):
    watcher = Gallery(tmp_path / "gallery.json")
    Gallery(tmp_path / "gallery.json").enroll("Alex", [unit(1)])  # face_admin, elsewhere
    watcher.reload()
    assert watcher.names == ["Alex"]


def test_a_model_is_downloaded_once_and_checked(tmp_path, monkeypatch):
    data = b"onnx model"
    calls = []

    def fake_retrieve(url, path):
        calls.append(url)
        with open(path, "wb") as f:
            f.write(data)

    monkeypatch.setattr(faces.urllib.request, "urlretrieve", fake_retrieve)
    model = ("dir/model.onnx", hashlib.sha256(data).hexdigest())
    path = faces.fetch_model(model, tmp_path)
    assert path.read_bytes() == data and faces.ZOO_COMMIT in calls[0]
    faces.fetch_model(model, tmp_path)
    assert len(calls) == 1

    with pytest.raises(ValueError, match="SHA-256"):
        faces.fetch_model(("dir/other.onnx", "0" * 64), tmp_path)
    assert not (tmp_path / "other.onnx").exists()
    assert not list(tmp_path.glob("*.part"))


def test_blurred_faces_are_less_sharp():
    rng = np.random.default_rng(1)
    sharp = rng.integers(0, 255, (112, 112, 3), dtype=np.uint8)
    blurred = ((sharp.astype(float) + np.roll(sharp, 1, 0) + np.roll(sharp, 1, 1)) / 3).astype(
        np.uint8
    )
    assert faces.sharpness(sharp) > 3 * faces.sharpness(blurred)
