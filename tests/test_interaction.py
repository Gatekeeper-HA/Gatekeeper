import asyncio
import json
import math
import threading
import time

import pytest

from conftest import write_wav
from gatekeeper.audio import wav_duration
from gatekeeper.eventlog import EventLog
from gatekeeper.interaction import (
    PLAYBACK_HOLD_SECONDS,
    TALKBACK_LATENCY_SECONDS,
    Interaction,
)
from gatekeeper.listen import Word
from gatekeeper.talkback import Playback
from gatekeeper.vad import CHUNK_SECONDS

EID = "1727712000.123456-abc123"
ALL_REPLY_KEYS = ("no_answer", "delivery", "sales", "maintenance", "generic")
WAV_SECONDS = 2.0
T0 = 1000.0  # time.monotonic() when the visit starts, on the rig's fake clock
# With the greeting sent at T0, its echo ends at:
ECHO_END = T0 + WAV_SECONDS + TALKBACK_LATENCY_SECONDS


class FakeSession:
    """A talkback session: plays clips instantly on the rig's fake clock (or with
    the last frame ``sent_delay`` real seconds late)."""

    def __init__(self, rig, n):
        self.rig, self.n = rig, n

    def play(self, wav):
        rig = self.rig
        rig.calls.append(("play", wav.name))
        loop = asyncio.get_running_loop()
        seconds = wav_duration(wav)
        started, sent = loop.create_future(), loop.create_future()
        started.set_result(rig.now)
        done_at = rig.now + seconds + rig.lag
        if rig.sent_delay is None:
            sent.set_result(done_at)
        elif rig.sent_delay != math.inf:

            def finish():
                rig.calls.append(("sent", wav.name))
                sent.set_result(done_at)

            loop.call_later(rig.sent_delay, finish)
        return Playback(seconds, started, sent)

    async def close(self):
        self.rig.calls.append(("close", self.n))


class FakeTap:
    """The camera's audio, as speech probabilities: the visitor talks during
    ``speech`` [(start, end), ...]; audio exists up to the rig's clock."""

    def __init__(self, rig, speech=(), up_until=math.inf):
        self.rig, self.speech, self.up_until = rig, speech, up_until

    def healthy(self):
        return self.rig.now < self.up_until

    def probs_after(self, t):
        out = []
        k = math.floor(t / CHUNK_SECONDS + 1e-9) + 1
        while k * CHUNK_SECONDS <= min(self.rig.now, self.up_until):
            ct = k * CHUNK_SECONDS
            out.append((ct, 0.9 if any(a <= ct <= b for a, b in self.speech) else 0.02))
            k += 1
        return out

    def write_wav(self, t0, t1, path):
        self.rig.calls.append(("slice", round(t0 - T0, 1), round(t1 - T0, 1)))
        return write_wav(path, seconds=1.0)


class FakeSynth:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, path):
        self.calls.append(text)
        write_wav(path, seconds=WAV_SECONDS)
        return True


class FakeTranscriber:
    """Hears the next of ``texts`` on each call (the last one repeats), 0.3 s per
    word, taking ``delay`` real seconds."""

    def __init__(self, *texts, delay=0.0):
        self.texts = list(texts)
        self.delay = delay
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        time.sleep(self.delay)
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return [Word(f" {w}", i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(text.split())]


class Rig:
    """An Interaction wired to fakes, recording what it plays and how long it waits.

    Without ``speech`` it has no audio tap and records a fixed window (the
    fallback); with it, it listens on a FakeTap.
    """

    def __init__(
        self,
        settings,
        *,
        answer="I have a package for you",
        answer2="",
        capture_ok=True,
        connect_fails=(),
        presynth_keys=("greeting", *ALL_REPLY_KEYS),
        speech=None,
        tap_up_until=math.inf,
        lag=0.0,
        sent_delay=None,
        wav_seconds=WAV_SECONDS,
        present=None,
        grab_frame=None,
        speech_check=lambda path, start, end: 10.0,  # Silero heard the answer
    ):
        self.settings = settings
        self.calls: list = []
        self.sleeps: list[float] = []
        self.now = T0
        self.lag, self.sent_delay = lag, sent_delay
        self.connects = 0
        self.connect_fails = set(connect_fails)
        self.synth = FakeSynth()
        self.capture_ok = capture_ok
        presynth = {
            key: write_wav(settings.out_dir / f"_presynth_{key}.wav", seconds=wav_seconds)
            for key in presynth_keys
        }
        self.tap = None if speech is None else FakeTap(self, speech, tap_up_until)
        self.interaction = Interaction(
            settings,
            synth=self.synth,
            transcriber=FakeTranscriber(
                f"{settings.greeting} {answer}", f"{settings.reply_no_answer} {answer2}"
            ),
            presynth=presynth,
            event_log=EventLog(settings.event_log_file),
            connect=self.connect,
            tap=self.tap,
            present=present,
            grab_frame=grab_frame,
            speech_check=speech_check,
            capture=self.capture,
            sleep=self.sleep,
            clock=lambda: 1234.5,
            webrtc_available=True,
        )

    async def connect(self):
        self.connects += 1
        n = self.connects
        if n in self.connect_fails:
            self.calls.append(("connect failed", n))
            return None
        self.calls.append(("connect", n))
        return FakeSession(self, n)

    def capture(self, url, path, seconds):
        self.calls.append(("capture", url, path.name, seconds))
        if not self.capture_ok:
            return None
        return write_wav(path, seconds=1.0)

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)

    def log_records(self):
        f = self.settings.event_log_file
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]


