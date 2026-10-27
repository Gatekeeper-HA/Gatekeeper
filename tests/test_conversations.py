"""The multi-turn conversations (dialogue.py) through a whole visit, on the
fake rig from test_interaction (fixed recording windows, instant playback)."""

import threading
import time

from conftest import write_wav
from gatekeeper.config import PHRASE_KEYS
from gatekeeper.interaction import _Turns
from gatekeeper.notify import compose
from test_interaction import ALL_REPLY_KEYS, ECHO_END, EID, FakeTranscriber, Rig

KEYS = ("greeting", *ALL_REPLY_KEYS, *PHRASE_KEYS)


def rig_for(settings, *answers, **kw):
    """A rig whose visitor answers each prompt in turn: [(phrase key, answer), ...]."""
    rig = Rig(settings, presynth_keys=KEYS, **kw)
    prompts = {"greeting": settings.greeting, **settings.replies, **settings.phrases}
    rig.interaction._transcriber = FakeTranscriber(
        *(f"{prompts[key]} {answer}" for key, answer in answers)
    )
    rig.notified = []

    def on_notify(visit):
        rig.notified.append(visit)
        rig.calls.append(("notify", visit.stage))

    rig.interaction._on_notify = [on_notify]
    return rig


def played(rig):
    """The phrase keys played, in order."""
    names = [c[1] for c in rig.calls if c[0] == "play"]
    return [n.removeprefix("_presynth_").removesuffix(".wav") for n in names]


# Civic visitors: candidates, campaigns, canvassers.


def test_candidate_is_asked_for_their_message_and_thanked(settings):
    rig = rig_for(
        settings,
        ("greeting", "Hi, I'm running for state senate"),  # no name: asked who they are
        ("civic_ask_identity", "Chris Doe, with my campaign"),
        ("civic_ask_message", "I'd appreciate your vote on November 3rd"),
    )
    visit = rig.interaction.run(EID)

    assert played(rig) == ["greeting", "civic_ask_identity", "civic_ask_message", "civic_thanks"]
    assert (visit.classification, visit.flow, visit.outcome) == ("civic", "civic", "completed")
    assert visit.details == {
        "identity": "Chris Doe, with my campaign",
        "message": "I'd appreciate your vote on November 3rd",
    }
    assert visit.response == settings.phrase_civic_thanks
    # One notification, at the end, with everything in it.
    assert [c for c in rig.calls if c[0] == "notify"] == [("notify", "final")]
    message = compose(rig.notified[0]).message
    assert 'Who: "Chris Doe, with my campaign"' in message
    assert 'Message: "I\'d appreciate your vote on November 3rd"' in message
    # The message may be long: its recording window is longer.
    (message_capture,) = [c for c in rig.calls if c[0] == "capture" and c[2] == f"{EID}-3.wav"]
    assert message_capture[3] > 10
    (record,) = rig.log_records()
    assert [d["asked"] for d in record["dialogue"]] == [
        "greeting", "civic_ask_identity", "civic_ask_message",
    ]  # fmt: skip


def test_voter_registration_canvasser_is_not_told_to_leave(settings):
    rig = rig_for(
        settings,
        ("greeting", "Just asking people to register to vote"),
        ("civic_ask_identity", "I'm Pat with the League of Women Voters"),
        ("civic_ask_message", "Election day is November 3rd"),
    )
    rig.interaction.run(EID)
    assert "sales" not in played(rig)
    assert played(rig)[-1] == "civic_thanks"


def test_canvasser_who_introduced_themselves_is_not_asked_who_they_are(settings):
    first = "My name is Sam. I'm a volunteer with Pat Lee and the Democrats, with some flyers"
    rig = rig_for(
        settings,
        ("greeting", first),
        ("civic_ask_message", "Please vote on November 3rd"),
    )
    visit = rig.interaction.run(EID)

    # Gatekeeper#78: their first sentence gave their name and who they're with.
    assert played(rig) == ["greeting", "civic_ask_message", "civic_thanks"]
    assert visit.details == {"identity": first, "message": "Please vote on November 3rd"}
    assert f'Who: "{first}"' in compose(rig.notified[0]).message


# Law enforcement.


def test_officer_without_a_warrant_is_asked_to_leave_once_if_they_go(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police, we're investigating a break-in down the street, no warrant"),
        ("police_ask_identity", "Officer Dana Smith, Springfield police, badge 4471"),
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)

    # Their first sentence said why, and that there's no warrant: neither is asked again,
    # but the disclosure that starts the reason question is still said (Gatekeeper#23).
    assert played(rig) == ["greeting", "police_disclosure", "police_ask_identity", "police_leave"]
    # The resident is alerted before Gatekeeper says anything more to the officer.
    assert rig.calls.index(("notify", "alert")) < rig.calls.index(
        ("play", "_presynth_police_disclosure.wav")
    )
    assert settings.leave_check_seconds in rig.sleeps
    assert visit.details == {
        "reason": "Police, we're investigating a break-in down the street, no warrant",
        "warrant": False,
        "identity": "Officer Dana Smith, Springfield police, badge 4471",
        "decision": "asked_to_leave",
        "left": True,
    }
    final = compose(rig.notified[-1])
    assert final.title == "Law enforcement asked to leave the front door"
    assert final.priority == 5
    assert "Warrant: none." in final.message and "Left when asked." in final.message


