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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gatekeeper import talkback
from gatekeeper.audio import Synthesizer, Transcriber, capture_audio_clip
from gatekeeper.classify import classify_response
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.listen import Word, join_words, strip_greeting
from gatekeeper.logs import event_id_var
from gatekeeper.vad import Endpointer, TurnEnd

log = logging.getLogger(__name__)

# Typical delay from a clip being sent to it being heard at the door (go2rtc
# jitter buffer, RTSP backchannel, camera buffer): ~2.4 s on a Reolink
# doorbell. The prompt's echo ends about this long after its last frame.
TALKBACK_LATENCY_SECONDS = 2.5
# How long to keep the talkback session open after the last clip was sent.
# Generous: the delay varies (3.2 s when a button press's chime plays first),
# closing early cuts the end off, and holding it open costs nothing.
PLAYBACK_HOLD_SECONDS = 5.0
# A clip normally finishes sending after its own duration. On a slow or busy CPU
# it takes longer; wait up to this much longer for its last frame, and warn when
# it's this late.
MAX_PLAYBACK_LAG_SECONDS = 15.0
PLAYBACK_LAG_WARN_SECONDS = 1.0
# The visitor's speech counts from this long before the prompt's echo is due to
# end: the latency estimate is approximate, and Silero rarely mistakes the echo
# (our TTS through the doorbell speaker) for speech anyway.
ECHO_MARGIN_SECONDS = 0.5
TAP_POLL_SECONDS = 0.05

# connect() -> an open talkback session (play(wav) -> talkback.Playback, async
# close()), or None if it can't connect.
ConnectFn = Callable[[], Awaitable[Any]]
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
    endpoint is how the last listening turn ended: "answered" (the visitor
    spoke, then fell silent), "no_input", "max" (talked too long), "tap_lost",
    or "window" (no audio tap: a fixed-length recording).
    reply_latency is the seconds from the end of the visitor's answer to the
    reply starting to play (add the camera's ~2.4 s to hear it).
    """

    event_id: str
    camera: str
    outcome: str = "error"
    classification: str | None = None
    transcript: str = ""
    response: str | None = None
    trigger: str = "person"
    turns: int = 0
    endpoint: str | None = None
    reply_latency: float | None = None
    notified: bool = False
    # When the visitor stopped talking (time.monotonic()), if the tap heard it.
    speech_end: float | None = field(default=None, repr=False)

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
            "endpoint": self.endpoint,
            "reply_latency": self.reply_latency,
        }


async def wait_sent(playback) -> float | None:
    """When the clip's last frame was sent (time.monotonic()), or None if it still
    hasn't been MAX_PLAYBACK_LAG_SECONDS after it should have."""
    try:
        sent = await asyncio.wait_for(
            asyncio.shield(playback.sent), playback.seconds + MAX_PLAYBACK_LAG_SECONDS
        )
    except TimeoutError:
        log.warning(
            "clip still playing %.0f s after it should have ended", MAX_PLAYBACK_LAG_SECONDS
        )
        return None
    if playback.started.done():
        lag = sent - playback.started.result() - playback.seconds
        if lag >= PLAYBACK_LAG_WARN_SECONDS:
            log.warning("clip finished playing %.1f s late: the CPU is too slow or too busy", lag)
    return sent


class _Talk:
    """How a visit speaks: one talkback session for the whole visit, or (per_clip)
    a new session per phrase, closed once the phrase has been heard."""

    def __init__(self, connect: ConnectFn, sleep, per_clip: bool) -> None:
        self._connect = connect
        self._sleep = sleep
        self._per_clip = per_clip
        self._session = None
        self._closing: list[asyncio.Task] = []

    async def say(self, wav: Path):
        """Queue ``wav``; return its Playback, or None if talkback can't connect."""
        session = self._session
        if session is None:
            session = await self._connect()
            if session is None:
                return None
        playback = session.play(wav)
        if self._per_clip:
            self._closing.append(asyncio.ensure_future(self._close_after(session, playback)))
        else:
            self._session = session
        return playback

    async def heard(self, playback) -> None:
        """Wait until ``playback`` has been sent and the camera has played it."""
        await wait_sent(playback)
        await self._sleep(PLAYBACK_HOLD_SECONDS)

    async def _close_after(self, session, playback) -> None:
        try:
            await self.heard(playback)
        finally:
            await session.close()

    async def close(self) -> None:
        """Close now (per-clip sessions still playing are cut short)."""
        for task in self._closing:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._closing, return_exceptions=True)
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()


