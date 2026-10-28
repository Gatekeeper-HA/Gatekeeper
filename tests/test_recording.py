import json
from types import SimpleNamespace

import numpy as np
import pytest

from gatekeeper.own_perception import Track
from gatekeeper.perception import PersonEvent
from gatekeeper.recording import Recorder, track_start

av = pytest.importorskip("av")

FPS = 10


def camera(path, seconds=8, gop=FPS):
    """A camera's stream as a file: an H.264 frame each 1/FPS s, a keyframe each second."""
    out = av.open(str(path), "w")
    s = out.add_stream("h264", rate=FPS)
    s.width, s.height, s.pix_fmt, s.gop_size = 64, 48, "yuv420p", gop
    s.options = {"bf": "0", "g": str(gop), "keyint_min": str(gop)}
    for n in range(seconds * FPS):
        frame = av.VideoFrame.from_ndarray(np.full((48, 64, 3), (n * 3) % 255, np.uint8))
        frame.pts = n
        for p in s.encode(frame):
            out.mux(p)
    for p in s.encode(None):
        out.mux(p)
    out.close()


class Video:
    """The video tap over a file: packet n arrived at ``start + n / FPS``."""

    def __init__(self, path, start, clock, at=None):
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        self.list = [
            SimpleNamespace(at=start + n / FPS, keyframe=p.is_keyframe, data=p)
            for n, p in enumerate(q for q in self.container.demux(self.stream) if q.size)
        ]
        self.clock, self.at = clock, at or {}

    def packets(self, since, stop):
        keys = [i for i, p in enumerate(self.list) if p.keyframe and p.at <= since]
        for p in self.list[keys[-1] if keys else 0 :]:
            self.clock[0] = p.at
            for t in [t for t in self.at if t <= p.at]:
                self.at.pop(t)()  # what happens then (someone leaves)
            if stop():
                return
            yield p, self.stream


class Audio:
    """The microphone: a tone, on a monotonic clock 500 s behind the wall clock."""

    def healthy(self):
        return True

    def slice(self, t0, t1):
        n = round((t1 - t0) * 16000)
        return (3000 * np.sin(np.arange(n) / 5)).astype(np.int16)


def test_a_clip_with_sound_and_a_picture_of_each_person(tmp_path):
    camera(tmp_path / "camera.mp4")
    clock = [1003.2]  # they've just been seen
    track = Track("1003.000000-own1", (0.5, 0.5, 0.7, 0.9), 1003.0, 1005.0, best=0.9)
    track.best_at, track.entered = 1004.5, ["porch"]
    recorder = Recorder(
        None,
        Audio(),
        tmp_path / "clips",
        "front_door",
        pre=2.0,
        post=1.0,
        clock=lambda: clock[0],
        mono=lambda: clock[0] - 500,
    )

    def leaves():
        recorder.on_track(track)
        recorder.on_event(PersonEvent("end", track.id, "front_door", "person"))

    recorder.video = Video(tmp_path / "camera.mp4", 1000.0, clock, at={1006.0: leaves})
    recorder.on_event(PersonEvent("new", track.id, "front_door", "person"))
    path = recorder.record(track_start(PersonEvent("new", track.id, "x", "person")) - 2.0)

    # From the keyframe 2 s before they appeared until 1 s after they'd gone.
    assert path == tmp_path / "clips" / path.parent.name / "1001.000.mp4"
    index = json.loads(path.with_suffix(".json").read_text())
    assert index["start"] == pytest.approx(1001.0) and index["end"] == pytest.approx(1006.9)
    assert index["camera"] == "front_door" and index["audio"] is True
    (person,) = index["people"]
    assert person["track"] == track.id and person["zones"] == ["porch"]
    picture = path.with_name(person["picture"])
    assert picture.read_bytes()[:2] == b"\xff\xd8"
    with av.open(str(path)) as clip:
        video, sound = clip.streams.video[0], clip.streams.audio[0]
        assert (video.codec_context.name, sound.codec_context.name) == ("h264", "aac")
        frames = sum(1 for _ in clip.decode(video))
    assert frames == 60  # 1001.0 to 1006.9 at 10 fps
    assert recorder.clips == 1


def test_who_is_there(tmp_path):
    recorder = Recorder(None, None, tmp_path, "front_door", post=2.0, clock=lambda: 50.0)
    recorder.on_event(PersonEvent("new", "40.000000-own1", "front_door", "person"))
    recorder.on_event(PersonEvent("update", "45.500000-own2", "front_door", "person"))
    assert recorder._present == {"40.000000-own1": 40.0, "45.500000-own2": 45.5}
    assert not recorder._done(started=50.0)
    for track in ("40.000000-own1", "45.500000-own2"):
        recorder.on_event(PersonEvent("end", track, "front_door", "person"))
    assert not recorder._done(started=50.0)  # for post seconds more
    recorder._clock = lambda: 52.0
    assert recorder._done(started=50.0)
    recorder.stop(wait=0)
    assert recorder._done(started=52.0)
