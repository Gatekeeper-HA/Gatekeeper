"""One visit: greet, listen, transcribe, classify, reply, log."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gatekeeper import talkback
from gatekeeper.audio import Synthesizer, Transcriber, capture_audio_clip, wav_duration
from gatekeeper.classify import classify_response
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.listen import join_words, strip_greeting
from gatekeeper.logs import event_id_var

log = logging.getLogger(__name__)

# Typical delay from talkback connect to audible speech (go2rtc jitter buffer,
# RTSP backchannel, camera buffer): ~2.4 s on a Reolink doorbell. Used to size
# the recording window.
TALKBACK_LATENCY_SECONDS = 2.5
# How long to keep the talkback session open after the clip ends. Generous:
# the delay varies (3.2 s when a button press's chime plays first), closing
# early cuts the end off, and holding it open costs nothing while recording.
PLAYBACK_HOLD_SECONDS = 5.0
# A clip normally finishes sending after its own duration. On a slow or busy CPU,
# aiortc sends it slower than real time; wait up to this much longer for the last
# frame, and warn when it's this late.
MAX_PLAYBACK_LAG_SECONDS = 15.0
PLAYBACK_LAG_WARN_SECONDS = 1.0
PLAYBACK_POLL_SECONDS = 0.1

# play(wav_path) -> an open connection with an async close(), or None on failure.
PlayFn = Callable[[Path], Awaitable[Any]]
CaptureFn = Callable[[str, Path, int], Path | None]


@dataclass
class Visit:
    """What happened at the door; written to events.jsonl once per visit.

    outcome is one of: completed, reply_failed, talkback_busy,
    talkback_failed, talkback_unavailable, timeout, error, or
    pressed_during_visit (a doorbell press while a visit ran or just ended).
    trigger is "person" (Frigate detection) or "button" (doorbell press).
    turns is how many times Gatekeeper listened: 2 when the first answer was
    silence and the no-answer reply asked again.
    """

    event_id: str
    camera: str
    outcome: str = "error"
    classification: str | None = None
    transcript: str = ""
    response: str | None = None
    trigger: str = "person"
    turns: int = 0
    notified: bool = False

    def record(self, ts: float) -> dict:
        return {
            "ts": ts,
            "event_id": self.event_id,
            "camera": self.camera,
            "trigger": self.trigger,
            "outcome": self.outcome,
            "classification": self.classification,
            "transcript": self.transcript,
            "response": self.response,
            "turns": self.turns,
        }


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
        on_notify: Sequence[Callable[[Visit], None]] = (),
        on_logged: Sequence[Callable[[dict], None]] = (),
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
        # on_notify: once per visit, as soon as there's something to tell the
        # resident (right after classification, or when the visit fails).
        # on_logged: with the final record. Callbacks must not block.
        self._on_notify = list(on_notify)
        self._on_logged = list(on_logged)
        # go2rtc supports one talkback WebRTC session at a time.
        self._talkback_lock = threading.Lock()
        # Blocking work (ffmpeg, Whisper, Kokoro) runs here rather than in
        # asyncio's default executor, which asyncio.run waits for on exit: a
        # stuck thread must not stop a timed-out visit from returning.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="visit-io")

    async def _play_via_go2rtc(self, wav_path: Path):
        return await talkback.play_wav(
            self.settings.go2rtc_api, self.settings.go2rtc_talk_stream, wav_path
        )

    def run(self, event_id: str, trigger: str = "person") -> Visit:
        """Handle the visit for Frigate event ``event_id`` (blocking) and log it."""
        token = event_id_var.set(event_id)
        visit = Visit(event_id, self.settings.camera_name, trigger=trigger)
        try:
            log.info("interaction started")
            if not self._talkback_lock.acquire(blocking=False):
                log.warning("talkback busy; logging the visit without a greeting")
                visit.outcome = "talkback_busy"
            else:
                try:
                    asyncio.run(self._run_with_timeout(visit))
                except TimeoutError:
                    log.error("visit timed out after %.0f s", self.settings.visit_timeout_seconds)
                    visit.outcome = "timeout"
                except Exception:
                    log.exception("interaction error")
                    visit.outcome = "error"
                finally:
                    self._talkback_lock.release()
            self._notify(visit)
            self._log_visit(visit)
            return visit
        finally:
            event_id_var.reset(token)

    def acknowledge_press(self, visit: Visit) -> Visit:
        """A doorbell press during a visit's cooldown: notify at once, then say
        REPLY_PRESSED if the speaker is free (not mid-visit), and log it."""
        token = event_id_var.set(visit.event_id)
        try:
            self._notify(visit)
            if self._webrtc_available and self._talkback_lock.acquire(blocking=False):
                try:
                    visit.response = self.settings.replies["pressed"]
                    asyncio.run(self._say(visit, "pressed"))
                except Exception:
                    log.exception("could not answer the doorbell press")
                    visit.response = None
                finally:
                    self._talkback_lock.release()
            self._log_visit(visit)
            return visit
        finally:
            event_id_var.reset(token)

    async def _say(self, visit: Visit, key: str) -> None:
        """Play one phrase and keep the talkback open until it has been heard."""
        text = self.settings.replies[key]
        wav, pc = await asyncio.wait_for(
            self._speak(key, text, self.settings.out_dir / f"{visit.event_id}_{key}.wav"),
            self.settings.visit_timeout_seconds,
        )
        if pc is None:
            visit.response = None
            return
        try:
            await self._hold(pc, wav_duration(wav))
        finally:
            await pc.close()

    async def _hold(self, pc, seconds: float) -> None:
        """Keep talkback ``pc`` open while its clip of ``seconds`` plays, then
        PLAYBACK_HOLD_SECONDS more for the camera to play out what it buffered.

        Closing on the clock alone cut greetings off on a slow host (an HA OS VM),
        where the clip took longer than its duration to send: wait for its last
        frame too.
        """
        await self._sleep(seconds)
        lag = await _wait_until_sent(pc)
        if lag >= PLAYBACK_LAG_WARN_SECONDS:
            log.warning("clip finished playing %.1f s late: the CPU is too slow or too busy", lag)
        await self._sleep(PLAYBACK_HOLD_SECONDS)

    def report(self, visit: Visit) -> None:
        """Notify and log something that isn't a visit run here (e.g. a press
        during a visit)."""
        self._notify(visit)
        self._log_visit(visit)

    def _notify(self, visit: Visit) -> None:
        if visit.notified:
            return
        visit.notified = True
        for callback in self._on_notify:
            try:
                callback(visit)
            except Exception:
                log.exception("notify callback failed")

    def _log_visit(self, visit: Visit) -> None:
        record = visit.record(self._clock())
        self._event_log.append(record)
        log.info("visit: %s", json.dumps(record, ensure_ascii=False))
        for callback in self._on_logged:
            try:
                callback(record)
            except Exception:
                log.exception("visit-logged callback failed")

    async def _run_with_timeout(self, visit: Visit) -> None:
        await asyncio.wait_for(self._run_async(visit), self.settings.visit_timeout_seconds)

    async def _in_thread(self, fn: Callable, *args):
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(self._executor, functools.partial(ctx.run, fn, *args))

    async def _speak(self, key: str, text: str, fallback_path: Path):
        """Play a pre-synthesized phrase (or synthesize it now); return the open connection."""
        wav = self._presynth.get(key)
        if wav is None:
            wav = fallback_path
            await self._in_thread(self._synth.synthesize, text, wav)
        return wav, await self._play(wav)

    async def _ask(self, visit: Visit, prompt: str, wav: Path, pc, clip_name: str) -> str:
        """Listen while ``prompt`` plays on the open talkback ``pc`` and for
        LISTEN_SECONDS after it; classify the answer. Returns the reply key.

        Recording starts with the prompt: visitors answer as soon as it ends,
        and opening the RTSP stream takes about as long as the talkback
        latency. The prompt's own echo is cut out after transcription (see
        listen.strip_greeting).
        """
        s = self.settings
        visit.turns += 1
        try:
            seconds = wav_duration(wav)
            capture_seconds = math.ceil(seconds + TALKBACK_LATENCY_SECONDS + s.listen_seconds)
            clip = s.in_dir / clip_name
            capture = asyncio.ensure_future(
                self._in_thread(self._capture, s.audio_rtsp_url, clip, capture_seconds)
            )
            await self._hold(pc, seconds)
        finally:
            await pc.close()

        clip_path = await capture
        log.info("capture: %s (%d bytes)", clip_path, clip_path.stat().st_size if clip_path else 0)

        visit.transcript = ""
        visit.classification, reply_key = "no_response", "no_answer"
        if clip_path:
            words = await self._in_thread(self._transcriber.transcribe, clip_path)
            answer, method = strip_greeting(words, prompt)
            visit.transcript = join_words(answer)
            log.info("heard: %r", join_words(words))
            log.info("transcript (%s): %r", method, visit.transcript)
            visit.classification, reply_key = classify_response(visit.transcript)
        return reply_key

    async def _run_async(self, visit: Visit) -> None:
        s = self.settings
        event_id = visit.event_id
        if not self._webrtc_available:
            log.error("aiortc not available")
            visit.outcome = "talkback_unavailable"
            return

        greet_wav, greet_pc = await self._speak(
            "greeting", s.greeting, s.out_dir / f"{event_id}_greeting.wav"
        )
        if greet_pc is None:
            visit.outcome = "talkback_failed"
            return
        reply_key = await self._ask(visit, s.greeting, greet_wav, greet_pc, f"{event_id}.wav")

        if reply_key == "no_answer":
            # The no-answer reply asks again ("...please state your purpose"):
            # listen once more while and after it plays.
            ask_wav, ask_pc = await self._speak(
                "no_answer", s.replies["no_answer"], s.out_dir / f"{event_id}_reply.wav"
            )
            if ask_pc is None:
                visit.response = None
                visit.outcome = "reply_failed"
                self._notify(visit)
                return
            reply_key = await self._ask(
                visit, s.replies["no_answer"], ask_wav, ask_pc, f"{event_id}-2.wav"
            )
            if reply_key == "no_answer":
                # Still silent: the question was the reply.
                visit.response = s.replies["no_answer"]
                self._notify(visit)
                visit.outcome = "completed"
                return

        visit.response = s.replies[reply_key]
        self._notify(visit)

        # Reply
        reply_wav, reply_pc = await self._speak(
            reply_key, visit.response, s.out_dir / f"{event_id}_reply.wav"
        )
        if reply_pc is None:
            visit.outcome = "reply_failed"
            return
        try:
            await self._hold(reply_pc, wav_duration(reply_wav))
        finally:
            await reply_pc.close()
        visit.outcome = "completed"


async def _wait_until_sent(pc) -> float:
    """Wait (up to MAX_PLAYBACK_LAG_SECONDS) until every audio track on talkback
    ``pc`` has ended, i.e. its last frame has been sent. Returns the seconds waited."""
    tracks = [s.track for s in pc.getSenders() if s.track] if hasattr(pc, "getSenders") else []
    start = time.monotonic()
    while any(t.readyState == "live" for t in tracks):
        if time.monotonic() - start >= MAX_PLAYBACK_LAG_SECONDS:
            break
        await asyncio.sleep(PLAYBACK_POLL_SECONDS)
    return time.monotonic() - start
