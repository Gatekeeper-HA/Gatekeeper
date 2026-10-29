import datetime
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from gatekeeper.own_perception import Track
from gatekeeper.own_service import OwnService, is_own
from gatekeeper.perception import PersonEvent

OWN = "1800000100.000000-own3"
FRIGATES = "1800000000.123456-abc123"


class Frigate:
    """The fallback: says what it was asked."""

    def snapshot(self, event_id):
        return b"frigate " + event_id.encode()

    def event_snapshot(self, event_id, height=240):
        return b"frigate small " + event_id.encode()

    def event_path(self, event_id):
        return [((0.5, 0.5), 1.0)]

    def recording_frame(self, at):
        return b"frigate frame"


def tracked(track_id=OWN, first=1800000100.0, confirmed=True):
    track = Track(track_id, (0.5, 0.5, 0.6, 0.9), first, first + 4, confirmed=confirmed)
    track.path, track.times = [(0.55, 0.6), (0.6, 0.8), (0.62, 0.9)], [first, first + 1, first + 2]
    return track


def own_with(pictures=None, tracks=(), newest=None):
    return SimpleNamespace(
        pictures=dict(pictures or {}),
        tracker=SimpleNamespace(tracks=list(tracks)),
        newest=lambda: newest,
    )


def test_own_ids():
    assert is_own(OWN) and not is_own(FRIGATES) and not is_own("press-1800000000.000")


def test_person_events_go_to_the_visits():
    service = OwnService(zones=None)
    seen = []
    service.start(seen.append)
    event = PersonEvent("new", OWN, "front_door", "person", ("porch",))
    service.on_event(event)
    assert seen == [event]


def test_snapshots_are_each_persons_best_picture(tmp_path):
    service = OwnService(zones=None, fallback=Frigate(), clock=lambda: 1800000200.0)
    service.attach(own_with({OWN: b"best of own3"}))
    assert service.snapshot(OWN) == b"best of own3"
    assert service.snapshot(FRIGATES) == b"frigate " + FRIGATES.encode()  # from before
    assert service.snapshot("press-1800000300.000") is None  # no frame from the camera yet


def test_who_came_for_the_package_and_where_they_went():
    first = tracked("1800000050.000000-own1", first=1800000050.0)
    later = tracked(OWN, first=1800000100.0)
    service = OwnService(zones=None, fallback=Frigate())
    service.attach(
        own_with({first.id: b"one", OWN: b"three"}, tracks=[tracked("x-own9", confirmed=False)])
    )
    service.on_track(first)
    service.on_track(later)
    assert service.person_snapshot(1800000095.0) == b"three"
    assert service.person_snapshot(1800000000.0) == b"one"
    assert service.event_path(OWN) == [
        ((0.55, 0.6), 1800000100.0),
        ((0.6, 0.8), 1800000101.0),
        ((0.62, 0.9), 1800000102.0),
    ]
    assert service.event_path("1.0-own77") == []
    assert service.event_path(FRIGATES) == [((0.5, 0.5), 1.0)]


av = pytest.importorskip("av")


def jpeg(height=480, width=640, level=100):
    from gatekeeper.face_admin import jpeg_bytes

    return jpeg_bytes(np.full((height, width, 3), level, np.uint8))


def test_the_latest_frame_is_own_perceptions_newest(tmp_path):
    frame = av.VideoFrame.from_ndarray(np.full((480, 640, 3), 90, np.uint8), format="rgb24")
    now = [1800000000.0]
    service = OwnService(zones=None, clock=lambda: now[0])
    service.attach(own_with(newest=(1800000000.0 - 0.2, frame)))
    shot = service.latest_frame()
    assert shot.rgb.shape == (480, 640, 3) and shot.jpeg[:2] == b"\xff\xd8"
    assert service.snapshot("press-1800000000.000")[:2] == b"\xff\xd8"  # the camera now
    now[0] += 10  # the stream stopped
    assert service.latest_frame() is None


def test_the_visits_page_shows_the_recordings_picture_made_small(tmp_path):
    folder = tmp_path / "clips" / datetime.datetime.fromtimestamp(1800000100.0).strftime("%Y-%m-%d")
    folder.mkdir(parents=True)
    (folder / "1800000097.000-1.jpg").write_bytes(jpeg(1920, 2560))
    index = {"start": 1800000097.0, "end": 1800000110.0, "file": "1800000097.000.mp4"}
    index["people"] = [{"track": OWN, "picture": "1800000097.000-1.jpg"}]
    (folder / "1800000097.000.json").write_text(json.dumps(index))
    service = OwnService(zones=None, clip_dir=tmp_path / "clips", fallback=Frigate())
    service.attach(own_with({"1800000200.000000-own4": jpeg()}))
    small = service.event_snapshot(OWN, 240)
    with av.open(io.BytesIO(small)) as c:
        frame = next(c.decode(video=0))
    assert (frame.width, frame.height) == (320, 240)
    assert (folder / "1800000097.000-1-h240.jpg").read_bytes() == small  # kept for next time
    # No recording of them: their best picture, made small; Frigate's visits from Frigate.
    assert service.event_snapshot("1800000200.000000-own4", 240)[:2] == b"\xff\xd8"
    assert service.event_snapshot(FRIGATES) == b"frigate small " + FRIGATES.encode()
    assert service.event_snapshot("1800000300.000000-own5") is None


def test_full_resolution_frames_come_from_the_recordings(tmp_path):
    from test_recording import camera

    folder = tmp_path / "clips" / datetime.datetime.fromtimestamp(1800000100.0).strftime("%Y-%m-%d")
    folder.mkdir(parents=True)
    camera(folder / "1800000097.000.mp4")
    index = {"start": 1800000097.0, "end": 1800000105.0, "file": "1800000097.000.mp4"}
    (folder / "1800000097.000.json").write_text(json.dumps(index))
    service = OwnService(zones=None, clip_dir=tmp_path / "clips", fallback=Frigate())
    assert service.recording_frame(1800000100.5)[:2] == b"\xff\xd8"
    assert service.recording_frame(1800000200.0) == b"frigate frame"  # no clip then