def window_seconds(settings):
    return math.ceil(WAV_SECONDS + TALKBACK_LATENCY_SECONDS + settings.listen_seconds)


# Without the audio tap: a fixed recording window per prompt.


def test_full_visit_record(settings):
    rig = Rig(settings)
    visit = rig.interaction.run(EID)

    assert rig.calls == [
        ("connect", 1),
        ("play", "_presynth_greeting.wav"),
        ("capture", "rtsp://go2rtc:8554/front_door", f"{EID}.wav", window_seconds(settings)),
        ("play", "_presynth_delivery.wav"),
        ("close", 1),
    ]
    # One session for the whole visit, held open until the reply has been heard.
    assert rig.sleeps == [PLAYBACK_HOLD_SECONDS]
    assert visit.outcome == "completed"
    assert rig.log_records() == [
        {
            "ts": 1234.5,
            "event_id": EID,
            "camera": "front_door",
            "trigger": "person",
            "outcome": "completed",
            "classification": "likely_delivery",
            "transcript": "I have a package for you",
            "response": "Thank you. Please leave the package at the door.",
            "turns": 1,
            "endpoint": "window",
            "reply_latency": None,
            "flow": None,
            "details": {},
            "dialogue": [{"asked": "greeting", "heard": "I have a package for you"}],
        }
    ]


def test_talkback_stays_open_until_a_late_clip_has_been_sent(settings, caplog):
    # On a slow host (an HA OS VM) the greeting took longer than its duration
    # to send and closing on the clock cut it off.
    rig = Rig(settings, sent_delay=0.05, lag=1.5)
    visit = rig.interaction.run(EID)
    assert visit.outcome == "completed"
    names = [c for c in rig.calls if c[0] in ("sent", "close")]
    assert names[-2:] == [("sent", "_presynth_delivery.wav"), ("close", 1)]
    assert "clip finished playing 1.5 s late" in caplog.text


def test_talkback_gives_up_waiting_for_a_clip_that_never_finishes(settings, monkeypatch):
    from gatekeeper import interaction

    monkeypatch.setattr(interaction, "MAX_PLAYBACK_LAG_SECONDS", 0.05)
    rig = Rig(settings, sent_delay=math.inf, wav_seconds=0.05)
    visit = rig.interaction.run(EID)
    assert visit.outcome == "completed"
    assert rig.calls[-1] == ("close", 1)


def test_recording_starts_while_greeting_plays(settings):
    # P0-23: the visitor answers as soon as the greeting ends, so recording
    # starts with it.
    rig = Rig(settings)
    rig.interaction.run(EID)
    names = [c[0] for c in rig.calls]
    assert names.index("play") < names.index("capture") < names.index("close")


def test_greeting_echo_alone_is_no_answer(settings):
    rig = Rig(settings, answer="")
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert record["outcome"] == "completed"
    assert record["transcript"] == ""
    assert record["classification"] == "no_response"
    assert record["response"] == settings.reply_no_answer


def test_failed_capture_gives_no_answer_reply(settings):
    rig = Rig(settings, capture_ok=False)
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert record["outcome"] == "completed"
    assert record["classification"] == "no_response"
    assert record["transcript"] == ""
    assert record["response"] == settings.reply_no_answer
    assert ("play", "_presynth_no_answer.wav") in rig.calls


