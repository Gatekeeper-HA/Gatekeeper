import logging
import os

import numpy as np

from gatekeeper import face_admin
from gatekeeper.face_watch import KEEP_PER_EVENT, FaceWatcher, display_crop, owner
from gatekeeper.faces import TEMPLATE, Face, Gallery


def unit(*values):
    v = np.array(values + (0.0,) * (128 - len(values)))
    return v / np.linalg.norm(v)


class Frame:
    """A 2560x1920 frame (as av.VideoFrame) with a textured picture, so faces are sharp."""

    width, height = 2560, 1920

    def __init__(self):
        rng = np.random.default_rng(0)
        self.full = rng.integers(0, 255, (1920, 2560, 3), dtype=np.uint8)
        self.asked = []

    def to_ndarray(self, width=None, height=None, format=None):
        self.asked.append((width, height))
        if width:
            return self.full[:: 1920 // height, :: 2560 // width]
        return self.full


def face_at(x, y, width):
    """A face (at 640x480) whose box starts at (x, y), ``width`` wide."""
    scale = width / 112
    return Face((x, y, width, width), TEMPLATE * scale + (x, y), 0.95)


class Detector:
    def __init__(self, *faces):
        self.faces = list(faces)

    def detect(self, image):
        assert image.shape[:2] == (480, 640)
        return self.faces


class Embedder:
    def __init__(self, *embeddings):
        self.embeddings = list(embeddings)

    def embed(self, aligned):
        assert aligned.shape == (112, 112, 3)
        return self.embeddings.pop(0) if len(self.embeddings) > 1 else self.embeddings[0]


class Tap:
    def __init__(self, frames):
        self.frames_given = frames
        self.asked = []

    def frames(self, since, every, stop):
        self.asked.append((since, every))
        for at, frame in self.frames_given:
            if stop():
                return
            yield at, frame


def watcher(tmp_path, detector, embedder, people, seen, tap=None, clock=lambda: 1000.0):
    gallery = Gallery(tmp_path / "gallery.json")
    gallery.enroll("Alex", [unit(1)])
    w = FaceWatcher(
        tap or Tap([]),
        detector=detector,
        embedder=embedder,
        gallery=gallery,
        people=people,
        on_face=seen.append,
        threshold=0.5,
        min_width=80,
        candidate_dir=tmp_path / "candidates",
        candidate_days=14,
        clock=clock,
        encode=lambda bgr: b"JPEG %dx%d" % bgr.shape[:2],
    )
    return w


def test_a_face_is_matched_from_the_full_frame_and_put_down_to_the_person(tmp_path):
    seen = []
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(1, 0.2)),
        lambda: [("A", 990.0, 0.2)],
        seen,
    )
    frame = Frame()
    (s,) = w.look(1000.5, frame)
    assert s is seen[0]
    assert (s.event_id, s.name) == ("A", "Alex") and s.score > 0.9
    assert s.width == 120  # 30 px at 640x480 is 120 in the 2560x1920 frame
    assert frame.asked == [(640, 480), (None, None)]  # detected small, aligned from full


def test_a_face_unlike_anyone_enrolled_has_no_name(tmp_path):
    seen = []
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(0, 1)),
        lambda: [("A", 0, None)],
        seen,
    )
    (s,) = w.look(1000.5, Frame())
    assert s.name is None and s.score < 0.5


def test_small_or_unsure_faces_are_skipped(tmp_path):
    seen = []
    small = face_at(100, 50, 15)  # 60 px in the full frame
    unsure = Face(face_at(300, 50, 30).box, face_at(300, 50, 30).landmarks, 0.6)
    w = watcher(
        tmp_path, Detector(small, unsure), Embedder(unit(1)), lambda: [("A", 0, None)], seen
    )
    frame = Frame()
    assert w.look(1000.5, frame) == [] and seen == []
    assert frame.asked == [(640, 480)]  # no need to convert the full frame


def test_with_two_people_a_face_goes_to_the_nearest_track():
    people = [("A", 0, 0.2), ("B", 0, 0.7)]
    assert owner(0.25, people) == "A"
    assert owner(0.65, people) == "B"
    assert owner(0.45, people) is None  # between them: can't tell
    assert owner(0.9, [("A", 0, None)]) == "A"  # the only one


