"""One visit: greet, listen, transcribe, classify, reply, log."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
import logging
import math
import random
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from gatekeeper import dialogue, talkback
from gatekeeper.audio import Synthesizer, Transcriber, capture_audio_clip
from gatekeeper.camera import go2rtc_frame, photo_sharpness
from gatekeeper.classify import CONVERSATIONS, classify_response
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.listen import Word, join_words, strip_greeting
from gatekeeper.logs import event_id_var
from gatekeeper.vad import CHUNK_SECONDS, Endpointer, TurnEnd, speech_seconds

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
# A queued phrase that hasn't started playing by then (after any phrase ahead of it): the
# speaker's connection stopped taking audio. The visit ends (reply_failed, or
# talkback_failed if nothing played) rather than waiting for the visit's timeout (it once
# waited 3 minutes after a reply).
TALKBACK_START_SECONDS = 20.0
PLAYBACK_LAG_WARN_SECONDS = 1.0
# The visitor's speech counts from this long before the prompt's echo is due to
# end: the latency estimate is approximate, and Silero rarely mistakes the echo
# (our TTS through the doorbell speaker) for speech anyway.
ECHO_MARGIN_SECONDS = 0.5
# Words Whisper places this long after the prompt's estimated echo end are the
# visitor's, even if they repeat the prompt (its timestamps are approximate).
ECHO_UNTIL_SLACK_SECONDS = 0.5
TAP_POLL_SECONDS = 0.05
# An answer needs at least this much speech (by Silero) where Whisper heard it;
# less, and Whisper made it up from noise ("Thanks for watching!").
MIN_ANSWER_SPEECH_SECONDS = 0.25
# A turn with less than this of even weak speech isn't transcribed (Interaction._any_speech).
ANY_SPEECH_SECONDS = 0.1
# Photos of a document held up to the camera (a warrant): how many, how far apart.
# Several, close together, so one catches the page held still (Gatekeeper#24).
FRAME_COUNT = 6
FRAME_INTERVAL_SECONDS = 0.5

# connect() -> an open talkback session (play(wav) -> talkback.Playback, async
# close()), or None if it can't connect.
ConnectFn = Callable[[], Awaitable[Any]]
CaptureFn = Callable[[str, Path, int], Path | None]


@dataclass
class Visit:
    """What happened at the door; written to events.jsonl once per visit.

    outcome is one of: completed, reply_failed, talkback_busy,
    talkback_failed, talkback_unavailable, timeout, error,
    pressed_during_visit (a doorbell press while a visit ran or just ended),
    left (someone came out of the front door and walked away), stepped_out (came
    out and went back in), or returned (someone walked up soon after leaving: most
    likely them coming back in). None of these is greeted or notified. entered:
    greeted, but they went in by the front door without saying anything (the
    household coming home), so the visit stopped there
    and the notification is a quiet one. household: someone the owner enrolled,
    recognized by face (details: name, greeted), either before the greeting (not
    greeted) or during the visit before they said anything (stopped); also a quiet
    notification. quick_reply records the resident's quick reply to a visit (same
    event id; details: reply, result, after_seconds, present).
    package_delivered and package_picked_up record a package left at the door or
    taken (packages.py; event id package-<when the visit that left it started>;
    trigger "package"; details: box, score, since).
    trigger is "person" (Frigate detection), "button" (doorbell press) or
    "quick_reply".
    turns is how many times Gatekeeper listened: 2 when the first answer was
    silence and the no-answer reply asked again.
    endpoint is how the last listening turn ended: "answered" (the visitor
    spoke, then fell silent), "no_input", "max" (talked too long), "tap_lost",
    or "window" (no audio tap: a fixed-length recording).
    reply_latency is the seconds from the end of the visitor's answer to the
    reply (or a conversation's first phrase) starting to play (add the camera's
    ~2.4 s to hear it).
    flow is the conversation that took over (dialogue.py): "emergency",
    "law_enforcement" or "civic"; details is what it found out (identity,
    reason, warrant, message, decision, ...); dialogue lists every question
    asked and what was heard.
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
    flow: str | None = None
    details: dict = field(default_factory=dict)
    dialogue: list = field(default_factory=list)
    notified: bool = False
    # When the visitor stopped talking (time.monotonic()), if the tap heard it.
    speech_end: float | None = field(default=None, repr=False)
    # A photo to send instead of Frigate's snapshot (a warrant held up to the camera).
    image: Path | None = field(default=None, repr=False)
    # "alert": an interim notification during a conversation; "final" otherwise.
    stage: str = field(default="final", repr=False)

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
            "flow": self.flow,
            "details": self.details,
            "dialogue": self.dialogue,
        }


