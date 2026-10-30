import threading

import numpy as np
import pytest

from gatekeeper import own_perception as own
from gatekeeper.config import Settings
from gatekeeper.own_perception import Detection, Motion, OwnPerception, Tracker

PORCH = [(0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]
DOOR = [(0.8, 0.5), (1.0, 0.5), (1.0, 1.0), (0.8, 1.0)]


def person(x0, y0, x1, y1, score=0.8):
    return Detection((x0, y0, x1, y1), score)


# -- the detector's output ----------------------------------------------------


def test_the_grid_covers_strides_8_16_32():
    grid_xy, stride = own.grid(416)
    assert len(grid_xy) == 52 * 52 + 26 * 26 + 13 * 13
    assert stride[0] == 8 and stride[52 * 52] == 16 and stride[-1] == 32
    assert tuple(grid_xy[53]) == (1, 1)  # row-major: x across, then y


def test_letterbox_scales_into_the_top_left_and_pads_with_grey():
    bgr = np.full((480, 640, 3), 200, np.uint8)
    image, r = own.letterbox(bgr, 416)
    assert image.shape == (3, 416, 416) and r == pytest.approx(0.65)
    assert image[:, :312].min() == 200 and image[:, 312:].max() == 114


def raw(rows, size=416):
    """YOLOX's output with ``rows`` set, each as
    {index: (dx, dy, log w, log h, objectness, class, score)}."""
    grid_xy, _ = own.grid(size)
    out = np.zeros((len(grid_xy), 85), np.float32)
    for i, (dx, dy, lw, lh, obj, cls, score) in rows.items():
        out[i, :5] = (dx, dy, lw, lh, obj)
        out[i, 5 + cls] = score
    return out


def test_decode_finds_people_and_drops_duplicates_and_other_things():
    grid_xy, stride = own.grid(416)
    i = 52 * 52 + 8 * 26 + 10  # stride 16, cell (10, 8): centre (168, 136) at 416
    out = raw(
        {
            i: (0.5, 0.5, np.log(4), np.log(8), 0.9, 0, 0.9),  # 64 x 128 px
            i + 1: (0.0, 0.5, np.log(4), np.log(8), 0.8, 0, 0.8),  # the same person
            100: (0.5, 0.5, 0.0, 0.0, 0.9, 2, 0.9),  # a car
            200: (0.5, 0.5, 0.0, 0.0, 0.2, 0, 0.5),  # too unsure
        }
    )
    # A 640x480 frame, scaled by 0.65 to 416x312.
    found = own.decode(out, grid_xy, stride, 0.65, 640, 480, 0.25)
    assert len(found) == 1
    (x0, y0, x1, y1), score = found[0].box, found[0].score
    assert score == pytest.approx(0.81)
    assert (x0, y0, x1, y1) == pytest.approx(
        ((168 - 32) / 416, (136 - 64) / 312, (168 + 32) / 416, (136 + 64) / 312), abs=1e-4
    )
    assert found[0].foot == pytest.approx((0.4038, 0.6410), abs=1e-3)


def test_nms_keeps_the_best_of_overlapping_boxes():
    boxes = np.array([[0, 0, 1, 1], [0.05, 0, 1, 1], [2, 2, 3, 3]], float)
    assert own.nms(boxes, np.array([0.5, 0.9, 0.4]), 0.5) == [1, 2]


def test_part_of_a_bigger_box_isnt_another_person():
    boxes = np.array(
        [
            [0.58, 0.55, 0.96, 0.99],  # bending over a bag at the door
            [0.57, 0.66, 0.77, 1.0],  # their upper body: 0.85 inside, but it scores higher
            [0.1, 0.2, 0.3, 0.9],  # someone else
            [0.25, 0.3, 0.45, 0.9],  # beside them, overlapping a little
        ]
    )
    assert own.without_parts(boxes, [1, 0, 2, 3]) == [0, 2, 3]
    assert own.without_parts(boxes, [1]) == [1]
    same = np.array([[0.1, 0.1, 0.2, 0.2], [0.1, 0.1, 0.2, 0.2]])
    assert own.without_parts(same, [0, 1]) == [0, 1]  # neither is bigger


def test_decode_drops_part_of_someone():
    grid_xy, stride = own.grid(416)
    i = 52 * 52 + 8 * 26 + 10  # stride 16, cell (10, 8): centre (168, 136) at 416
    out = raw(
        {
            i: (0.5, 0.5, np.log(4), np.log(8), 0.9, 0, 0.8),  # 64 x 128 px
            17 * 52 + 21: (0.0, 0.0, np.log(2), np.log(2), 0.9, 0, 0.9),  # 16 x 16 px inside it
        }
    )
    found = own.decode(out, grid_xy, stride, 0.65, 640, 480, 0.25)
    assert [round(d.score, 2) for d in found] == [0.72]


def test_fit_is_the_size_before_padding():
    class Fake(own.Yolox):
        def __init__(self):
            self.size = 416

    assert Fake().fit(640, 480) == (416, 312)
    assert Fake().fit(480, 640) == (312, 416)


# -- the motion check ---------------------------------------------------------


def test_motion_is_a_change_on_part_of_the_frame():
    motion = Motion()
    still = np.full((312, 416, 3), 80, np.uint8)
    assert not motion.update(still)  # the first frame is the background
    assert not motion.update(still.copy())
    noisy = still + np.random.default_rng(1).integers(0, 10, still.shape, dtype=np.uint8)
    assert not motion.update(noisy)  # sensor noise
    walker = still.copy()
    walker[100:200, 200:240] = 220
    assert motion.update(walker)


def test_the_background_follows_a_lasting_change():
    motion = Motion()
    frame = np.full((120, 160, 3), 80, np.uint8)
    motion.update(frame)
    brighter = frame + 60  # the porch light came on
    assert motion.update(brighter)
    for _ in range(60):
        moved = motion.update(brighter)
    assert not moved


# -- tracking -----------------------------------------------------------------


def walk(tracker, boxes, start=100.0, every=0.25):
    """Each item (a detection, a list of them, or None) one frame apart; the events."""
    events = []
    for n, item in enumerate(boxes):
        detections = [] if item is None else item if isinstance(item, list) else [item]
        events += tracker.update(detections, start + n * every)
    return events


def test_a_person_is_reported_from_the_second_detection():
    tracker = Tracker("front_door")
    events = walk(tracker, [person(0.1, 0.2, 0.2, 0.6), person(0.11, 0.2, 0.21, 0.6)])
    assert [e.type for e in events] == ["new"]
    assert events[0].camera == "front_door" and events[0].label == "person"
    assert events[0].id.endswith("-own1") and events[0].id.startswith("100.000000")


def test_one_stray_box_is_nobody():
    tracker = Tracker("front_door")
    events = walk(tracker, [person(0.1, 0.2, 0.2, 0.6)] + [None] * 30)
    assert events == [] and tracker.ended == [] and not tracker.active


def test_a_track_moves_through_zones_and_ends():
    zones = {"porch": PORCH, "door": DOOR, "driveway": None}  # driveway: not loaded yet
    tracker = Tracker("front_door", lambda: zones, max_age=1.0, zone_seconds=0)
    feet = [(0.35 + 0.1 * n, 0.45 + 0.08 * n) for n in range(7)]  # up the steps to the door
    steps = [person(x - 0.1, y - 0.4, x + 0.1, y) for x, y in feet]
    events = walk(tracker, steps + [None] * 6)
    assert [e.type for e in events] == ["new"] + ["update"] * 5 + ["end"]
    assert events[0].entered_zones == () and events[1].entered_zones == ("porch",)
    assert events[-1].entered_zones == ("porch", "door")
    assert events[-1].current_zones == ("porch", "door")
    path = events[-1].path  # point by point: pytest.approx doesn't take nested lists
    assert len(path) == len(feet)
    assert all(p == pytest.approx(f) for p, f in zip(path, feet, strict=True))
    (track,) = tracker.ended
    assert track.first == 100.0 and track.last == 101.5 and track.hits == 7


def test_a_zone_counts_after_its_inertia():
    tracker = Tracker("front_door", lambda: {"porch": PORCH, "door": DOOR})
    # Frames 0.25 s apart. On the porch throughout; at the door's edge for 2 frames (cut
    # off at the bottom of the frame, close to the camera): 0.25 s, less than 0.3 s; then
    # back, then at the door for 3 (0.5 s).
    feet = [(0.7, 0.7), (0.72, 0.7), (0.81, 0.72), (0.82, 0.72), (0.75, 0.72), (0.81, 0.72)]
    feet += [(0.83, 0.72), (0.85, 0.72)]
    events = walk(tracker, [person(x - 0.1, y - 0.4, x + 0.1, y) for x, y in feet])
    by_frame = [(e.entered_zones, e.current_zones) for e in events]
    assert by_frame[0] == ((), ())  # "new", on the second frame
    assert ("porch",) in [entered for entered, _ in by_frame]
    assert all("door" not in entered for entered, _ in by_frame[:-1])
    assert len(tracker.tracks) == 1
    assert by_frame[-1] == (("porch", "door"), ("porch", "door"))


def test_standing_still_is_no_update():
    tracker = Tracker("front_door")
    events = walk(tracker, [person(0.1, 0.2, 0.2, 0.6)] * 10)
    assert [e.type for e in events] == ["new"]


def test_two_people_are_two_tracks():
    tracker = Tracker("front_door")
    a = [person(0.1 + 0.01 * n, 0.2, 0.2 + 0.01 * n, 0.6) for n in range(3)]
    b = [person(0.7 - 0.01 * n, 0.2, 0.8 - 0.01 * n, 0.6) for n in range(3)]
    events = walk(tracker, [[x, y] for x, y in zip(a, b, strict=True)])
    assert sorted({e.id.split("-")[1] for e in events}) == ["own1", "own2"]


def test_someone_walking_fast_stays_one_track():
    tracker = Tracker("front_door")
    # Each frame the box moves by its width: no overlap, but well within half its height.
    events = walk(tracker, [person(0.1 + 0.1 * n, 0.2, 0.2 + 0.1 * n, 0.8) for n in range(4)])
    assert [e.type for e in events] == ["new", "update", "update"]
    assert len(tracker.tracks) == 1


def test_someone_coming_out_from_behind_a_pillar_stays_one_track():
    # Their head over the pillar, then nothing for a few frames, then all of them: lower
    # and bigger, too far for half the small box's height.
    tracker = Tracker("front_door")
    head = [person(0.45, 0.54, 0.5, 0.61) for _ in range(4)]
    whole = [person(0.48, 0.56, 0.53, 0.79) for _ in range(4)]
    events = walk(tracker, head + [None] * 3 + whole)
    assert [e.type for e in events] == ["new", "update"]
    assert len(tracker.tracks) == 1


def test_summary_line():
    tracker = Tracker("front_door", lambda: {"porch": PORCH}, zone_seconds=0)
    walk(tracker, [person(0.6, 0.3, 0.7, 0.6, 0.7), person(0.6, 0.3, 0.7, 0.9, 0.9)] + [None] * 22)
    (track,) = tracker.ended
    assert own.summary(track) == (
        "own perception: person track 100.000000-own1 start=100.0 end=100.2 zones=porch "
        "path=(0.65,0.60)->(0.65,0.90) best=0.90 hits=2"
    )


# -- the service --------------------------------------------------------------


class Detector:
    def __init__(self, found=()):
        self.found = list(found)
        self.calls = 0

    def fit(self, width, height):
        return width // 2, height // 2

    def detect(self, bgr):
        self.calls += 1
        return self.found


def frame(level=80):
    return np.full((120, 160, 3), level, np.uint8)


def test_the_detector_only_runs_on_motion_or_while_someone_is_tracked():
    detector = Detector()
    service = OwnPerception("rtsp://go2rtc:8554/front_door_sub", detector, Tracker("x"))
    service.look(frame(), 1.0)
    service.look(frame(), 2.0)
    assert detector.calls == 0
    moved = frame()
    moved[20:80, 40:60] = 230
    detector.found = [person(0.25, 0.15, 0.4, 0.7)]
    service.look(moved, 3.0)
    service.look(moved, 3.2)  # no motion now, but someone is tracked
    assert detector.calls == 2 and service.tracker.active
    assert (service.frames, service.detected) == (4, 2)


def test_events_and_ended_tracks_reach_the_callbacks():
    seen, tracks = [], []
    detector = Detector([person(0.25, 0.15, 0.4, 0.7)])
    service = OwnPerception(
        "rtsp://x", detector, Tracker("x"), on_event=seen.append, on_track=tracks.append
    )
    moved = frame()
    moved[20:80, 40:60] = 230
    service.look(frame(), 1.0)
    service.look(moved, 1.2)
    service.look(moved, 1.4)
    detector.found = []
    service.look(moved, 9.0)
    assert [e.type for e in seen] == ["new", "end"]
    assert len(tracks) == 1 and service.tracker.ended == []


def test_a_failing_callback_doesnt_stop_it():
    def boom(event):
        raise RuntimeError("boom")

    detector = Detector([person(0.25, 0.15, 0.4, 0.7)])
    service = OwnPerception("rtsp://x", detector, Tracker("x", min_hits=1), on_event=boom)
    moved = frame()
    moved[20:80, 40:60] = 230
    service.look(frame(), 1.0)
    assert [e.type for e in service.look(moved, 1.2)] == ["new"]


def test_stopping_reports_the_tracks_still_open():
    # Gatekeeper restarting (a deploy) while someone is at the door: still seen.
    seen, tracks = [], []
    detector = Detector([person(0.25, 0.15, 0.4, 0.7)])
    service = OwnPerception(
        "rtsp://x", detector, Tracker("x"), on_event=seen.append, on_track=tracks.append
    )
    moved = frame()
    moved[20:80, 40:60] = 230
    service.look(frame(), 1.0)
    service.look(moved, 1.2)  # one stray box: not reported
    service.look(moved, 1.4)
    service.tracker.update([person(0.7, 0.1, 0.8, 0.3)], 1.6)  # someone else, seen once
    service.stop()
    assert [e.type for e in seen] == ["new", "end"]
    assert [t.last for t in tracks] == [1.4]
    assert not service.tracker.active


def test_tracks_end_when_the_stream_stops():
    tracks = []
    clock = [10.0]
    service = OwnPerception(
        "rtsp://x",
        Detector([person(0.25, 0.15, 0.4, 0.7)]),
        Tracker("x"),
        on_track=tracks.append,
        clock=lambda: clock[0],
    )
    moved = frame()
    moved[20:80, 40:60] = 230
    service.look(frame(), 9.0)
    service.look(moved, 9.2)
    service.look(moved, 9.4)
    clock[0] = 20.0
    service.end_stale()
    assert len(tracks) == 1 and not service.tracker.active


class VideoFrame:
    def __init__(self, level, width=640, height=480):
        self.level, self.width, self.height = level, width, height
        self.asked = None

    def to_ndarray(self, **kwargs):
        self.asked = kwargs
        return np.full((kwargs["height"], kwargs["width"], 3), self.level, np.uint8)


class Container:
    def __init__(self, frames):
        self.frames = frames
        self.closed = False

        class Codec:
            thread_type = None

        class Stream:
            codec_context = Codec()

        class Streams:
            video = [Stream()]

        self.streams = Streams()

    def decode(self, video=0):
        yield from self.frames

    def close(self):
        self.closed = True


def test_reading_keeps_only_the_newest_frame():
    frames = [VideoFrame(n) for n in range(5)]
    container = Container(frames)
    times = iter(range(100, 200))
    service = OwnPerception(
        "rtsp://u:p@go2rtc:8554/front_door_sub",
        Detector(),
        Tracker("x"),
        opener=lambda url: container,
        clock=lambda: float(next(times)),
    )
    service._read_once()
    assert service._frame == (104.0, frames[-1]) and container.closed
    assert container.streams.video[0].codec_context.thread_type == "AUTO"


def test_looking_takes_the_newest_frame_scaled_for_the_detector():
    detector = Detector()
    clock = [100.0]
    slept = []
    service = OwnPerception(
        "rtsp://x", detector, Tracker("x"), fps=4, clock=lambda: clock[0], sleep=slept.append
    )
    looked = []
    service.look = lambda bgr, at, frame=None: (looked.append((bgr.shape, at)), service.stop())
    newest = VideoFrame(80)
    service._frame = (99.5, newest)
    worker = threading.Thread(target=service.look_forever)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert looked == [((240, 320, 3), 99.5)]
    assert slept == [0.25]  # 4 a second, from when it took the frame (however old)
    assert newest.asked == {
        "width": 320,
        "height": 240,
        "format": "bgr24",
        "interpolation": "BILINEAR",
    }


def test_looking_beats_on_each_pass_even_without_frames():
    beats = []
    service = OwnPerception("rtsp://x", Detector(), Tracker("x"), on_look=lambda: beats.append(1))

    class Waits(type(threading.Condition())):  # no frame comes: each wait times out
        def wait(self, timeout=None):
            if len(beats) == 2:
                service._stopping = True
            return False

    service._cond = Waits()
    worker = threading.Thread(target=service.look_forever)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert len(beats) == 3  # no frame came: each wait still ends in a heartbeat


# -- settings and wiring ------------------------------------------------------


def test_settings_default_to_off_and_the_sub_stream(monkeypatch):
    monkeypatch.delenv("OWN_PERCEPTION", raising=False)
    monkeypatch.delenv("OWN_PERCEPTION_URL", raising=False)
    monkeypatch.delenv("AUDIO_RTSP_URL", raising=False)
    settings = Settings()
    assert settings.own_perception == "off"
    assert settings.own_perception_url == "rtsp://go2rtc:8554/front_door_sub"
    monkeypatch.setenv("OWN_PERCEPTION", "shadow")
    monkeypatch.setenv("OWN_PERCEPTION_URL", "rtsp://cam/low")
    settings = Settings()
    assert (settings.own_perception, settings.own_perception_url) == ("shadow", "rtsp://cam/low")


def test_app_starts_it_with_the_zones_gatekeeper_uses(monkeypatch, tmp_path):
    from gatekeeper import app

    class Zone:
        def __init__(self, name):
            self.polygon = PORCH if name == "porch" else None

    class Perception:
        asked = []

        def zone(self, name):
            self.asked.append(name)
            return Zone(name)

    started = []
    monkeypatch.setattr(
        app,
        "start_thread",
        lambda name, target, *a: (
            started.append(name) or (target(*a) if name == "own-perception-start" else None)
        ),
    )
    monkeypatch.setattr(own, "fetch_model", lambda model, cache: tmp_path / "yolox_tiny.onnx")

    class Yolox:
        def __init__(self, path, threshold, threads, device):
            self.args, self.device = (path, threshold, threads, device), "cpu"

    monkeypatch.setattr(own, "Yolox", Yolox)
    settings = Settings(
        trigger_zones="porch", door_zone="door", package_zone="porch", model_dir=tmp_path
    )
    service = app.start_own_perception(settings, Perception())
    assert Perception.asked == ["porch", "door"]
    assert service.tracker._zones() == {"porch": PORCH, "door": None}
    assert service.detector.args == (tmp_path / "yolox_tiny.onnx", 0.3, 2, "auto")
    assert started == [
        "own-perception-start",
        "own-perception-read",
        "own-perception",
        "own-perception-watch",
    ]
    beats = []
    beating = app.start_own_perception(settings, Perception(), on_look=lambda: beats.append(1))
    beating._on_look()
    assert beats == [1]


def test_a_stalled_look_loop_says_where_it_is_stuck(caplog):
    # Once the loop has run, not passing for STALL_SECONDS is logged with where its
    # thread waits (here: on a lock nobody releases), once; and when it's back.
    service = OwnPerception("rtsp://x", Detector(), Tracker("x"))
    service.check_stall(100.0)  # never passed (the model loading): nothing to say
    stuck = threading.Event()

    def blocked_somewhere():
        service._look_thread = threading.get_ident()
        service._passed = 0.0
        stuck.wait(10)

    worker = threading.Thread(target=blocked_somewhere)
    worker.start()
    while service._passed is None:
        pass
    service.check_stall(own.STALL_SECONDS + 1)
    service.check_stall(own.STALL_SECONDS + 20)  # still stuck: said once
    stuck.set()
    worker.join()
    critical = [r for r in caplog.records if r.levelname == "CRITICAL"]
    assert len(critical) == 1
    assert "hasn't passed for 61 s" in critical[0].getMessage()
    assert "blocked_somewhere" in critical[0].getMessage()
    service._passed = 200.0
    service.check_stall(201.0)
    assert caplog.records[-1].getMessage() == "own perception looking again"
