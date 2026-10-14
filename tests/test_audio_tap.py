import io
import wave

import numpy as np
import pytest

from gatekeeper import audio_tap
from gatekeeper.audio_tap import AudioTap, tap_command
from gatekeeper.vad import CHUNK_SAMPLES, SAMPLE_RATE

SR = SAMPLE_RATE


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class LoudnessVAD:
    """Probability 1.0 for a chunk containing any non-zero sample."""

    def __init__(self):
        self.pending = np.zeros(0, dtype=np.int16)

    def feed(self, samples):
        buf = np.concatenate([self.pending, samples])
        n = len(buf) // CHUNK_SAMPLES
        self.pending = buf[n * CHUNK_SAMPLES :]
        return [float(np.any(buf[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES])) for i in range(n)]


def make_tap(clock, seconds=2.0):
    tap = AudioTap("rtsp://cam/x", seconds=seconds, vad_factory=LoudnessVAD, clock=clock)
    tap._vad = LoudnessVAD()
    return tap


def arrive(tap, clock, seconds, value=0):
    """``seconds`` of audio arriving in real time, 0.1 s at a time."""
    for _ in range(round(seconds / 0.1)):
        clock.t += 0.1
        tap.ingest(np.full(SR // 10, value, dtype=np.int16))


def test_tap_command_reads_mono_16khz_pcm_with_a_socket_timeout():
    cmd = tap_command("rtsp://go2rtc:8554/front_door")
    assert cmd[cmd.index("-timeout") + 1] == str(audio_tap.RTSP_IO_TIMEOUT_US)
    assert cmd[cmd.index("-ar") + 1] == "16000" and cmd[cmd.index("-f") + 1] == "s16le"
    assert cmd[-1] == "-"


def test_samples_are_placed_on_the_clock_they_arrive_on():
    clock = Clock()
    tap = make_tap(clock)
    arrive(tap, clock, 0.5, 0)
    arrive(tap, clock, 0.3, 1000)  # speech from 100.5 to 100.8
    arrive(tap, clock, 0.2, 0)
    assert clock.t == pytest.approx(101.0)

    speech = tap.slice(100.5, 100.8)
    assert len(speech) == pytest.approx(0.3 * SR, abs=2)
    assert np.all(speech == 1000)
    assert np.all(tap.slice(100.8, 101.0) == 0)


def test_speech_probabilities_carry_chunk_end_times():
    clock = Clock()
    tap = make_tap(clock)
    arrive(tap, clock, 0.5, 0)
    arrive(tap, clock, 0.5, 1000)
    loud = [t for t, p in tap.probs_after(0) if p == 1.0]
    assert loud[0] == pytest.approx(100.5, abs=CHUNK_SAMPLES / SR)
    assert all(t > 100.6 for t, _ in tap.probs_after(100.6))


def test_the_buffer_keeps_only_the_last_seconds():
    clock = Clock()
    tap = make_tap(clock, seconds=2.0)
    arrive(tap, clock, 1.0, 7)
    arrive(tap, clock, 2.5, 9)  # wraps around: the 7s are gone
    everything = tap.slice(0, clock.t)
    assert len(everything) == 2 * SR
    assert np.all(everything == 9)


def test_a_gap_re_anchors_the_timeline():
    clock = Clock()
    tap = make_tap(clock)
    arrive(tap, clock, 0.5, 1)
    clock.t += 5.0  # stream stalled, then resumes
    arrive(tap, clock, 0.5, 2)
    assert np.all(tap.slice(clock.t - 0.5, clock.t) == 2)


def test_healthy_only_while_audio_keeps_arriving():
    clock = Clock()
    tap = make_tap(clock)
    assert not tap.healthy()
    arrive(tap, clock, 0.2)
    assert tap.healthy()
    clock.t += audio_tap.STALL_SECONDS + 0.1
    assert not tap.healthy()


def test_write_wav_saves_a_slice_and_skips_a_tiny_one(tmp_path):
    clock = Clock()
    tap = make_tap(clock)
    arrive(tap, clock, 1.0, 5)
    path = tap.write_wav(100.2, 100.8, tmp_path / "a.wav")
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels()) == (SR, 1)
        assert w.getnframes() == pytest.approx(0.6 * SR, abs=2)
    assert tap.write_wav(100.2, 100.3, tmp_path / "b.wav") is None


class FakeProc:
    def __init__(self, payload: bytes):
        self.stdout = io.BufferedReader(io.BytesIO(payload))
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def test_read_once_ingests_the_stream_including_odd_byte_reads():
    clock = Clock()
    samples = np.arange(1, 3 * CHUNK_SAMPLES + 1, dtype=np.int16)
    started = []

    def popen(cmd, **kw):
        started.append(cmd)
        return FakeProc(samples.tobytes() + b"\x01")  # trailing odd byte is dropped

    tap = AudioTap("rtsp://cam/x", vad_factory=LoudnessVAD, popen=popen, clock=clock)
    tap._read_once()
    assert started and started[0][-1] == "-"
    assert np.array_equal(tap.slice(0, clock.t + 1), samples)
    assert len(tap.probs_after(0)) == 3
