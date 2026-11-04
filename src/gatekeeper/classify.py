"""Keyword classification of the visitor's answer.

Whole words and phrases only (so "ups" doesn't match "groups"). Categories
are checked in order and the first match wins:

1. emergency: someone reporting an emergency (never sent away);
2. law_enforcement: police and other officers (their own conversation);
3. civic: candidates, campaigns, canvassers (asked for their message);
4. solicitor, before delivery, so "I'm selling Amazon gift cards" is a solicitor;
5. delivery, then service.

English only: Whisper is run with language="en".
"""

from __future__ import annotations

import re

EMERGENCY_PHRASES = [
    "emergency", "welfare check", "wellness check", "911", "nine one one",
    "ambulance", "paramedic", "paramedics", "fire department", "firefighter",
    "firefighters", "on fire", "smoke", "gas leak", "carbon monoxide",
    "injured", "someone is hurt", "someone's hurt", "somebody is hurt", "medical",
]  # fmt: skip

# No bare "agent" (real estate agents) or "ice" (ice cream).
LAW_ENFORCEMENT_PHRASES = [
    "police", "policeman", "policewoman", "officer", "officers", "sheriff", "sheriff's",
    "deputy", "detective", "trooper", "state patrol", "highway patrol", "law enforcement",
    "marshal", "marshals", "warrant", "fbi", "dea", "atf", "special agent", "federal agent",
    "federal agents", "homeland security", "immigration", "ice agent", "ice agents",
    "ice officer", "ice officers", "police department",
]  # fmt: skip

CIVIC_PHRASES = [
    "candidate", "campaign", "campaigning", "running for", "election", "elections",
    "vote", "voting", "voter", "voters", "register to vote", "ballot", "senate",
    "senator", "representative", "legislature", "legislator", "city council",
    "council member", "councilmember", "mayor", "petition", "canvass", "canvassing",
    "canvasser", "canvassers", "political", "politics", "politician", "politicians", "pac",
    "dfl", "gop", "democrat", "democrats", "democratic", "republican", "republicans",
]  # fmt: skip

SOLICITOR_PHRASES = [
    "sales", "salesman", "selling", "sell", "solicit", "soliciting", "solicitation",
    "survey", "donate", "donation", "donations", "fundraiser", "fundraising", "charity",
    "raffle", "subscription", "magazine", "magazines", "special offer", "free estimate",
    "solar", "church", "bible", "ministry",
]  # fmt: skip

DELIVERY_PHRASES = [
    "delivery", "deliveries", "deliver", "delivering", "delivered", "drop off",
    "dropping off", "package", "packages", "parcel", "parcels", "box for",
    "fedex", "fed ex", "ups", "usps", "dhl", "amazon", "postal", "mail", "mailman",
    "courier", "doordash", "door dash", "uber eats", "ubereats", "grubhub",
    "instacart", "postmates", "your order", "your food", "pizza", "groceries",
    "sign for", "signature",
]  # fmt: skip

SERVICE_PHRASES = [
    "maintenance", "repair", "repairs", "repairman", "service", "servicing",
    "technician", "tech", "contractor", "landscaping", "landscaper", "lawn",
    "plumber", "plumbing", "electrician", "electric company", "gas company",
    "utility", "utilities", "meter", "inspection", "inspector", "hvac", "furnace",
    "air conditioning", "internet", "cable", "install", "installation",
    "appointment", "cleaning", "cleaner", "roofer", "roofing", "painter", "pest control",
]  # fmt: skip


# On Halloween evening only (occasions.py): outside it, "trick or treat" is no reason to
# think a visitor is a trick-or-treater.
TRICK_OR_TREAT_PHRASES = [
    "trick or treat", "trick or treating", "trick or treats", "trick or treaters",
    # How little kids say it, as speech recognition hears it.
    "tricker treat", "trick a treat", "trick r treat", "trick or tree", "trick treat",
    "halloween", "candy", "candies", "treat", "treats", "sweets", "chocolate",
    "lollipop", "boo", "smell my feet", "costume", "costumes",
]  # fmt: skip


def phrase_pattern(phrases: list[str]) -> re.Pattern[str]:
    """Whole-word match of any of ``phrases``; multi-word phrases match across any
    run of spaces or hyphens."""
    alternatives = sorted((r"[\s-]+".join(map(re.escape, p.split())) for p in phrases), key=len)
    return re.compile(r"\b(?:" + "|".join(reversed(alternatives)) + r")\b")


# Checked in this order; the first category with a match wins. The second item
# is the reply key (Settings.replies) or, for the first three, the conversation
# that takes over (see dialogue.py).
CATEGORIES = [
    ("emergency", "emergency", phrase_pattern(EMERGENCY_PHRASES)),
    ("law_enforcement", "law_enforcement", phrase_pattern(LAW_ENFORCEMENT_PHRASES)),
    ("civic", "civic", phrase_pattern(CIVIC_PHRASES)),
    ("solicitor", "sales", phrase_pattern(SOLICITOR_PHRASES)),
    ("likely_delivery", "delivery", phrase_pattern(DELIVERY_PHRASES)),
    ("service_visit", "maintenance", phrase_pattern(SERVICE_PHRASES)),
]
# Classifications handled by a conversation of their own rather than one reply.
CONVERSATIONS = {"emergency", "law_enforcement", "civic"}
TRICK_OR_TREAT = phrase_pattern(TRICK_OR_TREAT_PHRASES)


def classify_response(text: str, halloween: bool = False) -> tuple[str, str]:
    """Return ``(classification, reply_key)`` for a transcript.

    Classifications: ``emergency``, ``law_enforcement``, ``civic``,
    ``likely_delivery``, ``solicitor``, ``service_visit``, ``cooperative_other``
    and ``no_response``; on Halloween evening, ``trick_or_treat`` too (reply key
    "halloween"): it comes right after an emergency, so a kid dressed as a police officer
    is a trick-or-treater, and it's also any answer that isn't one of the others (a "yes!",
    "I'm a dinosaur!"), since the greeting asked "Trick or treat?".
    """
    normalized = text.lower().strip()
    if not normalized:
        return "no_response", "no_answer"

    emergency, *others = CATEGORIES
    if emergency[2].search(normalized):
        return emergency[0], emergency[1]
    if halloween and TRICK_OR_TREAT.search(normalized):
        return "trick_or_treat", "halloween"
    for classification, reply_key, pattern in others:
        if pattern.search(normalized):
            return classification, reply_key

    if halloween:
        # Any other answer to "Trick or treat?" that evening, a "yes!" or an "I'm a
        # dinosaur!", is most likely a trick-or-treater.
        return "trick_or_treat", "halloween"
    if len(re.findall(r"\w+", normalized)) <= 1:
        return "no_response", "no_answer"

    return "cooperative_other", "generic"
