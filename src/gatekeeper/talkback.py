"""Speaking through the camera speaker via a go2rtc WebRTC backchannel.

A TalkSession is one WebRTC connection to go2rtc's talk stream. Clips are
queued on it and played back to back, with silence in between, so a visit
connects once instead of once per phrase. Each queued clip's Playback says
when its first and last frames were actually sent, which is when the
visitor starts and (after the camera's own delay) stops hearing it.
"""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    import aiohttp
    from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame

    HAS_WEBRTC = True
except ImportError:
    HAS_WEBRTC = False
    MediaStreamTrack = object  # QueueTrack's queueing and pacing work without aiortc

log = logging.getLogger(__name__)

RATE = 24000  # Kokoro's output rate; aiortc resamples for Opus
FRAME_SAMPLES = RATE // 50  # 20 ms


@dataclass
class Playback:
    """A queued clip. ``started``/``sent`` resolve to the time.monotonic() at which
    its first and last frames went out."""

    seconds: float
    started: asyncio.Future
    sent: asyncio.Future


def read_wav(path: Path) -> np.ndarray:
    """A WAV's samples as 16-bit mono at RATE (linear resampling if needed)."""
    with wave.open(str(path), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path.name}: {8 * width}-bit audio, expected 16-bit")
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != RATE and len(samples):
        n = round(len(samples) * RATE / rate)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples
        ).astype(np.int16)
    return samples


class QueueTrack(MediaStreamTrack):
    """An outgoing audio track that plays queued clips in real time, and silence
    when there's nothing to play."""

    kind = "audio"

    def __init__(self, clock=time.monotonic, sleep=asyncio.sleep) -> None:
        super().__init__()
        self._clock = clock
        self._sleep = sleep
        self._queue: deque[tuple[Playback, np.ndarray]] = deque()
        self._current: tuple[Playback, np.ndarray] | None = None
        self._pos = 0
        self._pts = 0
        self._t0: float | None = None

    def enqueue(self, samples: np.ndarray) -> Playback:
        loop = asyncio.get_running_loop()
        playback = Playback(len(samples) / RATE, loop.create_future(), loop.create_future())
        self._queue.append((playback, samples))
        return playback

    def idle(self) -> bool:
        return self._current is None and not self._queue

    async def recv(self):
        pts, samples = await self.next_samples()
        frame = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, RATE)
        return frame

    async def next_samples(self) -> tuple[int, np.ndarray]:
        """The next 20 ms frame and its timestamp, once it's due."""
        # Real-time pacing: frame n goes out at t0 + n * 20 ms.
        if self._t0 is None:
            self._t0 = self._clock()
        else:
            wait = self._t0 + self._pts / RATE - self._clock()
            if wait > 0:
                await self._sleep(wait)

        frame = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        filled = 0
        now = self._clock()
        while filled < FRAME_SAMPLES:
            if self._current is None:
                if not self._queue:
                    break
                self._current, self._pos = self._queue.popleft(), 0
                _resolve(self._current[0].started, now)
            playback, samples = self._current
            take = min(FRAME_SAMPLES - filled, len(samples) - self._pos)
            frame[filled : filled + take] = samples[self._pos : self._pos + take]
            filled += take
            self._pos += take
            if self._pos >= len(samples):
                _resolve(playback.sent, now)
                self._current = None

        pts = self._pts
        self._pts += FRAME_SAMPLES
        return pts, frame


def _resolve(future: asyncio.Future, value: float) -> None:
    if not future.done():
        future.set_result(value)


class TalkSession:
    """One talkback connection to go2rtc; play() queues clips on it."""

    def __init__(self, go2rtc_api: str, talk_stream: str) -> None:
        self.url = f"{go2rtc_api.rstrip('/')}/api/webrtc?src={talk_stream}"
        self.track = QueueTrack()
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))

    async def open(self) -> bool:
        """Connect; False (and closed) on failure."""
        pc = self.pc
        pc.addTrack(self.track)
        await pc.setLocalDescription(await pc.createOffer())
        for _ in range(50):
            if pc.iceGatheringState == "complete":
                break
            await asyncio.sleep(0.1)

        log.info("talkback → %s", self.url)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    self.url,
                    data=pc.localDescription.sdp,
                    headers={"Content-Type": "application/sdp"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status >= 400:
                        log.error("go2rtc %s: %s", resp.status, await resp.text())
                        await self.close()
                        return False
                    answer_sdp = await resp.text()
        except Exception as e:
            log.error("talkback signaling failed: %s", e)
            await self.close()
            return False

        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
        for _ in range(100):
            if pc.connectionState in ("connected", "failed", "closed"):
                break
            await asyncio.sleep(0.1)
        if pc.connectionState != "connected":
            log.error("talkback never connected: %s", pc.connectionState)
            await self.close()
            return False
        log.info("talkback connected")
        return True

    def play(self, wav_path: Path) -> Playback:
        playback = self.track.enqueue(read_wav(wav_path))
        log.info("playing %s (%.1f s)", wav_path.name, playback.seconds)
        return playback

    async def close(self) -> None:
        await self.pc.close()
