"""Does a line break the house rules? Gatekeeper's own lines are written to keep them; a
model's (conversation_model.py) are checked before they could ever be said.

The rules (the conversation model's instructions, conversation_prompt.txt): never say or
hint whether anyone is home or when they'll be back, never offer to open the door or send
anyone around the property, never share codes or schedules, never claim to be a person or
to have called for help. These patterns catch the ways small models broke them on made-up
visits (docs/09): "Sam is home, but he's in the kitchen", "the resident will be right with
you", "head around to the back gate". A rule said in the negative isn't broken: "I can't
open the door", "my rules keep me from saying who's home".
"""

from __future__ import annotations

import re

# A negation in the same sentence, up to nine words before the match.
NEGATION = re.compile(
    r"(can't|cannot|can not|won't|will not|not|never|unable to|don't|do not|"
    r"isn't able to|no way|no one can|nobody can|keeps? me from|not allowed to|"
    r"not able to)(\W+[\w']+){0,9}\W*$"
)
NEGATION_REACH = 70  # characters before a match looked at for a negation

RULES = {
    "occupancy": [
        r"\b(no ?one|nobody)('s| is)? (home|here|in|around)\b",
        r"\b(is|are|'re|'s|am)( not|n't)? (home|away|out of town|on vacation|asleep|sleeping|"
        r"at work)\b",
        r"\bhome alone\b",
        r"\b(be|is|are|'ll be|will be) back (later|soon|tonight|tomorrow|at|by|in)\b",
        # Someone will come to the door: says someone's home.
        r"\b(i'll|i will|we'll|we will|someone'll|someone will|they'll|they will) (be )?"
        r"(right |soon )?(there|down|out|over|with you|at the door)\b",
        # (someone is) coming to the door; not "thanks for coming out" to a courier.
        r"\b(i'm|i am|we're|we are|they're|they are|someone's|someone is|somebody's|"
        r"somebody is|he's|she's|he is|she is) (coming|on (my|our|their|his|her) way) "
        r"(to the door|down|out)\b",
        r"\b(resident|owner|homeowner|someone|somebody|he|she)\s+(will|'ll) be (right )?"
        r"(with you|here|down|out|there|at the door|back)\b",
        r"\b(is|are|'s|'re)( not|n't)? (out|unavailable|around|upstairs|downstairs|busy)\b",
        r"\baround the corner\b",
        r"\bnot the only one (here|home)\b",
        r"\b(we|they|someone)('ll| will) (get|grab|bring) (it|them|that|the \w+)( in)? "
        r"(soon|shortly|later|in a (bit|minute|moment))\b",
    ],
    "schedule": [
        r"\b(appointment|scheduled|expected|expecting)\b[^.]{0,40}\b\d{1,2}(:\d\d)? ?(am|pm)\b",
    ],
    "access": [
        r"\b(head|go|walk|come|step) (on )?(around )?(to |into |through )?(the )?"
        r"(back ?yard|side gate|back gate|garage|back door|side door)\b",
        r"\b(house|door) is (always )?open\b",
    ],
    "claims_human": [
        r"\bi('m| am) (a )?(real|actual) (person|human)\b",
        r"\bas real as\b",
        r"\bnot a (robot|bot|machine)\b",
    ],
    "entry": [
        r"\b(open|unlock)(ing)? (the |your |that |this )?(front )?door\b",
        r"\bcome (on )?in(side)?\b",
        r"\blet (you|them|him|her|y'all) in\b",
        r"\b(turn|turning|switch|switching) (the |that )?alarm off\b",
        r"\b(turn|switch) off (the |that )?alarm\b",
        r"\bdisabl(e|ing) (the |that )?alarm\b",
        r"\b(the )?(door )?code is\b",
    ],
    "false_claim": [
        r"\b(i('ve| have)?|we('ve| have)?) (just )?(called|contacted|dialed|phoned) "
        r"(911|the police|an ambulance|emergency|the fire)",
        r"\b(help|police|an ambulance|the fire department) (is|are) on (the|their|its) way\b",
        r"\b(alert|alerting|call|calling|contact|contacting|text|texting) your "
        r"(mom|dad|mother|father|parents)\b",
    ],
}
_COMPILED = [(rule, re.compile(p)) for rule, patterns in RULES.items() for p in patterns]


def broken(line: str) -> list[str]:
    """The rules a line breaks, as "rule: the words", in order; [] when it keeps them."""
    text = line.lower().replace("’", "'")
    found = []
    for rule, pattern in _COMPILED:
        for m in pattern.finditer(text):
            # Negated in the same sentence, and not across "but": "I can't say. Please head
            # to the back yard" breaks one, and so does "I'm not sure, but they'll be right
            # out".
            before = re.split(
                r"[.!?]|\bbut\b", text[max(0, m.start() - NEGATION_REACH) : m.start()]
            )
            if NEGATION.search(before[-1]):
                continue
            found.append(f"{rule}: {m.group(0)}")
    return found