def test_missing_presynth_is_synthesized_on_demand(settings):
    rig = Rig(settings, presynth_keys=())
    rig.interaction.run(EID)
    assert rig.synth.calls == [settings.greeting, settings.reply_delivery]
    assert ("play", f"{EID}_greeting.wav") in rig.calls
    assert ("play", f"{EID}_delivery.wav") in rig.calls


def test_custom_reply_text_is_logged(settings):
    settings.reply_delivery = "Leave it by the bench, thanks."
    rig = Rig(settings)
    rig.interaction.run(EID)
    assert rig.log_records()[0]["response"] == "Leave it by the bench, thanks."


def test_failed_reply_is_logged_with_what_was_heard(settings):
    settings.talkback_session = "clip"
    rig = Rig(settings, connect_fails={2})
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert record["outcome"] == "reply_failed"
    assert record["classification"] == "likely_delivery"
    assert record["transcript"] == "I have a package for you"


def test_failed_greeting_is_logged(settings):
    # P0-16 (H5): v0.1 lost these visits entirely.
    rig = Rig(settings, connect_fails={1})
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert record["outcome"] == "talkback_failed"
    assert record["classification"] is None
    assert record["response"] is None
    assert not any(c[0] == "capture" for c in rig.calls)


def test_busy_talkback_is_logged(settings):
    # P0-16 (H4): v0.1 dropped the second visitor silently.
    rig = Rig(settings)
    rig.interaction._talkback_lock.acquire()
    try:
        rig.interaction.run(EID)
    finally:
        rig.interaction._talkback_lock.release()
    assert rig.calls == []
    (record,) = rig.log_records()
    assert record["outcome"] == "talkback_busy"


def test_error_is_logged_and_lock_released(settings):
    rig = Rig(settings)

    def broken_capture(*_):
        raise OSError("ffmpeg missing")

    rig.interaction._capture = broken_capture
    rig.interaction.run(EID)
    assert rig.log_records()[0]["outcome"] == "error"
    assert ("close", 1) in rig.calls
    assert rig.interaction._talkback_lock.acquire(blocking=False)


def test_hung_capture_times_out(settings):
    # P0-15 (H2): a capture that never returns must not hold the doorbell.
    settings.visit_timeout_seconds = 0.3
    rig = Rig(settings)
    release = threading.Event()

    def hung_capture(*_):
        release.wait(10)

    rig.interaction._capture = hung_capture
    started = time.monotonic()
    try:
        visit = rig.interaction.run(EID)
    finally:
        release.set()
    assert time.monotonic() - started < 3
    assert visit.outcome == "timeout"
    assert rig.log_records()[0]["outcome"] == "timeout"
    assert ("close", 1) in rig.calls
    assert rig.interaction._talkback_lock.acquire(blocking=False)


def test_no_webrtc_is_logged(settings):
    rig = Rig(settings)
    rig.interaction._webrtc_available = False
    rig.interaction.run(EID)
    assert rig.calls == []
    assert rig.log_records()[0]["outcome"] == "talkback_unavailable"


def test_sequential_visits_on_worker_threads(settings):
    rig = Rig(settings)
    for i in range(3):
        t = threading.Thread(target=rig.interaction.run, args=(f"e{i}",))
        t.start()
        t.join()
    assert [r["event_id"] for r in rig.log_records()] == ["e0", "e1", "e2"]


def test_notify_fires_once_right_after_classification(settings):
    rig = Rig(settings)
    notified = []

    def on_notify(visit):
        notified.append((visit.classification, visit.transcript, visit.response))
        rig.calls.append(("notify",))

    rig.interaction._on_notify = [on_notify]
    rig.interaction.run(EID)
    assert notified == [("likely_delivery", "I have a package for you", settings.reply_delivery)]
    # Before the reply is played: the resident hears about it as soon as possible.
    assert rig.calls.index(("notify",)) < rig.calls.index(("play", "_presynth_delivery.wav"))


def test_notify_fires_for_visits_that_fail(settings):
    rig = Rig(settings, connect_fails={1})
    notified = []
    rig.interaction._on_notify = [notified.append]
    rig.interaction.run(EID)
    (visit,) = notified
    assert visit.outcome == "talkback_failed"
    assert visit.classification is None


def test_logged_hook_gets_the_record_and_survives_errors(settings):
    rig = Rig(settings)
    records = []

    def broken(record):
        raise RuntimeError("mqtt down")

    rig.interaction._on_logged = [broken, records.append]
    rig.interaction._on_notify = [broken]
    rig.interaction.run(EID)
    (record,) = records
    assert record == rig.log_records()[0]
    assert record["outcome"] == "completed"