class Stopped(Exception):
    """The visit is for the household (they went in by the door, or were recognized)
    and they haven't said anything: it ends here, with ``outcome`` and ``details``."""

    def __init__(self, outcome: str, details: dict) -> None:
        super().__init__(outcome)
        self.outcome = outcome
        self.details = details


# Outcomes of visits that turned out to be the household (Stopped, log_household).
HOUSEHOLD_OUTCOMES = ("entered", "household")


class TalkbackStalled(Exception):
    """A phrase never started playing: the doorbell's speaker connection stopped."""


async def wait_started(playback) -> float:
    """When the clip's first frame went out (time.monotonic()); ``TalkbackStalled`` if it
    hasn't within TALKBACK_START_SECONDS."""
    try:
        return await asyncio.wait_for(asyncio.shield(playback.started), TALKBACK_START_SECONDS)
    except TimeoutError:
        raise TalkbackStalled(
            f"a phrase hasn't started playing {TALKBACK_START_SECONDS:.0f} s after it was queued"
        ) from None


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
        self.per_clip = per_clip
        self._session = None
        self._closing: list[asyncio.Task] = []
        self._playbacks: list = []

    async def say(self, wav: Path):
        """Queue ``wav``; return its Playback, or None if talkback can't connect."""
        session = self._session
        if session is None:
            session = await self._connect()
            if session is None:
                return None
        playback = session.play(wav)
        self._playbacks.append(playback)
        if self.per_clip:
            self._closing.append(asyncio.ensure_future(self._close_after(session, playback)))
        else:
            self._session = session
        return playback

    def played(self) -> bool:
        """Whether any phrase started playing (someone at the door heard something)."""
        return any(p.started.done() for p in self._playbacks)

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
        present: Callable[[], bool] | None = None,
        leaving: Callable[[], bool] | None = None,
        grab_frame: Callable[[Path], Path | None] | None = None,
        speech_check: Callable[[Path, float, float], float | None] | None = None,
        photo_score: Callable[[Path], float | None] = photo_sharpness,
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
        self._random = random.Random()
        self._last: dict[str, str] = {}  # the line said last, by kind: not twice in a row
        self._event_log = event_log
        self._connect = connect or self._connect_go2rtc
        # The persistent audio tap (audio_tap.AudioTap), or None: record a
        # fixed window per prompt instead.
        self._tap = tap
        # Is someone still on the porch (SessionTracker.person_present)?
        self._present = present
        # Is everyone on camera walking away (SessionTracker.walking_away)?
        self._leaving = leaving
        self._grab_frame = grab_frame or self._grab_go2rtc_frame
        # How sharp a photo is (camera.photo_sharpness): the sharpest is sent.
        self._photo_score = photo_score
        # Seconds of speech in a clip between two times (None: can't tell).
        self._speech_check = speech_check or self._silero_speech
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
        # Visits to stop, and why (went_inside, recognized; from SessionTracker).
        self._stops: dict[str, tuple[str, dict]] = {}
        self._stops_lock = threading.Lock()
        # Blocking work (ffmpeg, Whisper, Kokoro) runs here rather than in
        # asyncio's default executor, which asyncio.run waits for on exit: a
        # stuck thread must not stop a timed-out visit from returning.
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="visit-io")

    async def _connect_go2rtc(self):
        session = talkback.TalkSession(self.settings.go2rtc_api, self.settings.go2rtc_talk_stream)
        return session if await session.open() else None

    def _silero_speech(self, path: Path, start: float, end: float) -> float | None:
        try:
            return speech_seconds(path, start, end, self.settings.vad_threshold)
        except Exception:
            log.exception("couldn't check the answer for speech")
            return None

    def _grab_go2rtc_frame(self, path: Path) -> Path | None:
        return go2rtc_frame(self.settings.go2rtc_api, self.settings.camera_name, path)

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
                except Stopped as stop:
                    log.info("stopped: %s", stop.outcome)
                    visit.outcome = stop.outcome
                    visit.details.update(stop.details)
                except TimeoutError:
                    log.error("visit timed out after %.0f s", self.settings.visit_timeout_seconds)
                    visit.outcome = "timeout"
                except Exception:
                    log.exception("interaction error")
                    visit.outcome = "error"
                finally:
                    self._talkback_lock.release()
            self._notify(visit)
            # The household isn't Home Assistant's last visitor.
            self._log_visit(visit, publish=visit.outcome not in HOUSEHOLD_OUTCOMES)
            return visit
        finally:
            with self._stops_lock:
                self._stops.pop(event_id, None)
            event_id_var.reset(token)

    def went_inside(self, event_id: str) -> None:
        """``SessionTracker``: the person visit ``event_id`` is for went in by the front
        door. The visit stops at its next chance, unless they've said something."""
        self._stop(event_id, "entered", {})

    def recognized(self, event_id: str, name: str) -> None:
        """``SessionTracker``: the person visit ``event_id`` is for is ``name``, by their
        face. The visit stops at its next chance, unless they've said something."""
        self._stop(event_id, "household", {"name": name, "greeted": True})

    def _stop(self, event_id: str, outcome: str, details: dict) -> None:
        with self._stops_lock:
            self._stops.setdefault(event_id, (outcome, details))

    def _check_stop(self, visit: Visit) -> None:
        """Stop the visit (``Stopped``) if it's for the household and they haven't
        answered."""
        with self._stops_lock:
            stop = self._stops.get(visit.event_id)
        if stop and not visit.transcript and visit.flow is None:
            raise Stopped(*stop)

    def log_household(self, event_id: str, name: str) -> None:
        """``SessionTracker``: someone recognized by face before their greeting. Not
        greeted; a quiet notification, and not Home Assistant's last visitor."""
        token = event_id_var.set(event_id)
        try:
            visit = Visit(event_id, self.settings.camera_name, outcome="household")
            visit.details = {"name": name, "greeted": False}
            self._notify(visit)
            self._log_visit(visit, publish=False)
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
                    asyncio.run(self._say_once(visit, "pressed", visit.response))
                except Exception:
                    log.exception("could not answer the doorbell press")
                    visit.response = None
                finally:
                    self._talkback_lock.release()
            self._log_visit(visit)
            return visit
        finally:
            event_id_var.reset(token)

    def quick_reply(self, event_id: str, reply: str, after: float | None, left: float) -> Visit:
        """The resident's quick reply to visit ``event_id`` (quick_reply.py): say it
        at the door, once the speaker is free, and log it. ``after``: seconds since
        the visit was notified (None: not known); ``left``: how long it may wait to
        be said (<= 0: too late)."""
        token = event_id_var.set(event_id)
        try:
            visit = Visit(
                event_id, self.settings.camera_name, outcome="quick_reply", trigger="quick_reply"
            )
            visit.details = {
                "reply": reply,
                "after_seconds": None if after is None else round(after, 1),
            }
            if left <= 0:
                result = "too_late"
            elif not self._webrtc_available:
                result = "failed"
            elif not self._talkback_lock.acquire(timeout=left):
                result = "busy"  # a visit held the speaker until the window ended
            else:
                try:
                    if self._present is not None:
                        visit.details["present"] = self._present()
                    visit.response = self.settings.quick_reply_phrases[reply]
                    asyncio.run(self._say_once(visit, f"quick_{reply}", visit.response))
                    result = "said" if visit.response else "failed"
                except Exception:
                    log.exception("could not say the quick reply")
                    visit.response, result = None, "failed"
                finally:
                    self._talkback_lock.release()
            visit.details["result"] = result
            log.info("quick reply %r: %s", reply, result)
            self._log_visit(visit, publish=False)
            return visit
        finally:
            event_id_var.reset(token)

    async def _say_once(self, visit: Visit, key: str, text: str) -> None:
        """Play one phrase on its own session and keep it open until it's heard."""
        talk = self._talk()
        try:
            playback = await asyncio.wait_for(
                self._say(talk, visit, key, text),
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

    def _alert(self, visit: Visit) -> None:
        """An interim notification during a conversation (e.g. "law enforcement at
        the door" before it's over); the final one still follows."""
        alert = replace(visit, stage="alert", details=dict(visit.details))
        for callback in self._on_notify:
            try:
                callback(alert)
            except Exception:
                log.exception("notify callback failed")

    def log_not_greeted(self, event_id: str, outcome: str) -> None:
        """The household coming out of the front door ("left") or back in soon after
        ("returned"), from ``SessionTracker``'s door zone. Only logged: no greeting,
        no notification, and Home Assistant's last visitor stays the last real one."""
        token = event_id_var.set(event_id)
        try:
            visit = Visit(event_id, self.settings.camera_name, outcome=outcome)
            self._log_visit(visit, publish=False)
        finally:
            event_id_var.reset(token)

    def log_only(self, visit: Visit) -> None:
        """Log a record that isn't a visit run here (e.g. a package left at the door):
        not notified, and Home Assistant's last visitor stays the last real one."""
        token = event_id_var.set(visit.event_id)
        try:
            self._log_visit(visit, publish=False)
        finally:
            event_id_var.reset(token)

    def _log_visit(self, visit: Visit, publish: bool = True) -> None:
        record = visit.record(self._clock())
        self._event_log.append(record)
        log.info("visit: %s", json.dumps(record, ensure_ascii=False))
        for callback in self._on_logged if publish else ():
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

    async def _hear(
        self, visit: Visit, key: str, prompt: str, playback, max_answer: float | None = None
    ) -> str:
        """Listen for the visitor's answer to ``prompt`` (phrase ``key``, playing
        as ``playback``) and return it.

        The recording includes the prompt's own echo (the doorbell's mic hears
        its speaker); it's cut out after transcription (see listen.strip_greeting).
        """
        visit.turns += 1
        suffix = "" if visit.turns == 1 else f"-{visit.turns}"
        path = self.settings.in_dir / f"{visit.event_id}{suffix}.wav"
        if self._tap is not None and self._tap.healthy():
            words, echo_until = await self._answer_from_tap(visit, playback, path, max_answer)
        else:
            if self._tap is not None:
                log.warning("audio tap down; recording a fixed window instead")
            words, echo_until = await self._answer_from_window(visit, playback, path, max_answer)

        answer, method = strip_greeting(words, prompt, echo_until)
        if answer:
            spoken = await self._in_thread(
                self._speech_check, path, answer[0].start - 0.3, answer[-1].end + 0.3
            )
            if spoken is not None and spoken < MIN_ANSWER_SPEECH_SECONDS:
                log.info(
                    "ignoring %r: no speech where Whisper heard it (made up from noise)",
                    join_words(answer),
                )
                answer, method = [], "no speech"
        heard = join_words(answer)
        log.info("heard: %r", join_words(words))
        log.info("transcript (%s): %r", method, heard)
        visit.dialogue.append({"asked": key, "heard": heard})
        return heard

    def _line(self, kind: str, visit: Visit) -> tuple[str, str]:
        """What to say for ``kind`` ("greeting" or "no_answer"): (phrase key, text). One of
        Gatekeeper's lines at random, not the one said last time; those that mention the
        bell only after a doorbell press; only lines synthesized already (the rest come in
        the background after startup)."""
        texts = getattr(self.settings, f"{kind}_texts")
        lines = texts(pressed=visit.trigger == "button")
        lines = {k: t for k, t in lines.items() if k in self._presynth} or lines
        keys = [k for k in lines if k != self._last.get(kind)] or list(lines)
        key = self._random.choice(keys)
        self._last[kind] = key
        return key, lines[key]

    async def _ask(self, visit: Visit, key: str, prompt: str, playback) -> str:
        """Hear the answer to ``prompt`` and classify it. Returns the reply key."""
        visit.transcript = await self._hear(visit, key, prompt, playback)
        if not visit.transcript:
            visit.classification = "no_response"
            return "no_answer"
        visit.classification, reply_key = classify_response(visit.transcript)
        return reply_key

    async def _answer_from_window(
        self, visit: Visit, playback, path: Path, max_answer: float | None = None
    ) -> tuple[list[Word], float | None]:
        """Record a fixed window from the prompt's start: its echo plus
        LISTEN_SECONDS, or more for a long answer (the Phase 0 way, without the tap).
        The recording's start relative to the prompt isn't known, so neither is
        when the echo ends in it."""
        s = self.settings
        visit.endpoint, visit.speech_end = "window", None
        listen = s.listen_seconds if max_answer is None else max(s.listen_seconds, max_answer / 2)
        seconds = math.ceil(playback.seconds + TALKBACK_LATENCY_SECONDS + listen)
        clip = await self._in_thread(self._capture, s.audio_rtsp_url, path, seconds)
        log.info("capture: %s (%d bytes)", clip, clip.stat().st_size if clip else 0)
        if not clip:
            return [], None
        return await self._in_thread(self._transcriber.transcribe, clip), None

    def _endpointer(
        self, listen_from: float, no_input_by: float, max_answer: float | None = None
    ) -> Endpointer:
        s = self.settings
        return Endpointer(
            listen_from=listen_from,
            no_input_by=no_input_by,
            threshold=s.vad_threshold,
            end_silence=s.end_silence_ms / 1000,
            max_answer=s.max_answer_seconds if max_answer is None else max_answer,
        )

    async def _answer_from_tap(
        self, visit: Visit, playback, path: Path, max_answer: float | None = None
    ) -> tuple[list[Word], float | None]:
        """Listen on the audio tap until the visitor has finished answering. Returns
        the words and when the prompt's echo ended in the clip."""
        s = self.settings
        started = await wait_started(playback)
        sent = await wait_sent(playback) or started + playback.seconds
        echo_end = sent + TALKBACK_LATENCY_SECONDS
        listen_from = echo_end - ECHO_MARGIN_SECONDS
        turn = await self._endpoint(
            self._endpointer(listen_from, echo_end + s.listen_seconds, max_answer),
            started,
            visit,
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
            if turn.reason == "no_input" and not self._any_speech(started, turn.at):
                # Not even a hint of speech in the whole clip: anything Whisper made of
                # it would fail the speech check (MIN_ANSWER_SPEECH_SECONDS) anyway, and
                # skipping it asks again ~1.5 s sooner. Speech too weak to start an
                # answer (a voice over a lawnmower) or said over the prompt is still
                # transcribed.
                log.info("no speech: not transcribed")
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
        # The clip starts when the prompt started playing.
        return words, echo_end - started + ECHO_UNTIL_SLACK_SECONDS

    def _any_speech(self, t0: float, t1: float) -> bool:
        """Did the tap hear any hint of speech between ``t0`` and ``t1``: 0.1 s above
        the level where speech ends (``VAD_THRESHOLD`` - 0.15)? Noise alone stays well
        below it: a lawnmower next door scored up to 0.14, the officer talking over it
        0.40-0.61. The prompt's own echo rarely counts."""
        level = max(self.settings.vad_threshold - 0.15, 0.01)
        speech = sum(CHUNK_SECONDS for t, p in self._tap.probs_after(t0) if t <= t1 and p >= level)
        return speech >= ANY_SPEECH_SECONDS

    async def _endpoint(self, ep: Endpointer, after: float, visit: Visit) -> TurnEnd:
        """Feed the tap's speech probabilities (from ``after``) to ``ep`` until it ends the
        turn. Stops the visit if its person goes in by the door before saying anything."""
        t = after
        while True:
            if not ep.heard_speech:
                self._check_stop(visit)
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
        except TalkbackStalled as e:
            log.error("the doorbell's speaker stopped: %s; the visit ends", e)
            visit.outcome = "reply_failed" if talk.played() else "talkback_failed"
        finally:
            await talk.close()

    async def _converse(self, visit: Visit, talk: _Talk) -> None:
        s = self.settings
        greeting_key, greeting_text = self._line("greeting", visit)
        greeting = await self._say(talk, visit, greeting_key, greeting_text)
        if greeting is None:
            visit.outcome = "talkback_failed"
            return
        reply_key = await self._ask(visit, greeting_key, greeting_text, greeting)

        if reply_key == "no_answer":
            self._check_stop(visit)  # no need to ask an empty porch again
            if self._leaving is not None and self._leaving():
                # They're already walking away (a courier who dropped something off,
                # mostly): a friendly send-off instead of asking again.
                key, line = self._line("goodbye", visit)
                said = await self._say(talk, visit, key, line)
                visit.response = line if said is not None else None
                visit.details["walked_away"] = True
                visit.outcome = "completed" if said is not None else "reply_failed"
                self._notify(visit)
                return
            # The no-answer reply asks again, in Gatekeeper's voice (one of
            # NO_ANSWER_LINES): listen once more after it.
            key, line = self._line("no_answer", visit)
            question = await self._say(talk, visit, key, line)
            if question is None:
                visit.response = None
                visit.outcome = "reply_failed"
                self._notify(visit)
                return
            reply_key = await self._ask(visit, key, line, question)
            if reply_key == "no_answer":
                self._check_stop(visit)
                # Still silent: the question was the reply.
                visit.response = line
                self._notify(visit)
                visit.outcome = "completed"
                return

        if visit.classification in CONVERSATIONS:
            # Officers, emergencies, canvassers: a conversation of their own.
            await dialogue.run(visit.classification, visit, _Turns(self, talk, visit), s)
            return

        visit.response = s.replies[reply_key]
        self._notify(visit)

        reply = await self._say(talk, visit, reply_key, visit.response)
        if reply is None:
            visit.outcome = "reply_failed"
            return
        await self._time_reply(visit, reply)
        await talk.heard(reply)
        visit.outcome = "completed"

    async def _time_reply(self, visit: Visit, playback) -> None:
        """Set ``reply_latency`` from the first reply played after the visitor's answer:
        the one-line reply, or a conversation's first phrase (Gatekeeper#25)."""
        if visit.reply_latency is not None or visit.speech_end is None:
            return
        visit.reply_latency = round(await wait_started(playback) - visit.speech_end, 2)
        log.info("reply started %.2f s after the visitor stopped talking", visit.reply_latency)


class _Turns:
    """What a conversation (dialogue.py) can do during one visit."""

    def __init__(self, interaction: Interaction, talk: _Talk, visit: Visit) -> None:
        self._i = interaction
        self._talk = talk
        self._visit = visit

    async def ask(
        self,
        key: str,
        max_answer: float | None = None,
        photos: bool = False,
        preface: str | None = None,
        hold: tuple[str, str] | None = None,
    ):
        if preface is not None:
            first = await self._i._say(
                self._talk, self._visit, preface, self._i.settings.phrases[preface]
            )
            if first is None:
                return None
            await self._i._time_reply(self._visit, first)
            if self._talk.per_clip:
                await self._talk.heard(first)  # one session per phrase: not two at once
        text = self._i.settings.phrases[key]
        playback = await self._i._say(self._talk, self._visit, key, text)
        if playback is None:
            return None
        await self._i._time_reply(self._visit, playback)
        # Photos while they answer, e.g. while holding a warrant up to the camera.
        taking = asyncio.ensure_future(self._photos(playback)) if photos else None
        try:
            answer = await self._i._hear(self._visit, key, text, playback, max_answer)
            if hold is not None and taking is not None and not taking.done():
                # They answered before the photos were all taken: don't leave them
                # wondering whether to keep holding it up.
                keep_holding, finished = hold
                if await self.say(keep_holding):
                    await taking
                    await self.say(finished)
            return answer
        finally:
            if taking is not None:
                await taking

    async def say(self, key: str) -> bool:
        playback = await self._i._say(self._talk, self._visit, key, self._i.settings.phrases[key])
        if playback is None:
            return False
        await self._i._time_reply(self._visit, playback)
        await self._talk.heard(playback)
        return True

    def alert(self) -> None:
        self._i._alert(self._visit)

    def notify(self) -> None:
        self._i._notify(self._visit)

    async def wait(self, seconds: float) -> None:
        await self._i._sleep(seconds)

    def present(self) -> bool:
        if self._i._present is None:
            return False  # can't tell: assume they've gone rather than escalate
        return self._i._present()

    async def _photos(self, playback) -> None:
        """FRAME_COUNT full-resolution photos, starting once the prompt has been
        heard at the door. The sharpest goes with the notification (the middle one
        if they can't be scored); all are kept."""
        visit, i = self._visit, self._i
        await wait_sent(playback)
        await i._sleep(TALKBACK_LATENCY_SECONDS)
        i.settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
        taken = []
        for n in range(1, FRAME_COUNT + 1):
            path = i.settings.snapshot_dir / f"{visit.event_id}-{n}.jpg"
            try:
                got = await i._in_thread(i._grab_frame, path)
            except Exception:
                log.exception("couldn't take a photo")
                got = None
            if got:
                taken.append(got)
            if n < FRAME_COUNT:
                await i._sleep(FRAME_INTERVAL_SECONDS)
        log.info("took %d photo(s) of the visitor", len(taken))
        if taken:
            scores = [await i._in_thread(i._photo_score, p) for p in taken]
            scored = [(s, p) for s, p in zip(scores, taken, strict=True) if s is not None]
            visit.image = max(scored)[1] if scored else taken[len(taken) // 2]
            visit.details["photos"] = [p.name for p in taken]
            visit.details["photo_sent"] = visit.image.name
            visit.details["photo_sharpness"] = [None if s is None else round(s, 1) for s in scores]