class Interaction:
    def __init__(
        self,
        settings: Settings,
        *,
        synth: Synthesizer,
        transcriber: Transcriber,
        presynth: dict[str, Path],
        event_log: EventLog,
        connect: ConnectFn | None = None,
        tap=None,
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
        self._connect = connect or self._connect_go2rtc
        # The persistent audio tap (audio_tap.AudioTap), or None: record a
        # fixed window per prompt instead.
        self._tap = tap
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

    async def _connect_go2rtc(self):
        session = talkback.TalkSession(self.settings.go2rtc_api, self.settings.go2rtc_talk_stream)
        return session if await session.open() else None

    def _talk(self) -> _Talk:
        return _Talk(self._connect, self._sleep, self.settings.talkback_session == "clip")

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
                    asyncio.run(self._say_once(visit, "pressed"))
                except Exception:
                    log.exception("could not answer the doorbell press")
                    visit.response = None
                finally:
                    self._talkback_lock.release()
            self._log_visit(visit)
            return visit
        finally:
            event_id_var.reset(token)

    async def _say_once(self, visit: Visit, key: str) -> None:
        """Play one phrase on its own session and keep it open until it's heard."""
        talk = self._talk()
        try:
            playback = await asyncio.wait_for(
                self._say(talk, visit, key, self.settings.replies[key]),
                self.settings.visit_timeout_seconds,
            )
            if playback is None:
                visit.response = None
                return
            await talk.heard(playback)
        finally:
            await talk.close()

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

    async def _say(self, talk: _Talk, visit: Visit, key: str, text: str):
        """Queue a pre-synthesized phrase (or synthesize it now); return its Playback."""
        wav = self._presynth.get(key)
        if wav is None:
            wav = self.settings.out_dir / f"{visit.event_id}_{key}.wav"
            await self._in_thread(self._synth.synthesize, text, wav)
        return await talk.say(wav)

    # -- listening ---------------------------------------------------------------

    async def _ask(self, visit: Visit, prompt: str, playback, clip_name: str) -> str:
        """Listen for the visitor's answer to ``prompt`` (playing as ``playback``)
        and classify it. Returns the reply key.

        The recording includes the prompt's own echo (the doorbell's mic hears
        its speaker); it's cut out after transcription (see listen.strip_greeting).
        """
        visit.turns += 1
        path = self.settings.in_dir / clip_name
        if self._tap is not None and self._tap.healthy():
            words = await self._answer_from_tap(visit, playback, path)
        else:
            if self._tap is not None:
                log.warning("audio tap down; recording a fixed window instead")
            words = await self._answer_from_window(visit, playback, path)

        answer, method = strip_greeting(words, prompt)
        visit.transcript = join_words(answer)
        log.info("heard: %r", join_words(words))
        log.info("transcript (%s): %r", method, visit.transcript)
        if not visit.transcript:
            visit.classification = "no_response"
            return "no_answer"
        visit.classification, reply_key = classify_response(visit.transcript)
        return reply_key

    async def _answer_from_window(self, visit: Visit, playback, path: Path) -> list[Word]:
        """Record a fixed window from the prompt's start: its echo plus
        LISTEN_SECONDS (the Phase 0 way, without the tap)."""
        s = self.settings
        visit.endpoint, visit.speech_end = "window", None
        seconds = math.ceil(playback.seconds + TALKBACK_LATENCY_SECONDS + s.listen_seconds)
        clip = await self._in_thread(self._capture, s.audio_rtsp_url, path, seconds)
        log.info("capture: %s (%d bytes)", clip, clip.stat().st_size if clip else 0)
        if not clip:
            return []
        return await self._in_thread(self._transcriber.transcribe, clip)

    def _endpointer(self, listen_from: float, no_input_by: float) -> Endpointer:
        s = self.settings
        return Endpointer(
            listen_from=listen_from,
            no_input_by=no_input_by,
            threshold=s.vad_threshold,
            end_silence=s.end_silence_ms / 1000,
            max_answer=s.max_answer_seconds,
        )

    async def _answer_from_tap(self, visit: Visit, playback, path: Path) -> list[Word]:
        """Listen on the audio tap until the visitor has finished answering."""
        s = self.settings
        started = await asyncio.shield(playback.started)
        sent = await wait_sent(playback) or started + playback.seconds
        echo_end = sent + TALKBACK_LATENCY_SECONDS
        turn = await self._endpoint(
            self._endpointer(echo_end - ECHO_MARGIN_SECONDS, echo_end + s.listen_seconds), started
        )
        words: list[Word] = []
        resumed = False
        while True:
            clip = self._tap.write_wav(started, turn.at, path)
            log.info(
                "turn ended %s, %.1f s after the prompt's echo%s",
                turn.reason,
                turn.at - echo_end,
                ""
                if turn.speech_end is None
                else f"; answer {turn.speech_end - turn.speech_start:.1f} s",
            )
            if clip is None:
                break
            stt = asyncio.ensure_future(self._in_thread(self._transcriber.transcribe, clip))
            if turn.reason != "answered" or resumed:
                words = await stt
                break
            # A pause can split an answer ("...the package. [pause] It's for Sam."):
            # if they talk again while we transcribe, listen on and transcribe it all.
            more = await self._resumed(turn.at, stt)
            if more is None:
                words = await stt
                break
            log.info("visitor kept talking after a %.1f s pause", s.end_silence_ms / 1000)
            turn, resumed = more, True
        visit.endpoint = turn.reason
        visit.speech_end = turn.speech_end
        return words

    async def _endpoint(self, ep: Endpointer, after: float) -> TurnEnd:
        """Feed the tap's speech probabilities (from ``after``) to ``ep`` until it ends the turn."""
        t = after
        while True:
            for chunk_end, prob in self._tap.probs_after(t):
                t = chunk_end
                end = ep.update(chunk_end, prob)
                if end:
                    return end
            if not self._tap.healthy():
                log.warning("audio tap lost mid-turn")
                return TurnEnd("tap_lost", t)
            await self._sleep(TAP_POLL_SECONDS)

    async def _resumed(self, after: float, stt: asyncio.Future) -> TurnEnd | None:
        """While ``stt`` runs, watch for the visitor talking again after ``after``.
        Returns the new end of their turn, or None if they stayed silent."""
        ep = self._endpointer(after, math.inf)
        t = after
        while not stt.done() or ep.heard_speech:
            for chunk_end, prob in self._tap.probs_after(t):
                t = chunk_end
                end = ep.update(chunk_end, prob)
                if end:
                    return end
            if not self._tap.healthy():
                return TurnEnd("tap_lost", t) if ep.heard_speech else None
            await self._sleep(TAP_POLL_SECONDS)
        return None

    # -- the visit ---------------------------------------------------------------

    async def _run_async(self, visit: Visit) -> None:
        if not self._webrtc_available:
            log.error("aiortc not available")
            visit.outcome = "talkback_unavailable"
            return
        talk = self._talk()
        try:
            await self._converse(visit, talk)
        finally:
            await talk.close()

    async def _converse(self, visit: Visit, talk: _Talk) -> None:
        s = self.settings
        event_id = visit.event_id

        greeting = await self._say(talk, visit, "greeting", s.greeting)
        if greeting is None:
            visit.outcome = "talkback_failed"
            return
        reply_key = await self._ask(visit, s.greeting, greeting, f"{event_id}.wav")

        if reply_key == "no_answer":
            # The no-answer reply asks again ("...please state your purpose"):
            # listen once more after it.
            question = await self._say(talk, visit, "no_answer", s.replies["no_answer"])
            if question is None:
                visit.response = None
                visit.outcome = "reply_failed"
                self._notify(visit)
                return
            reply_key = await self._ask(
                visit, s.replies["no_answer"], question, f"{event_id}-2.wav"
            )
            if reply_key == "no_answer":
                # Still silent: the question was the reply.
                visit.response = s.replies["no_answer"]
                self._notify(visit)
                visit.outcome = "completed"
                return

        visit.response = s.replies[reply_key]
        self._notify(visit)

        reply = await self._say(talk, visit, reply_key, visit.response)
        if reply is None:
            visit.outcome = "reply_failed"
            return
        if visit.speech_end is not None:
            visit.reply_latency = round(await asyncio.shield(reply.started) - visit.speech_end, 2)
            log.info("reply started %.2f s after the visitor stopped talking", visit.reply_latency)
        await talk.heard(reply)
        visit.outcome = "completed"