def test_clip_mode_connects_for_each_phrase(settings):
    settings.talkback_session = "clip"
    rig = Rig(settings)
    rig.interaction.run(EID)
    connects = [c for c in rig.calls if c[0] in ("connect", "play")]
    assert connects == [
        ("connect", 1),
        ("play", "_presynth_greeting.wav"),
        ("connect", 2),
        ("play", "_presynth_delivery.wav"),
    ]
    assert sorted(c for c in rig.calls if c[0] == "close") == [("close", 1), ("close", 2)]


# With the audio tap: listen until the visitor stops talking.


def test_tap_ends_the_turn_when_the_visitor_stops_talking(settings):
    rig = Rig(settings, speech=[(ECHO_END + 0.1, ECHO_END + 1.5)])
    rig.interaction.run(EID)

    assert not any(c[0] == "capture" for c in rig.calls)
    (cut,) = [c for c in rig.calls if c[0] == "slice"]
    # From the greeting's start to 0.8 s of silence after the answer.
    assert cut[1] == 0.0
    assert cut[2] == pytest.approx(ECHO_END + 1.5 + 0.8 - T0, abs=0.15)
    (record,) = rig.log_records()
    assert (record["endpoint"], record["classification"]) == ("answered", "likely_delivery")
    # Measured from the end of speech, so it includes the 0.8 s of silence. (No upper
    # bound: the fake clock runs on while the transcription thread finishes.)
    assert record["reply_latency"] >= 0.8


def test_tap_silence_ends_at_the_no_input_deadline_and_asks_again(settings):
    rig = Rig(settings, answer="", answer2="", speech=[])
    rig.interaction.run(EID)
    slices = [c for c in rig.calls if c[0] == "slice"]
    assert slices[0][2] == pytest.approx(ECHO_END + settings.listen_seconds - T0, abs=0.1)
    assert len(slices) == 2
    (record,) = rig.log_records()
    assert (record["endpoint"], record["turns"], record["reply_latency"]) == ("no_input", 2, None)


def test_tap_echo_is_not_mistaken_for_an_answer(settings):
    # Something speech-like during the greeting's echo, then nothing.
    rig = Rig(settings, answer="", answer2="", speech=[(T0 + 2.6, ECHO_END - 0.6)])
    rig.interaction.run(EID)
    assert rig.log_records()[0]["endpoint"] == "no_input"


def test_tap_visitor_who_keeps_talking_after_a_pause_is_heard_in_full(settings):
    rig = Rig(settings, speech=[(ECHO_END + 0.1, ECHO_END + 1.0), (ECHO_END + 2.0, ECHO_END + 3.0)])
    rig.interaction._transcriber = FakeTranscriber(
        f"{settings.greeting} I have a package",
        f"{settings.greeting} I have a package for Sam",
        delay=0.2,
    )
    rig.interaction.run(EID)
    slices = [c for c in rig.calls if c[0] == "slice"]
    assert len(slices) == 2
    assert slices[1][2] == pytest.approx(ECHO_END + 3.0 + 0.8 - T0, abs=0.15)
    (record,) = rig.log_records()
    assert record["transcript"] == "I have a package for Sam"
    assert record["endpoint"] == "answered"


def test_tap_down_falls_back_to_a_fixed_window(settings):
    rig = Rig(settings, speech=[], tap_up_until=T0 - 1)
    rig.interaction.run(EID)
    assert any(c[0] == "capture" for c in rig.calls)
    assert rig.log_records()[0]["endpoint"] == "window"


def test_tap_lost_mid_turn_transcribes_what_it_had(settings):
    rig = Rig(settings, speech=[], tap_up_until=T0 + 3.0)
    rig.interaction.run(EID)
    (cut, *_) = [c for c in rig.calls if c[0] == "slice"]
    assert cut[2] == pytest.approx(3.0, abs=0.1)
    record = rig.log_records()[0]
    assert record["classification"] == "likely_delivery"
    assert record["endpoint"] == "tap_lost"


# Doorbell pressed during a visit's cooldown.


def press_visit():
    from gatekeeper.interaction import Visit

    return Visit("press-1.0", "front_door", outcome="pressed_during_visit", trigger="button")