def test_watching_runs_while_someone_is_on_camera_from_before_they_appeared(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="gatekeeper.face_watch")
    seen = []
    frames = [(1000.0 + k, Frame()) for k in range(3)]
    tap = Tap(frames)
    on_camera = [("A", 999.0, 0.2)]
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(1)),
        lambda: on_camera,
        seen,
        tap=tap,
    )
    assert w._watch_once()
    assert tap.asked == [(998.0, 0.25)]  # LEAD_SECONDS before the first person appeared
    assert [s.name for s in seen] == ["Alex"] * 3
    assert "face watch: 0 s, 3 frame(s), 3 face(s), best match 1.00; Alex x3" in caplog.text
    on_camera.clear()
    assert not w._watch_once()


def test_the_faces_of_someone_who_went_in_are_kept_for_enrollment(tmp_path):
    seen = []
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(0, 1)),
        lambda: [("A", 0, None)],
        seen,
    )
    for k in range(KEEP_PER_EVENT + 3):
        w.look(1000.0 + k, Frame())
    path = w.keep("A", "entered")
    with np.load(path) as d:
        assert d["embeddings"].shape == (KEEP_PER_EVENT, 128)
        assert d["faces"].shape == (KEEP_PER_EVENT, 112, 112, 3)
        assert str(d["outcome"]) == "entered" and list(d["names"]) == [""] * KEEP_PER_EVENT
    assert w.keep("A", "entered") is None  # taken: kept once
    assert w.keep("nobody", "entered") is None


def test_old_faces_and_candidates_are_forgotten(tmp_path):
    seen = []
    now = [1000.0]
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(1)),
        lambda: [("A", 0, None)],
        seen,
        clock=lambda: now[0],
    )
    w.look(1000.0, Frame())
    w.look(1000.0, Frame())
    path = w.keep("A", "household")
    w.look(1000.0, Frame())  # B's faces, never kept
    os.utime(path, (1000.0, 1000.0))
    now[0] = 1000.0 + 15 * 86400
    w.prune()
    assert not path.exists()
    assert w._faces == {}


def test_kept_faces_carry_a_sharper_picture_from_the_full_frame(tmp_path):
    seen = []
    w = watcher(
        tmp_path, Detector(face_at(100, 50, 30)), Embedder(unit(1)), lambda: [("A", 0, None)], seen
    )
    w.look(1000.0, Frame())
    w.keep("A", "entered")
    (face,) = face_admin.load_candidates(tmp_path / "candidates")
    # The 120 px face, 1.6x wider: 192 px from the 2560x1920 frame (not 112 aligned).
    assert face["display"] == b"JPEG 192x192"
    assert face["face"].shape == (112, 112, 3)


def test_a_big_face_is_scaled_down_and_kept_inside_the_frame():
    full = np.zeros((1920, 2560, 3), np.uint8)
    big = Face((2400, 1700, 400, 400), np.zeros((5, 2)), 0.9)  # at the corner
    crop = display_crop(full, big)
    assert crop.shape[0] == crop.shape[1] and crop.shape[0] <= 320 and crop.shape[0] % 2 == 0


class Watching(Tap):
    """A tap whose frames come while someone's on camera: keep() is called mid-watch."""

    def __init__(self, frames, during):
        super().__init__(frames)
        self.during = during

    def frames(self, since, every, stop):
        for k, (at, frame) in enumerate(self.frames_given):
            if k == 1:
                self.during()
            yield at, frame


def test_faces_seen_after_the_decision_are_kept_too(tmp_path):
    seen = []
    w = None

    def decide():  # e.g. "returned": decided while they're still walking up
        assert w.keep("A", "returned") is None  # not yet: they're still on camera

    tap = Watching([(1000.0 + k, Frame()) for k in range(4)], decide)
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(0, 1)),
        lambda: [("A", 999.0, None)],
        seen,
        tap=tap,
    )
    w._watch_once()
    (path,) = (tmp_path / "candidates").glob("*.npz")
    with np.load(path) as d:
        assert len(d["embeddings"]) == 4 and str(d["outcome"]) == "returned"


def test_capturing_keeps_whoever_walks_up(tmp_path):
    seen = []
    now = [1000.0]
    tap = Tap([(1000.0 + k, Frame()) for k in range(3)])
    w = watcher(
        tmp_path,
        Detector(face_at(100, 50, 30)),
        Embedder(unit(0, 1)),
        lambda: [("B", 999.0, None)],
        seen,
        tap=tap,
        clock=lambda: now[0],
    )
    assert w.capturing() == 0
    w.capture(120)
    assert w.capturing() == 120
    w._watch_once()
    (path,) = (tmp_path / "candidates").glob("*.npz")
    with np.load(path) as d:
        assert str(d["outcome"]) == "capture" and len(d["embeddings"]) == 3
    now[0] += 121
    assert w.capturing() == 0
