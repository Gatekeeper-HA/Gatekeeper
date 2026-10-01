"""One visit: greet, listen, transcribe, classify, reply, log."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from gatekeeper import talkback
from gatekeeper.audio import Synthesizer, Transcriber, capture_audio_clip, wav_duration
from gatekeeper.classify import classify_response
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.logs import event_id_var

log = logging.getLogger(__name__)

# Keep the talkback session open this long after the clip ends, to cover
# go2rtc's jitter buffer, RTSP backchannel latency and the camera's buffer.
PLAYBACK_TAIL_SECONDS = 2.5
# Pause between the end of the greeting and the start of recording, so the
# room echo dies down.
ECHO_SETTLE_SECONDS = 0.5

# play(wav_path) -> an open connection with an async close(), or None on failure.
PlayFn = Callable[[Path], Awaitable[Any]]
CaptureFn = Callable[[str, Path, int], Path | None]


class Interaction:
    def __init__(
        self,
        settings: Settings,
        *,
        synth: Synthesizer,
        transcriber: Transcriber,
        presynth: dict[str, Path],
        event_log: EventLog,
        play: PlayFn | None = None,
        capture: CaptureFn = capture_audio_clip,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        webrtc_available: bool = talkback.HAS_WEBRTC,
    ) -> None:
        self.settings = settings
        self._synth = synth
        self._transcriber = transcriber
        self._presynth = presynth
        self._event_log = event_log
        self._play = play or self._play_via_go2rtc
        self._capture = capture
        self._sleep = sleep
        self._clock = clock
        self._webrtc_available = webrtc_available
        # go2rtc supports one talkback WebRTC session at a time.
        self._talkback_lock = threading.Lock()

    async def _play_via_go2rtc(self, wav_path: Path):
        return await talkback.play_wav(
            self.settings.go2rtc_api, self.settings.go2rtc_talk_stream, wav_path
        )

    def run(self, event_id: str) -> None:
        """Handle the visit for Frigate event ``event_id`` (blocking)."""
        token = event_id_var.set(event_id)
        try:
            log.info("interaction started")
            if not self._talkback_lock.acquire(blocking=False):
                log.warning("talkback busy, skipping")
                return
            try:
                asyncio.run(self._run_async(event_id))
            except Exception:
                log.exception("interaction error")
            finally:
                self._talkback_lock.release()
        finally:
            event_id_var.reset(token)

    async def _speak(self, key: str, text: str, fallback_path: Path):
        """Play a pre-synthesized phrase (or synthesize it now); return the open connection."""
        wav = self._presynth.get(key)
        if wav is None:
            wav = fallback_path
            await asyncio.to_thread(self._synth.synthesize, text, wav)
        return wav, await self._play(wav)

    async def _run_async(self, event_id: str) -> None:
        s = self.settings
        if not self._webrtc_available:
            log.error("aiortc not available")
            return

        # Greeting
        greet_wav, greet_pc = await self._speak(
            "greeting", s.greeting, s.out_dir / f"{event_id}_greeting.wav"
        )
        if greet_pc is None:
            return
        await self._sleep(wav_duration(greet_wav) + PLAYBACK_TAIL_SECONDS)
        await greet_pc.close()

        # Listen
        await self._sleep(ECHO_SETTLE_SECONDS)
        clip_path = await asyncio.to_thread(
            self._capture, s.audio_rtsp_url, s.in_dir / f"{event_id}.wav", s.listen_seconds
        )
        log.info("capture: %s (%d bytes)", clip_path, clip_path.stat().st_size if clip_path else 0)

        transcript = ""
        classification, reply_key = "unknown_uncooperative", "no_answer"
        if clip_path:
            transcript = await asyncio.to_thread(self._transcriber.transcribe, clip_path)
            log.info("transcript: %r", transcript)
            classification, reply_key = classify_response(transcript)

        # Reply
        response_text = s.replies[reply_key]
        reply_wav, reply_pc = await self._speak(
            reply_key, response_text, s.out_dir / f"{event_id}_reply.wav"
        )
        if reply_pc is not None:
            await self._sleep(wav_duration(reply_wav) + PLAYBACK_TAIL_SECONDS)
            await reply_pc.close()

        result = {
            "ts": self._clock(),
            "event_id": event_id,
            "camera": s.camera_name,
            "classification": classification,
            "transcript": transcript,
            "response": response_text,
        }
        self._event_log.append(result)
        log.info("visit: %s", json.dumps(result, ensure_ascii=False))