def test_officer_who_stays_gets_the_second_warning(settings):
    rig = rig_for(
        settings,
        ("greeting", "Sheriff's office, open the door"),
        ("police_ask_reason", "We don't have a warrant, we need to talk to you"),
        ("police_ask_identity", "Deputy Lee, badge 210"),
        present=lambda: True,
    )
    visit = rig.interaction.run(EID)
    assert played(rig)[-2:] == ["police_leave", "police_leave_again"]
    assert visit.response == settings.phrase_police_leave_again
    assert (visit.details["decision"], visit.details["left"]) == ("asked_to_leave_twice", False)
    assert compose(rig.notified[-1]).title == "Law enforcement didn't leave the front door"


def test_judge_signed_warrant_identity_first_then_resident_notified(settings):
    frames = []

    def grab(path):
        frames.append(path.name)
        # The officer answers before the photos are all taken (they take ~5 s).
        deadline = time.monotonic() + 10
        while "police_hold_warrant" not in played(rig) and time.monotonic() < deadline:
            time.sleep(0.01)
        return write_wav(path)  # any file stands in for the JPEG

    rig = rig_for(
        settings,
        ("greeting", "Sheriff's office, we have a search warrant for this address"),
        ("police_ask_identity", "Deputy Lee, Greene County Sheriff, badge 210"),
        ("police_show_warrant", "Yes, it's signed by Judge Martinez"),
        grab_frame=grab,
    )
    # Photo 5 is the sharpest (the page held still); it goes with the notification.
    rig.interaction._photo_score = lambda path: {"5": 90.0, "2": 40.0}.get(path.stem[-1], 10.0)
    visit = rig.interaction.run(EID)

    assert played(rig) == [
        "greeting", "police_disclosure", "police_ask_identity", "police_show_warrant",
        "police_hold_warrant", "police_warrant_done", "police_notifying",
    ]  # fmt: skip
    assert frames == [f"{EID}-{n}.jpg" for n in range(1, 7)]
    assert visit.image.name == f"{EID}-5.jpg"
    assert visit.details["photo_sent"] == f"{EID}-5.jpg"
    assert visit.details["photo_sharpness"] == [10.0, 40.0, 10.0, 10.0, 90.0, 10.0]
    assert visit.details["judge_signed"] is True
    assert visit.details["decision"] == "notified"
    final = compose(rig.notified[-1])
    assert final.title == "Law enforcement with a judge-signed warrant at the front door"
    assert "Warrant: yes, signed by a judge, they say." in final.message
    assert 'Identified as: "Deputy Lee, Greene County Sheriff, badge 210"' in final.message


def test_warrant_not_signed_by_a_judge_means_please_leave(settings):
    rig = rig_for(
        settings,
        ("greeting", "Immigration officers"),
        ("police_ask_reason", "We have a warrant"),
        ("police_ask_identity", "Agent Brown"),
        ("police_show_warrant", "It's an administrative warrant"),
        grab_frame=lambda path: None,
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)
    assert played(rig)[-1] == "police_leave"
    assert visit.details["judge_signed"] is False


def test_unclear_answer_about_a_warrant_gets_a_yes_or_no_question(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police department"),
        ("police_ask_reason", "We need to speak with the homeowner"),
        ("police_ask_warrant", "No"),
        ("police_ask_identity", "Officer Smith"),
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)
    assert played(rig)[2:4] == ["police_ask_warrant", "police_ask_identity"]
    assert visit.details["warrant"] is False


def test_without_presence_information_no_second_warning(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police"),
        ("police_ask_reason", "No warrant"),
        ("police_ask_identity", "Officer Smith"),
    )
    rig.interaction.run(EID)
    assert "police_leave_again" not in played(rig)


# Emergencies: never sent away.


def test_emergency_is_alerted_at_once_and_asked_to_wait(settings):
    rig = rig_for(settings, ("greeting", "Fire department, we got a 911 call about smoke"))
    visit = rig.interaction.run(EID)
    assert played(rig) == ["greeting", "emergency_wait"]
    assert rig.calls.index(("notify", "final")) < rig.calls.index(
        ("play", "_presynth_emergency_wait.wav")
    )
    assert (visit.classification, visit.outcome) == ("emergency", "completed")
    note = compose(rig.notified[0])
    assert note.title == "Emergency at the front door" and note.priority == 5


def test_emergency_that_comes_up_mid_conversation_with_police(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police officer"),
        ("police_ask_reason", "We're doing a welfare check, someone called 911"),
    )
    visit = rig.interaction.run(EID)
    assert played(rig) == ["greeting", "police_ask_reason", "emergency_wait"]
    assert "police_leave" not in played(rig)
    assert [c for c in rig.calls if c[0] == "notify"] == [("notify", "alert"), ("notify", "final")]
    assert visit.classification == "emergency"


