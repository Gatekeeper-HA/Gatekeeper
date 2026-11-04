"""Threat levels (docs/10-occasions-and-threats.md): signs that a visit isn't what it seems,
a level from them, and what Gatekeeper does about it.

Gatekeeper never decides that someone is a criminal. It notices signs, raises a level, tells
the resident why, and stops engaging:

- 0, normal: no signs;
- 1, watch: one sign; the notification says what it was;
- 2, suspicious: a strong sign, or two others: Gatekeeper stops the conversation
  (PHRASE_THREAT_SUSPICIOUS) and the notification is urgent, with the reasons;
- 3, urgent: a threat, or several strong signs: PHRASE_THREAT_URGENT, and the same.

``THREAT_LEVELS``: "off", "shadow" (assessed, logged and in the daily report, nothing
else changes) or "on".

This first version reads the visitor's words: English phrases, like classify.py. Late at
night raises the other signs but never counts alone. What someone looks like, how they
talk or what language they speak never counts. Signs from the camera (a package taken, a
hidden face, the not-allowed list) come later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

POINTS = {"weak": 0.5, "moderate": 1.0, "strong": 2.0, "urgent": 4.0}
LEVEL_NAMES = ("normal", "watch", "suspicious", "urgent")


@dataclass(frozen=True)
class Sign:
    name: str
    weight: str  # weak, moderate, strong or urgent (POINTS)
    reason: str  # for the resident's phone: "asked whether anyone's home"


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


WHO = r"anyone|anybody|someone|somebody|they|he|she|the (?:owner|owners|homeowner|parents)"
# (sign, its patterns): each sign counts once however often it's heard.
WORD_SIGNS = [
    (
        Sign("asks_occupancy", "moderate", "asked whether anyone's home"),
        _any(
            rf"\b(?:is|are) (?:{WHO}|your (?:mom|dad|parents|wife|husband)) "
            r"(?:home|here|in|around|in there|inside)\b",
            r"\b(?:anyone|anybody|somebody|someone) (?:home|in there)\b",
            r"\bwho(?:'s| is) (?:home|inside|in there)\b",
            r"\b(?:home|are you|you) alone\b",
        ),
    ),
    (
        Sign("asks_schedule", "moderate", "asked when someone would be back"),
        _any(
            rf"\bwhen (?:will|are|do|does|is|would) (?:{WHO}|you)(?: \w+)?"
            r" (?:be back|be home|get home|get back|come back|come home|return)\b",
            rf"\bwhat time (?:do|does|will|would) (?:{WHO}|you)(?: \w+)?"
            r" (?:get|come|be) (?:home|back)\b",
            rf"\b(?:are|is) (?:{WHO}) (?:away|on vacation|out of town|at work)\b",
        ),
    ),
    (
        Sign("asks_entry", "moderate", "asked to come in"),
        _any(
            r"\blet(?:ting)? (?:me|us) in\b",
            r"\b(?:can|could|may) (?:i|we) (?:come|step|get) in(?:side)?\b",
            r"\bopen (?:the|this|your) door\b",
            r"\bopen up\b",
            r"\buse (?:your|the) (?:bathroom|restroom|toilet|phone)\b",
        ),
    ),
    (
        Sign("asks_code", "moderate", "asked for a code or about the alarm"),
        _any(
            r"\b(?:door|gate|garage|alarm|lock|lockbox|security|entry) code\b",
            r"\bwhat(?:'s| is) the code\b",
            r"\b(?:turn|switch|shut)(?:ing)? (?:off )?(?:the |your )?(?:alarm|security system)\b",
            r"\bdisarm\b",
        ),
    ),
    (
        Sign("scam_markers", "moderate", "sounded like a scam"),
        _any(
            r"\bfinal notice\b",
            r"\b(?:shut|cut|turn)(?:ting)? off (?:your|the) "
            r"(?:power|gas|water|electricity|electric|service)\b",
            r"\bdisconnect(?:ed|ion)?\b",
            r"\bpast due\b",
            r"\boverdue\b",
            r"\bpay (?:now|today|immediately|right now|in cash|cash)\b",
            r"\bgift cards?\b",
            r"\b(?:the irs|tax debt|back taxes)\b",
            r"\bwarrant for your arrest\b",
            r"\bbitcoin\b",
            r"\bwire (?:the )?(?:money|payment)\b",
            r"\bfree (?:security |home |roof )?inspection\b",
            r"\bleftover (?:asphalt|sealant|sealer|material|materials|paint)\b",
        ),
    ),
    (
        Sign("swearing", "moderate", "swore"),
        _any(r"\bf+u+c+k\w*", r"\bshit\w*", r"\bbitch\w*", r"\basshole\b"),
    ),
    (
        Sign("hostile", "strong", "demanded to be let in"),
        _any(
            r"\bopen (?:the|this|your) (?:damn|goddamn|fucking|freaking|f\w+ing) door\b",
            r"\bi know (?:you're|you are|someone's|somebody's|someone is|somebody is) "
            r"(?:in )?(?:there|home|inside)\b",
            r"\byou(?:'d| had)? better open\b",
            r"\b(?:open up|let me in) (?:right )?now\b",
        ),
    ),
    (
        Sign("threat", "urgent", "made a threat"),
        _any(
            r"\b(?:i'll|i will|i'm gonna|i'm going to|i am going to|gonna|going to) "
            r"(?:kill|hurt|shoot|stab|beat|burn|break in|break down|smash|kick in|kick down)\b",
            r"\b(?:break|kick|smash) (?:down |in )?(?:the|this|your) (?:door|window)s?\b",
            r"\byou(?:'re| are) (?:dead|gonna die|going to die)\b",
            r"\bi(?:'ve| have) (?:got )?a (?:gun|knife|weapon)\b",
        ),
    ),
]
LATE_NIGHT = Sign("late_night", "weak", "late at night")
NIGHT_HOURS = (23, 5)  # 11 pm to 5 am


@dataclass(frozen=True)
class Assessment:
    level: int
    signs: tuple[Sign, ...]

    def record(self, acted: bool) -> dict:
        """For the visit record (``details["threat"]``)."""
        return {
            "level": self.level,
            "signs": [s.name for s in self.signs],
            "reasons": [s.reason for s in self.signs],
            "acted": acted,
        }


# The visit log's outcomes that mean the household was there: went in, recognized by
# face, came out of the door, or came back soon after.
HOUSEHOLD_LIKE = {"entered", "household", "left", "stepped_out", "returned"}
VISITOR_TRIGGERS = ("person", "button", "knock")


def package_taken(visits: list[dict]) -> dict | None:
    """A package picked up after ``visits`` (the visit log's records while someone was at the
    door): suspicious when only visitors came, none of the household. Then the record for
    the package's notification, with what the visitor said; else None. A courier collecting
    a return looks the same, so it's "suspicious", not "urgent", and the resident is told
    what they said."""
    if any(v.get("outcome") in HOUSEHOLD_LIKE for v in visits):
        return None
    greeted = [v for v in visits if v.get("trigger") in VISITOR_TRIGGERS]
    if not greeted:
        return None
    said = next((v["transcript"] for v in reversed(greeted) if v.get("transcript")), "")
    return {
        "level": 2,
        "signs": ["package_taken"],
        "reasons": ["took a package and left without going inside"],
        "said": said,
        "visitor": greeted[-1].get("event_id"),
    }


def word_signs(text: str) -> list[Sign]:
    normalized = " ".join(text.lower().replace("’", "'").split())
    return [sign for sign, pattern in WORD_SIGNS if pattern.search(normalized)]


def level(signs: list[Sign] | tuple[Sign, ...]) -> int:
    """The level from the signs (each counted once): weak signs alone are nothing."""
    if all(s.weight == "weak" for s in signs):
        return 0
    if any(s.weight == "urgent" for s in signs):
        return 3
    points = sum(POINTS[s.weight] for s in signs)
    if sum(s.weight == "strong" for s in signs) >= 2 or points >= 4:
        return 3
    return 2 if points >= 2 else 1


def assess(texts: list[str], hour: int | None = None) -> Assessment:
    """What the visitor said (each answer heard), at ``hour`` (local, 0-23)."""
    signs = word_signs(" ".join(texts))
    start, end = NIGHT_HOURS
    if signs and hour is not None and (hour >= start or hour < end):
        signs.append(LATE_NIGHT)
    return Assessment(level(signs), tuple(signs))
