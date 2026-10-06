import asyncio
import wave

import numpy as np
import pytest

from gatekeeper.talkback import FRAME_SAMPLES, RATE, QueueTrack, read_wav


class Clock:
    """A clock that only moves when the track sleeps, so pacing is exact."""

    def __init__(self):
        self.t = 50.0
        self.slept = []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def frames(track, n):
    async def go():
        return [await track.next_samples() for _ in range(n)]

    return go


def test_silence_when_nothing_is_queued_paced_at_20_ms():
    clock = Clock()
    track = QueueTrack(clock=clock, sleep=clock.sleep)
    out = asyncio.run(frames(track, 5)())
    assert all(not s.any() for _, s in out)
    assert [pts for pts, _ in out] == [i * FRAME_SAMPLES for i in range(5)]
    assert clock.slept == pytest.approx([0.02] * 4)


def test_clips_play_back_to_back_and_report_when_they_were_sent():
    clock = Clock()
    track = QueueTrack(clock=clock, sleep=clock.sleep)

    async def go():
        first = track.enqueue(np.full(int(0.05 * RATE), 1, dtype=np.int16))  # 2.5 frames
        second = track.enqueue(np.full(int(0.03 * RATE), 2, dtype=np.int16))  # 1.5 frames
        out = [await track.next_samples() for _ in range(6)]
        return first, second, out

    first, second, out = asyncio.run(go())
    samples = np.concatenate([s for _, s in out])
    n1, n2 = int(0.05 * RATE), int(0.03 * RATE)
    assert np.all(samples[:n1] == 1)
    assert np.all(samples[n1 : n1 + n2] == 2)  # no gap between clips
    assert not samples[n1 + n2 :].any()  # then silence
    assert first.started.result() == pytest.approx(50.0)
    assert first.sent.result() == pytest.approx(50.04)  # its last frame is the 3rd
    assert second.started.result() == pytest.approx(50.04)
    assert second.sent.result() == pytest.approx(50.06)
    assert track.idle()


def test_a_clip_queued_later_starts_on_the_next_frame():
    clock = Clock()
    track = QueueTrack(clock=clock, sleep=clock.sleep)

    async def go():
        for _ in range(10):  # 200 ms of silence first
            await track.next_samples()
        playback = track.enqueue(np.ones(FRAME_SAMPLES, dtype=np.int16))
        await track.next_samples()
        return playback

    playback = asyncio.run(go())
    assert playback.started.result() == pytest.approx(50.2)
    assert playback.sent.result() == pytest.approx(50.2)
    assert playback.seconds == pytest.approx(0.02)


def write(path, samples, rate, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())
    return path


def test_read_wav_resamples_and_downmixes_to_24k_mono(tmp_path):
    assert len(read_wav(write(tmp_path / "a.wav", np.ones(24000), 24000))) == 24000
    assert len(read_wav(write(tmp_path / "b.wav", np.ones(16000), 16000))) == 24000
    stereo = np.tile([100, 300], 24000)
    mono = read_wav(write(tmp_path / "c.wav", stereo, 24000, channels=2))
    assert len(mono) == 24000 and np.all(mono == 200)