def test_timeout_mid_conversation_still_notifies_what_it_learned(settings):
    settings.visit_timeout_seconds = 0.5
    release = threading.Event()
    rig = rig_for(
        settings,
        ("greeting", "Police"),
        ("police_ask_reason", "We have a few questions, no warrant"),
    )
    captures = []

    def capture(url, path, seconds):
        captures.append(path.name)
        if len(captures) == 3:  # the identity answer never comes back
            release.wait(10)
            return None
        return write_wav(path, seconds=1.0)

    rig.interaction._capture = capture
    try:
        visit = rig.interaction.run(EID)
    finally:
        release.set()
    assert visit.outcome == "timeout"
    final = compose(rig.notified[-1])
    assert 'Reason: "We have a few questions, no warrant"' in final.message


def test_conversation_reply_is_timed_from_the_end_of_the_answer(settings):
    # With the audio tap: the conversation's first phrase is the reply (Gatekeeper#25).
    rig = rig_for(
        settings,
        ("greeting", "This is a wellness check, is everyone okay in there"),
        speech=[(ECHO_END + 0.1, ECHO_END + 2.0)],
    )
    visit = rig.interaction.run(EID)

    assert played(rig) == ["greeting", "emergency_wait"]
    assert (visit.flow, visit.endpoint) == ("emergency", "answered")
    # From the end of speech, so at least the 0.8 s of end-of-speech silence.
    (record,) = rig.log_records()
    assert record["reply_latency"] >= 0.8


def test_unscored_warrant_photos_send_the_middle_one(settings):
    def grab(path):
        return write_wav(path)

    rig = rig_for(
        settings,
        ("greeting", "Sheriff's office"),
        ("police_ask_reason", "We have a search warrant"),
        ("police_ask_identity", "Deputy Lee, badge 210"),
        ("police_show_warrant", "Yes, signed by a judge"),
        grab_frame=grab,
    )
    rig.interaction._photo_score = lambda path: None  # e.g. PyAV not installed
    visit = rig.interaction.run(EID)
    assert visit.image.name == f"{EID}-4.jpg"
    assert visit.details["photo_sharpness"] == [None] * 6


def test_officer_who_says_why_but_not_about_a_warrant_is_asked_only_that(settings):
    # The owner's first officer test (2026-10-14).
    rig = rig_for(
        settings,
        ("greeting", "I'm a police officer looking for Joe Schmo."),
        ("police_ask_warrant", "No."),
        ("police_ask_identity", "Coon Rapids Police Department, Philly."),
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)
    assert played(rig) == [
        "greeting", "police_disclosure", "police_ask_warrant", "police_ask_identity",
        "police_leave",
    ]  # fmt: skip
    assert visit.details["reason"] == "I'm a police officer looking for Joe Schmo."
    assert visit.details["warrant"] is False
    (record,) = rig.log_records()
    assert [d["asked"] for d in record["dialogue"]] == [
        "greeting", "police_ask_warrant", "police_ask_identity",
    ]  # fmt: skip


def test_officer_who_only_says_police_is_asked_the_reason(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police department"),
        ("police_ask_reason", "We're looking for a witness, no warrant"),
        ("police_ask_identity", "Officer Kim, badge 12"),
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)
    assert played(rig)[:3] == ["greeting", "police_ask_reason", "police_ask_identity"]
    assert "police_disclosure" not in played(rig)
    assert visit.details["reason"] == "We're looking for a witness, no warrant"


def test_disclosure_with_a_session_per_phrase(settings):
    settings.talkback_session = "clip"
    rig = rig_for(
        settings,
        ("greeting", "I'm a police officer looking for Joe Schmo."),
        ("police_ask_warrant", "No."),
        ("police_ask_identity", "Officer Kim, badge 12"),
        present=lambda: False,
    )
    rig.interaction.run(EID)
    assert played(rig)[:3] == ["greeting", "police_disclosure", "police_ask_warrant"]


def test_no_hold_line_when_the_photos_were_taken_before_the_answer(settings, monkeypatch):
    # An officer reading the warrant out answers after the photos are all taken.
    taken = threading.Event()
    take_photos = _Turns._photos

    async def photos(self, playback):
        await take_photos(self, playback)
        taken.set()

    monkeypatch.setattr(_Turns, "_photos", photos)
    rig = rig_for(
        settings,
        ("greeting", "Sheriff's office, we have a search warrant"),
        ("police_ask_identity", "Deputy Lee, badge 210"),
        ("police_show_warrant", "Yes, it's signed by Judge Martinez"),
        grab_frame=lambda path: write_wav(path),
    )
    rig.interaction._photo_score = lambda path: 1.0
    captures = []

    def capture(url, path, seconds):
        captures.append(path.name)
        if len(captures) == 3:  # the warrant answer
            taken.wait(10)
        return write_wav(path, seconds=1.0)

    rig.interaction._capture = capture
    rig.interaction.run(EID)
    assert taken.is_set()
    assert played(rig)[-2:] == ["police_show_warrant", "police_notifying"]