def test_press_during_cooldown_is_answered_and_logged(settings):
    rig = Rig(settings, presynth_keys=("greeting", *ALL_REPLY_KEYS, "pressed"))
    notified = []
    rig.interaction._on_notify = [lambda v: notified.append(rig.calls[:])]
    visit = rig.interaction.acknowledge_press(press_visit())
    assert notified == [[]]  # notified before anything was played
    assert rig.calls == [("connect", 1), ("play", "_presynth_pressed.wav"), ("close", 1)]
    assert rig.sleeps == [PLAYBACK_HOLD_SECONDS]
    assert visit.response == settings.reply_pressed
    (record,) = rig.log_records()
    assert record["outcome"] == "pressed_during_visit"
    assert record["trigger"] == "button"
    assert record["response"] == settings.reply_pressed


def test_press_during_an_active_visit_is_only_notified(settings):
    rig = Rig(settings)
    notified = []
    rig.interaction._on_notify = [notified.append]
    rig.interaction._talkback_lock.acquire()
    try:
        rig.interaction.acknowledge_press(press_visit())
    finally:
        rig.interaction._talkback_lock.release()
    assert rig.calls == []
    assert len(notified) == 1
    assert rig.log_records()[0]["response"] is None


def test_press_reply_failing_to_connect_is_logged_without_response(settings):
    rig = Rig(settings, connect_fails={1},
              presynth_keys=("greeting", *ALL_REPLY_KEYS, "pressed"))  # fmt: skip
    rig.interaction.acknowledge_press(press_visit())
    assert rig.log_records()[0]["response"] is None
    assert rig.interaction._talkback_lock.acquire(blocking=False)


# No answer: the no-answer reply asks again and Gatekeeper listens once more.


def test_answer_on_the_second_turn(settings):
    rig = Rig(settings, answer="", answer2="I'm visiting my friend Sam")
    notified = []
    rig.interaction._on_notify = [lambda v: notified.append((v.classification, v.transcript))]
    visit = rig.interaction.run(EID)
    assert [c[:3] for c in rig.calls] == [
        ("connect", 1),
        ("play", "_presynth_greeting.wav"),
        ("capture", "rtsp://go2rtc:8554/front_door", f"{EID}.wav"),
        ("play", "_presynth_no_answer.wav"),
        ("capture", "rtsp://go2rtc:8554/front_door", f"{EID}-2.wav"),
        ("play", "_presynth_generic.wav"),
        ("close", 1),
    ]
    assert notified == [("cooperative_other", "I'm visiting my friend Sam")]  # once, final
    assert (visit.turns, visit.outcome, visit.response) == (2, "completed", settings.reply_generic)


def test_silent_twice_ends_after_the_question(settings):
    rig = Rig(settings, answer="", answer2="")
    notified = []
    rig.interaction._on_notify = [notified.append]
    rig.interaction.run(EID)
    assert [c[:2] for c in rig.calls if c[0] == "play"] == [
        ("play", "_presynth_greeting.wav"),
        ("play", "_presynth_no_answer.wav"),
    ]
    (record,) = rig.log_records()
    assert record["classification"] == "no_response"
    assert record["response"] == settings.reply_no_answer
    assert record["turns"] == 2 and record["outcome"] == "completed"
    assert len(notified) == 1


def test_failed_second_question_is_logged(settings):
    settings.talkback_session = "clip"
    rig = Rig(settings, answer="", connect_fails={2})
    notified = []
    rig.interaction._on_notify = [lambda v: notified.append(v.outcome)]
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert (record["outcome"], record["turns"], record["response"]) == ("reply_failed", 1, None)
    assert notified == ["reply_failed"]  # the notification knows why


def test_words_whisper_made_up_from_noise_are_not_an_answer(settings):
    # 2026-10-08: "Thanks for watching!" and "I'll be in for it" x3 from people
    # walking past; Silero heard no speech at all in either clip.
    checked = []

    def no_speech(path, start, end):
        checked.append((path.name, start < end))
        return 0.0

    rig = Rig(settings, answer="Thanks for watching!", answer2="", speech_check=no_speech)
    visit = rig.interaction.run(EID)
    assert checked[0] == (f"{EID}.wav", True)
    assert (visit.classification, visit.transcript) == ("no_response", "")
    assert visit.response == settings.reply_no_answer
    assert visit.dialogue[0] == {"asked": "greeting", "heard": ""}


def test_speech_check_that_fails_keeps_the_answer(settings):
    rig = Rig(settings, speech_check=lambda path, start, end: None)
    assert rig.interaction.run(EID).classification == "likely_delivery"
