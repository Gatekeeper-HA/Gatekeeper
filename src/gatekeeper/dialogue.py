"""Conversations for visitors who need more than one reply.

When the visitor's answer is classified as an emergency, law enforcement or a
civic visit (candidates, campaigns, canvassers), one of these scripted
conversations takes over the rest of the visit:

- emergency: alert the resident urgently and ask the visitor to wait. Never
  sends anyone away.
- law_enforcement: alert the resident at once; ask the reason and whether
  they have a warrant (unless their first sentence said), then their agency,
  name and badge number. With a
  warrant, ask them to hold it up to the camera (photos are kept; if they
  answer first, to keep holding it until they're taken) and whether a
  judge signed it. A judge-signed warrant: "I'm notifying the resident".
  Otherwise: the resident doesn't consent, please leave; and, if they're still
  on the porch after a while, the second warning.
- civic: ask who they are and what message they'd like passed on, and promise
  to relay it.

Each spoken line is a setting (PHRASE_*). The lines state the resident's
position; they aren't legal advice.
"""

from __future__ import annotations

import re
from typing import Protocol

from gatekeeper.classify import classify_response

# How long the civic visitor may talk when giving their message.
MESSAGE_MAX_SECONDS = 30.0

_NEGATION = r"(?:no|not|don't|do not|doesn't|does not|haven't|have not|without|never)"


def yes_no(text: str) -> bool | None:
    """A yes or no answer, or None if it's neither."""
    t = text.lower()
    if re.search(r"\b(?:no|nope|negative|nah)\b|\b(?:don't|do not|not)\b", t):
        return False
    # "It is." answers yes; "Here it is." only shows something.
    if re.search(r"\b(?:yes|yeah|yep|yup|correct|affirmative|we do|i do|sure)\b", t) or re.search(
        r"(?<!here )\bit is\b", t
    ):
        return True
    return None


def has_warrant(text: str) -> bool | None:
    """Do they say they have a warrant? None if they didn't say."""
    t = text.lower()
    if re.search(rf"\b{_NEGATION}\b[\w\s']{{0,20}}\bwarrants?\b", t):
        return False
    if re.search(r"\bwarrants?\b", t):
        return True
    return yes_no(t)


# What makes an officer's first sentence more than "I'm a police officer": why they've
# come (Gatekeeper#23: "I'm a police officer looking for …").
_REASON = re.compile(
    r"\b(?:looking for|searching for|here (?:about|for|to|regarding|with)|regarding|"
    r"investigat\w*|complaints?|reports?|reported|call(?:ed)? about|check(?:ing)? on|"
    r"speak (?:to|with)|talk (?:to|with)|questions?|serv(?:e|ing)|arrest\w*|"
    r"follow(?:ing)? up|need to|want to|would like to|we'd like to|warrants?)\b"
)


def gave_reason(text: str) -> bool:
    """Does an officer's sentence say why they've come?"""
    return bool(_REASON.search(text.lower()))


def judge_signed(text: str) -> bool | None:
    """Is the warrant signed by a judge? None if they didn't say."""
    t = text.lower()
    if re.search(r"\badministrative\b", t):
        return False  # signed by an agency, not a court
    if re.search(rf"\b{_NEGATION}\b[\w\s']{{0,20}}\b(?:signed|judge|magistrate)\b", t):
        return False
    if re.search(r"\b(?:judge|magistrate|court)\b", t) or re.search(r"\bsigned\b", t):
        return True
    return yes_no(t)


class Turns(Protocol):
    """What a conversation can do during a visit (see Interaction._Turns)."""

    async def ask(
        self,
        key: str,
        max_answer: float | None = None,
        photos: bool = False,
        preface: str | None = None,
        hold: tuple[str, str] | None = None,
    ) -> str | None:
        """Say phrase ``key`` and return the visitor's answer ("" if silent,
        None if talkback failed). With ``photos``, keep full-resolution photos
        of what's in front of the camera while they answer; with ``hold`` (two
        phrase keys) as well, if they answer before the photos are all taken,
        say the first, then the second once they are. With ``preface``, say
        that phrase first, right before it."""

    async def say(self, key: str) -> bool:
        """Say phrase ``key`` and wait until it has been heard. False if talkback failed."""

    def alert(self) -> None:
        """Send the resident an interim (urgent) notification now."""

    def notify(self) -> None:
        """Send the resident the visit's notification now (once per visit)."""

    async def wait(self, seconds: float) -> None: ...

    def present(self) -> bool:
        """Is someone still on the porch?"""


async def run(conversation: str, visit, turns: Turns, settings) -> None:
    flows = {"emergency": emergency, "law_enforcement": law_enforcement, "civic": civic}
    await flows[conversation](visit, turns, settings)


async def emergency(visit, turns: Turns, settings) -> None:
    visit.flow = visit.classification = "emergency"  # also when it came up mid-conversation
    visit.response = settings.phrases["emergency_wait"]
    turns.notify()  # urgent, before anything else
    visit.outcome = "completed" if await turns.say("emergency_wait") else "reply_failed"


async def law_enforcement(visit, turns: Turns, settings) -> None:
    visit.flow = "law_enforcement"
    details = visit.details
    turns.alert()  # the resident hears about it at once, whatever happens next

    # The officer may have said why they've come, or whether they have a warrant,
    # in their first sentence; then don't ask again (Gatekeeper#23). The disclosure
    # that starts police_ask_reason is still said, before the next question.
    first = visit.transcript
    disclose: str | None = None
    if gave_reason(first) or has_warrant(first) is not None:
        reason, disclose = first, "police_disclosure"
    else:
        reason = await turns.ask("police_ask_reason")
        if reason is None:
            visit.outcome = "reply_failed"
            return
        if classify_response(reason)[0] == "emergency":
            details["reason"] = reason
            await emergency(visit, turns, settings)
            return
    details["reason"] = reason
    warrant = has_warrant(reason)
    if warrant is None:
        answer = await turns.ask("police_ask_warrant", preface=disclose)
        disclose = None
        warrant = bool(answer and has_warrant(answer))
    details["warrant"] = warrant

    identity = await turns.ask("police_ask_identity", preface=disclose)
    details["identity"] = identity or ""

    if warrant:
        shown = await turns.ask(
            "police_show_warrant", photos=True, hold=("police_hold_warrant", "police_warrant_done")
        )
        details["judge_signed"] = bool(shown and judge_signed(shown))

    if warrant and details.get("judge_signed"):
        details["decision"] = "notified"
        visit.response = settings.phrases["police_notifying"]
        visit.outcome = "completed" if await turns.say("police_notifying") else "reply_failed"
        return

    details["decision"] = "asked_to_leave"
    visit.response = settings.phrases["police_leave"]
    if not await turns.say("police_leave"):
        visit.outcome = "reply_failed"
        return
    await turns.wait(settings.leave_check_seconds)
    details["left"] = not turns.present()
    if not details["left"]:
        details["decision"] = "asked_to_leave_twice"
        visit.response = settings.phrases["police_leave_again"]
        await turns.say("police_leave_again")
    visit.outcome = "completed"


async def civic(visit, turns: Turns, settings) -> None:
    visit.flow = "civic"
    details = visit.details
    identity = await turns.ask("civic_ask_identity")
    if identity is None:
        visit.outcome = "reply_failed"
        return
    details["identity"] = identity
    details["message"] = await turns.ask("civic_ask_message", MESSAGE_MAX_SECONDS) or ""
    visit.response = settings.phrases["civic_thanks"]
    visit.outcome = "completed" if await turns.say("civic_thanks") else "reply_failed"
