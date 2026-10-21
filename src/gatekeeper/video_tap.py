"""The camera's high-resolution video, kept a few seconds back and decoded on demand.

Reading the stream's compressed packets costs ~1.5% of a core on the rig; decoding
the 2560x1920 picture ~65%. So the packets of the last ``BUFFER_SECONDS`` are kept,
and only decoded while someone is on camera (face_watch.py): from the keyframe
before they appeared, so the first frames show them walking up, and then live
until they've gone. On the Reolink a keyframe comes every 2 s, which is also why
a reader started only once someone appears would see them too late.

Packets are placed on time.time(), when they arrive (as Frigate's events are).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from gatekeeper.audio import RTSP_IO_TIMEOUT_US

log = logging.getLogger(__name__)

BUFFER_SECONDS = 4.0
MAX_BACKOFF_SECONDS = 30.0
WAIT_SECONDS = 0.5  # how long frames() waits for a packet before checking stop()


@dataclass
class _Packet:
    seq: int  # numbered from the tap's start
    at: float  # time.time() it arrived
    serial: int  # which connection it came from
    keyframe: bool
    data: Any  # the av.Packet


def open_rtsp(url: str):
    import av

    return av.open(
        url, options={"rtsp_transport": "tcp", "timeout": str(RTSP_IO_TIMEOUT_US)}, timeout=10
    )


def make_decoder(stream):
    import av

    ctx = av.CodecContext.create(stream.codec_context.name, "r")
    ctx.extradata = stream.codec_context.extradata
    ctx.thread_type = "AUTO"
    return ctx


class VideoTap:
    def __init__(
        self,
        rtsp_url: str,
        *,
        seconds: float = BUFFER_SECONDS,
        opener: Callable[[str], Any] = open_rtsp,
        decoder: Callable[[Any], Any] = make_decoder,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.rtsp_url = rtsp_url
        self.seconds = seconds
        self._opener = opener
        self._decoder = decoder
        self._clock = clock
        self._sleep = sleep
        self._cond = threading.Condition()
        self._packets: deque[_Packet] = deque()
        self._seq = 0
        self._serial = 0
        self._stream = None  # the current connection's video stream (for the decoder)
        self._stopping = False

    # -- reading ---------------------------------------------------------------

    def run_forever(self) -> None:
        backoff = 1.0
        while not self._stopping:
            started = self._clock()
            try:
                self._read_once()
            except Exception as e:
                log.warning("video tap: %s", e)
            if self._stopping:
                break
            if self._clock() - started > 60:
                backoff = 1.0
            log.warning("video tap stream ended; reconnecting in %.0f s", backoff)
            self._sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def stop(self) -> None:
        self._stopping = True

    def _read_once(self) -> None:
        container = self._opener(self.rtsp_url)
        try:
            stream = container.streams.video[0]
            with self._cond:
                self._serial += 1
                self._stream = stream
                self._packets.clear()  # a new connection: the old packets don't decode on
            log.info("video tap reading %s", self.rtsp_url.split("@")[-1])
            for packet in container.demux(stream):
                if self._stopping:
                    break
                if packet.size:
                    self.add(packet, packet.is_keyframe)
        finally:
            container.close()

    def add(self, packet, keyframe: bool) -> None:
        """Keep a packet that just arrived; drop those no longer needed (older than
        ``seconds``, but always from a keyframe on, so the oldest kept decodes)."""
        now = self._clock()
        with self._cond:
            self._seq += 1
            self._packets.append(_Packet(self._seq, now, self._serial, keyframe, packet))
            cutoff = now - self.seconds
            while len(self._packets) > 1 and self._packets[0].at < cutoff:
                next_key = next((i for i, p in enumerate(self._packets) if i and p.keyframe), None)
                if next_key is None or self._packets[next_key].at > cutoff:
                    break
                for _ in range(next_key):
                    self._packets.popleft()
            self._cond.notify_all()

    # -- decoding --------------------------------------------------------------

    def frames(
        self, since: float, every: float, stop: Callable[[], bool]
    ) -> Iterator[tuple[float, Any]]:
        """Decoded frames (time, av.VideoFrame), at most one per ``every`` seconds: from
        the keyframe at or before ``since`` (the oldest kept, if none), then live until
        ``stop()``. After a reconnect it carries on from the new stream's first keyframe."""
        decoder, serial, seq, next_at = None, None, None, 0.0
        while not stop():
            with self._cond:
                if decoder is None or serial != self._serial:
                    start = self._start(since if decoder is None else 0.0)
                    if start is None:
                        self._cond.wait(WAIT_SECONDS)
                        continue
                    decoder = self._decoder(self._stream)
                    serial, seq = self._serial, start.seq
                pending = [p for p in self._packets if p.seq >= seq]
                if pending and pending[0].seq > seq:
                    # Fell more than the buffer behind: carry on from the next keyframe.
                    pending = pending[
                        next((i for i, p in enumerate(pending) if p.keyframe), len(pending)) :
                    ]
                    if pending:
                        decoder = self._decoder(self._stream)
                if not pending:
                    self._cond.wait(WAIT_SECONDS)
                    continue
                seq = pending[-1].seq + 1
            for p in pending:
                try:
                    decoded = decoder.decode(p.data)
                except Exception as e:  # a corrupt packet: carry on from the next keyframe
                    log.debug("video tap: undecodable packet: %s", e)
                    decoded = []
                for frame in decoded:
                    if p.at >= next_at:
                        next_at = p.at + every
                        yield p.at, frame
                if stop():
                    return

    def _start(self, since: float) -> _Packet | None:
        """The keyframe to decode from: the last at or before ``since``, else the first."""
        keys = [p for p in self._packets if p.keyframe]
        if not keys:
            return None
        before = [p for p in keys if p.at <= since]
        return before[-1] if before else keys[0]
