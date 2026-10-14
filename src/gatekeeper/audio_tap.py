"""The persistent audio tap: the camera's audio, read all the time.

One ffmpeg process reads the camera's audio stream from go2rtc (16 kHz mono)
into a ring buffer of the last ~30 s, and runs voice-activity detection on it
as it arrives. A visit then listens without opening a stream first, hears
what was said while it was still connecting, and knows when the visitor has
stopped talking instead of recording for a fixed time.

Samples are placed on time.monotonic(): the newest sample is "now" when it
arrives, and the timeline is re-anchored after a gap (a stall or reconnect).
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path

import numpy as np

from gatekeeper.audio import RTSP_IO_TIMEOUT_US
from gatekeeper.vad import CHUNK_SAMPLES, SAMPLE_RATE, StreamingVAD

log = logging.getLogger(__name__)

BUFFER_SECONDS = 30.0
# Re-anchor the timeline when arrival time and sample count disagree by more.
MAX_DRIFT_SECONDS = 0.5
# No audio for this long: unhealthy, and the ffmpeg reader is restarted.
STALL_SECONDS = 3.0
RESTART_AFTER_STALL_SECONDS = 10.0
READ_BYTES = 4096
MAX_BACKOFF_SECONDS = 30.0


def tap_command(rtsp_url: str) -> list[str]:
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-nostdin",
        "-rtsp_transport", "tcp",
        "-timeout", str(RTSP_IO_TIMEOUT_US),
        "-i", rtsp_url,
        "-vn",
        "-map", "0:a:0",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "s16le",
        "-",
    ]  # fmt: skip


class AudioTap:
    def __init__(
        self,
        rtsp_url: str,
        *,
        seconds: float = BUFFER_SECONDS,
        vad_factory: Callable[[], StreamingVAD] = StreamingVAD,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rtsp_url = rtsp_url
        self._capacity = int(seconds * SAMPLE_RATE)
        self._buf = np.zeros(self._capacity, dtype=np.int16)
        self._vad_factory = vad_factory
        self._popen = popen
        self._clock = clock
        self._cond = threading.Condition()
        self._written = 0  # samples received since the tap started
        self._anchor: tuple[int, float] | None = None  # (sample index, its time)
        self._last_audio: float | None = None
        self._probs: deque[tuple[float, float]] = deque(
            maxlen=int(seconds * SAMPLE_RATE / CHUNK_SAMPLES)
        )
        self._vad: StreamingVAD | None = None
        self._vad_index = 0  # sample index where the VAD's current stream began
        self._vad_chunks = 0
        self._proc: subprocess.Popen | None = None
        self._stopping = False

    # -- reading ---------------------------------------------------------------

    def run_forever(self) -> None:
        """Read the stream until stop(), restarting ffmpeg with backoff."""
        threading.Thread(target=self._watch_stalls, name="tap-watch", daemon=True).start()
        backoff = 1.0
        while not self._stopping:
            started = self._clock()
            try:
                self._read_once()
            except Exception:
                log.exception("audio tap reader failed")
            if self._stopping:
                break
            if self._clock() - started > 60:
                backoff = 1.0
            log.warning("audio tap stream ended; reconnecting in %.0f s", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def stop(self) -> None:
        self._stopping = True
        proc = self._proc
        if proc and proc.poll() is None:
            proc.kill()

    def _read_once(self) -> None:
        proc = self._popen(
            tap_command(self.rtsp_url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._proc = proc
        self._vad = self._vad_factory()
        with self._cond:
            self._vad_index, self._vad_chunks = self._written, 0
            self._anchor = None
        log.info("audio tap reading %s", self.rtsp_url.split("@")[-1])
        leftover = b""
        try:
            while True:
                data = proc.stdout.read1(READ_BYTES)
                if not data:
                    break
                data = leftover + data
                usable = len(data) - len(data) % 2
                leftover = data[usable:]
                if usable:
                    self.ingest(np.frombuffer(data[:usable], dtype=np.int16))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def _watch_stalls(self) -> None:
        while not self._stopping:
            time.sleep(1.0)
            proc, last = self._proc, self._last_audio
            if proc is None or proc.poll() is not None:
                continue
            if last is not None and self._clock() - last > RESTART_AFTER_STALL_SECONDS:
                log.warning("audio tap stalled for %.0f s; restarting it", self._clock() - last)
                proc.kill()

    def ingest(self, samples: np.ndarray) -> None:
        """Add samples that just arrived (the last one is "now")."""
        now = self._clock()
        probs = self._vad.feed(samples) if self._vad else []
        with self._cond:
            end = self._written + len(samples)
            if self._anchor is None or abs(self._time_at(end) - now) > MAX_DRIFT_SECONDS:
                self._anchor = (end, now)
            pos = self._written % self._capacity
            first = min(len(samples), self._capacity - pos)
            self._buf[pos : pos + first] = samples[:first]
            if first < len(samples):
                rest = samples[first:][-self._capacity :]
                self._buf[: len(rest)] = rest
            self._written = end
            for p in probs:
                self._vad_chunks += 1
                chunk_end = self._vad_index + self._vad_chunks * CHUNK_SAMPLES
                self._probs.append((self._time_at(chunk_end), p))
            self._last_audio = now
            self._cond.notify_all()

    # -- queries ---------------------------------------------------------------

    def _time_at(self, index: int) -> float:
        anchor_index, anchor_time = self._anchor
        return anchor_time + (index - anchor_index) / SAMPLE_RATE

    def healthy(self) -> bool:
        last = self._last_audio
        return last is not None and self._clock() - last <= STALL_SECONDS

    def probs_after(self, t: float) -> list[tuple[float, float]]:
        """(chunk end time, speech probability) for chunks ending after ``t``."""
        with self._cond:
            return [(ct, p) for ct, p in self._probs if ct > t]

    def wait(self, timeout: float) -> None:
        """Block until more audio arrives or ``timeout`` passes."""
        with self._cond:
            self._cond.wait(timeout)

    def slice(self, t0: float, t1: float) -> np.ndarray:
        """Samples between times ``t0`` and ``t1`` that are still in the buffer."""
        with self._cond:
            if self._anchor is None:
                return np.zeros(0, dtype=np.int16)
            oldest = max(0, self._written - self._capacity)
            anchor_index, anchor_time = self._anchor
            i0 = max(oldest, anchor_index + round((t0 - anchor_time) * SAMPLE_RATE))
            i1 = min(self._written, anchor_index + round((t1 - anchor_time) * SAMPLE_RATE))
            if i1 <= i0:
                return np.zeros(0, dtype=np.int16)
            idx = np.arange(i0, i1) % self._capacity
            return self._buf[idx].copy()

    def write_wav(self, t0: float, t1: float, path: Path) -> Path | None:
        samples = self.slice(t0, t1)
        if len(samples) < SAMPLE_RATE // 4:
            return None
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(samples.tobytes())
        return path
