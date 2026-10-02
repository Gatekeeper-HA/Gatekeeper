import asyncio
import json
import math
import threading
import time
from pathlib import Path

from conftest import write_wav
from gatekeeper.eventlog import EventLog
from gatekeeper.interaction import PLAYBACK_TAIL_SECONDS, Interaction
from gatekeeper.listen import Word

EID = "1727712000.123456-abc123"
ALL_REPLY_KEYS = ("no_answer", "delivery", "sales", "maintenance", "generic")
GREETING_WAV_SECONDS = 2.0


class FakePC:
    def __init__(self, wav: Path, log: list):
        self.wav = wav
        self.log = log

    async def close(self):
        self.log.append(("close", self.wav.name))


class FakeSynth:
    def __init__(self):
        self.calls = []

    def synthesize(self, text, path):
        self.calls.append(text)
        write_wav(path, seconds=GREETING_WAV_SECONDS)
        return True


class FakeTranscriber:
    """Hears ``text`` spread over the clip, 0.3 s per word."""

    def __init__(self, text):
        self.text = text

    def transcribe(self, path):
        return [Word(f" {w}", i * 0.3, i * 0.3 + 0.25) for i, w in enumerate(self.text.split())]


class Rig:
    """An Interaction wired to fakes, recording what it plays and how long it waits."""

    def __init__(
        self,
        settings,
        *,
        answer="I have a package for you",
        capture_ok=True,
        play_fails=(),
        presynth_keys=("greeting", *ALL_REPLY_KEYS),
    ):
        self.settings = settings
        self.calls: list = []
        self.sleeps: list[float] = []
        self.synth = FakeSynth()
        self.play_fails = set(play_fails)
        self.capture_ok = capture_ok
        self.capture_started = threading.Event()
        presynth = {
            key: write_wav(
                settings.out_dir / f"_presynth_{key}.wav", seconds=GREETING_WAV_SECONDS
            )
            for key in presynth_keys
        }
        self.interaction = Interaction(
            settings,
            synth=self.synth,
            transcriber=FakeTranscriber(f"{settings.greeting} {answer}"),
            presynth=presynth,
            event_log=EventLog(settings.event_log_file),
            play=self.play,
            capture=self.capture,
            sleep=self.sleep,
            clock=lambda: 1234.5,
            webrtc_available=True,
        )

    async def play(self, wav):
        self.calls.append(("play", wav.name))
        if wav.name in self.play_fails:
            return None
        return FakePC(wav, self.calls)

    def capture(self, url, path, seconds):
        self.calls.append(("capture", url, path.name, seconds))
        self.capture_started.set()
        if not self.capture_ok:
            return None
        return write_wav(path, seconds=1.0)

    async def sleep(self, seconds):
        # Give the concurrent capture a chance to start, as real time would.
        for _ in range(200):
            if self.capture_started.is_set():
                break
            await asyncio.sleep(0.005)
        self.sleeps.append(seconds)

    def log_records(self):
        f = self.settings.event_log_file
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]


def test_full_visit_record(settings):
    rig = Rig(settings)
    visit = rig.interaction.run(EID)

    capture_seconds = math.ceil(GREETING_WAV_SECONDS + PLAYBACK_TAIL_SECONDS + 4)
    assert rig.calls == [
        ("play", "_presynth_greeting.wav"),
        ("capture", "rtsp://go2rtc:8554/front_door", f"{EID}.wav", capture_seconds),
        ("close", "_presynth_greeting.wav"),
        ("play", "_presynth_delivery.wav"),
        ("close", "_presynth_delivery.wav"),
    ]
    tail = GREETING_WAV_SECONDS + PLAYBACK_TAIL_SECONDS
    assert rig.sleeps == [tail, tail]
    assert visit.outcome == "completed"
    assert rig.log_records() == [
        {
            "ts": 1234.5,
            "event_id": EID,
            "camera": "front_door",
            "outcome": "completed",
            "classification": "likely_delivery",
            "transcript": "I have a package for you",
            "response": "Thank you. Please leave the package at the door.",
        }
    ]


def test_recording_starts_while_greeting_plays(settings):
    # P0-23: the visitor answers as soon as the greeting ends, so recording
    # must already be running before the greeting's talkback closes.
    rig = Rig(settings)
    rig.interaction.run(EID)
    names = [c[0] for c in rig.calls]
    assert names.index("capture") < names.index("close")


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
    assert ("play", f"{EID}_reply.wav") in rig.calls


def test_custom_reply_text_is_logged(settings):
    settings.reply_delivery = "Leave it by the bench, thanks."
    rig = Rig(settings)
    rig.interaction.run(EID)
    assert rig.log_records()[0]["response"] == "Leave it by the bench, thanks."


def test_failed_reply_is_logged_with_what_was_heard(settings):
    rig = Rig(settings, play_fails={"_presynth_delivery.wav"})
    rig.interaction.run(EID)
    (record,) = rig.log_records()
    assert record["outcome"] == "reply_failed"
    assert record["classification"] == "likely_delivery"
    assert record["transcript"] == "I have a package for you"


def test_failed_greeting_is_logged(settings):
    # P0-16 (H5): v0.1 lost these visits entirely.
    rig = Rig(settings, play_fails={"_presynth_greeting.wav"})
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
        rig.capture_started.set()
        raise OSError("ffmpeg missing")

    rig.interaction._capture = broken_capture
    rig.interaction.run(EID)
    assert rig.log_records()[0]["outcome"] == "error"
    assert ("close", "_presynth_greeting.wav") in rig.calls
    assert rig.interaction._talkback_lock.acquire(blocking=False)


def test_hung_capture_times_out(settings):
    # P0-15 (H2): a capture that never returns must not hold the doorbell.
    settings.visit_timeout_seconds = 0.3
    rig = Rig(settings)
    release = threading.Event()

    def hung_capture(*_):
        rig.capture_started.set()
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
    assert ("close", "_presynth_greeting.wav") in rig.calls
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
    assert notified == [
        ("likely_delivery", "I have a package for you", settings.reply_delivery)
    ]
    # Before the reply is played: the resident hears about it as soon as possible.
    names = [c[0] for c in rig.calls]
    assert names.index("notify") < names.index("play", names.index("close"))


def test_notify_fires_for_visits_that_fail(settings):
    rig = Rig(settings, play_fails={"_presynth_greeting.wav"})
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
