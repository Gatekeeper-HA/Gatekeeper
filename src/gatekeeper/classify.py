"""Keyword classification of the visitor's answer.

Whole words and phrases only (so "ups" doesn't match "groups"). Solicitors
are checked before deliveries, so "I'm selling Amazon gift cards" is a
solicitor. English only: Whisper is run with language="en".
"""

from __future__ import annotations

import re

SOLICITOR_PHRASES = [
    "sales", "salesman", "selling", "sell", "solicit", "soliciting", "solicitation",
    "canvass", "canvassing", "campaign", "campaigning", "petition", "survey",
    "donate", "donation", "donations", "fundraiser", "fundraising", "charity", "raffle",
    "subscription", "magazine", "magazines", "special offer", "free estimate",
    "solar", "candidate", "vote", "voting", "election", "church", "bible", "ministry",
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


def _pattern(phrases: list[str]) -> re.Pattern[str]:
    # Multi-word phrases match across any run of spaces or hyphens.
    alternatives = sorted((r"[\s-]+".join(map(re.escape, p.split())) for p in phrases), key=len)
    return re.compile(r"\b(?:" + "|".join(reversed(alternatives)) + r")\b")


# Checked in this order; the first category with a match wins.
CATEGORIES = [
    ("solicitor", "sales", _pattern(SOLICITOR_PHRASES)),
    ("likely_delivery", "delivery", _pattern(DELIVERY_PHRASES)),
    ("service_visit", "maintenance", _pattern(SERVICE_PHRASES)),
]


def classify_response(text: str) -> tuple[str, str]:
    """Return ``(classification, reply_key)`` for a transcript.

    Classifications: ``likely_delivery``, ``solicitor``, ``service_visit``,
    ``cooperative_other`` and ``no_response``. ``reply_key`` indexes
    ``Settings.replies``.
    """
    normalized = text.lower().strip()
    if not normalized:
        return "no_response", "no_answer"

    for classification, reply_key, pattern in CATEGORIES:
        if pattern.search(normalized):
            return classification, reply_key

    if len(re.findall(r"\w+", normalized)) <= 1:
        return "no_response", "no_answer"

    return "cooperative_other", "generic"
