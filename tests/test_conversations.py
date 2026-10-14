"""The multi-turn conversations (dialogue.py) through a whole visit, on the
fake rig from test_interaction (fixed recording windows, instant playback)."""

import threading

from conftest import write_wav
from gatekeeper.config import PHRASE_KEYS
from gatekeeper.notify import compose
from test_interaction import ALL_REPLY_KEYS, EID, FakeTranscriber, Rig

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
        ("greeting", "Hi, I'm Jim Abeler, I'm running for state senate"),
        ("civic_ask_identity", "Jim Abeler, with my campaign"),
        ("civic_ask_message", "I'd appreciate your vote on November 3rd"),
    )
    visit = rig.interaction.run(EID)

    assert played(rig) == ["greeting", "civic_ask_identity", "civic_ask_message", "civic_thanks"]
    assert (visit.classification, visit.flow, visit.outcome) == ("civic", "civic", "completed")
    assert visit.details == {
        "identity": "Jim Abeler, with my campaign",
        "message": "I'd appreciate your vote on November 3rd",
    }
    assert visit.response == settings.phrase_civic_thanks
    # One notification, at the end, with everything in it.
    assert [c for c in rig.calls if c[0] == "notify"] == [("notify", "final")]
    message = compose(rig.notified[0]).message
    assert 'Who: "Jim Abeler, with my campaign"' in message
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


# Law enforcement.


def test_officer_without_a_warrant_is_asked_to_leave_once_if_they_go(settings):
    rig = rig_for(
        settings,
        ("greeting", "Police, we'd like to ask a few questions"),
        ("police_ask_reason", "We're investigating a break-in down the street, no warrant"),
        ("police_ask_identity", "Officer Dana Smith, Lino Lakes police, badge 4471"),
        present=lambda: False,
    )
    visit = rig.interaction.run(EID)

    assert played(rig) == ["greeting", "police_ask_reason", "police_ask_identity", "police_leave"]
    # The resident is alerted before Gatekeeper asks the officer anything.
    assert rig.calls.index(("notify", "alert")) < rig.calls.index(
        ("play", "_presynth_police_ask_reason.wav")
    )
    assert settings.leave_check_seconds in rig.sleeps
    assert visit.details == {
        "reason": "We're investigating a break-in down the street, no warrant",
        "warrant": False,
        "identity": "Officer Dana Smith, Lino Lakes police, badge 4471",
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
        return write_wav(path)  # any file stands in for the JPEG

    rig = rig_for(
        settings,
        ("greeting", "Sheriff's office, we have a warrant"),
        ("police_ask_reason", "We have a search warrant for this address"),
        ("police_ask_identity", "Deputy Lee, Anoka County Sheriff, badge 210"),
        ("police_show_warrant", "Yes, it's signed by Judge Martinez"),
        grab_frame=grab,
    )
    visit = rig.interaction.run(EID)

    assert played(rig) == [
        "greeting", "police_ask_reason", "police_ask_identity", "police_show_warrant",
        "police_notifying",
    ]  # fmt: skip
    assert frames == [f"{EID}-1.jpg", f"{EID}-2.jpg", f"{EID}-3.jpg"]
    assert visit.image.name == f"{EID}-2.jpg"  # the middle one goes with the notification
    assert visit.details["judge_signed"] is True
    assert visit.details["decision"] == "notified"
    final = compose(rig.notified[-1])
    assert final.title == "Law enforcement with a judge-signed warrant at the front door"
    assert "Warrant: yes, signed by a judge, they say." in final.message
    assert 'Identified as: "Deputy Lee, Anoka County Sheriff, badge 210"' in final.message


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
