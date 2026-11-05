"""Does a line break the house rules? Gatekeeper's own lines are written to keep them; a
model's (conversation_model.py) are checked before they could ever be said.

The rules (the conversation model's instructions, conversation_prompt.txt): never say or
hint whether anyone is home or when they'll be back, never offer to open the door or send
anyone around the property, never share codes, schedules or the household's names, never
claim to be a person or to have called for help. These patterns catch the ways models broke
them on made-up visits (docs/09): "Sam is home, but he's in the kitchen", "the resident will
be right with you", "head around to the back gate". And in made-up conversations, where
kindness gave the house away: "I'll let Delphine know you stopped by", "they're on their
way to help you", "please wait while they check". A rule said in the negative isn't
broken: "I can't open the door", "my rules keep me from saying who's home".
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
# Where a sentence ends (and "but", or a new clause after ", and"), for the negation: not
# at "Mr." in "I can't say when Mr. Lee is around". "I can't say who's around, and no
# one's opening the door" isn't negated.
SENTENCE_END = re.compile(
    r"(?<!\bmr)(?<!\bmrs)(?<!\bms)(?<!\bdr)(?<!\bst)[.!?;]"  # not "Mr." and the like
    r"|\bbut\b|,\s*and\b"
)

RULES = {
    "occupancy": [
        r"\b(no ?one|nobody|no human|no person|no people)('s| is)? (home|here|in|around)\b",
        # Nobody's coming to the door: says somebody could (not "nobody's coming in": the
        # visitor isn't).
        r"\b(no ?one|nobody)('s| is| will| will be|'ll)? ((coming|come) (to the door|out|"
        r"down|back)|answering|answer|opening|open)\b",
        # People in the house: "I've alerted the grown-ups here".
        r"\b(grown-?ups|adults|parents|family|folks|people|everyone|someone|somebody) "
        r"(are |is )?(here|inside|in the house|at home)\b",
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
        # On their way, to anywhere: "they're on their way to help you find your mom".
        r"\b(they're|they are|someone's|someone is|somebody's|somebody is|he's|she's|he is|"
        r"she is|the resident is|the owner is) on (their|his|her|the) way\b",
        # Wait for someone: "please wait a moment while they check".
        r"\b(wait|hang on|hold on|stay)\b[^.!?]{0,40}\bwhile (they|the resident|the owner|"
        r"the homeowner|someone|somebody|(i|we) (let|tell|get|check with))\b",
        # Not the visitor's whereabouts: "I'm letting the resident know you're out there".
        r"\b(?<!you )(?<!you)(is|are|'s|'re)( not|n't)? "
        r"(out|unavailable|around|upstairs|downstairs|busy)\b",
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
        # Called now (rule 11's "the police will be called if you don't leave" is fine).
        r"\b(police|cops|911|an ambulance|the fire department|help) (are|is|have|has)"
        r"( been| being| already)* (called|contacted|notified|alerted|dispatched|coming)\b",
        r"\b(i'm|i am|we're|we are) (now )?(calling|contacting|dialing|phoning) "
        r"(911|the police|the cops|an ambulance)\b",
        r"\b(alert|alerted|alerting|call|called|calling|contact|contacted|contacting|text|"
        r"texted|texting|notify|notified|notifying) your "
        r"(mom|mommy|dad|daddy|mother|father|parents|family|grown-?ups)\b",
    ],
}
_COMPILED = [(rule, re.compile(p)) for rule, patterns in RULES.items() for p in patterns]
# Gatekeeper passing a message to a name says who lives here (rule 2): "I'll let Delphine
# know you stopped by", "avisaré a Marta". Not "you could text Jordan": that's the
# visitor's to do. By the name's capital, so before the line is lowercased.
NAMES = re.compile(
    r"\b(?:(?:I'll|I will|I can|I'd|I'm going to|I'm letting|I've let|We'll|we'll) "
    r"(?:let|tell|remind|text|message|pass (?:it|that|this|your \w+) (?:on )?to)"
    r"|(?:avisaré|le avisaré|le diré|les avisaré)(?: a)?) ([A-Z][a-z]+)\b"
)
NOT_NAMES = {"Me", "Us", "Them", "Him", "Her", "The", "Everyone", "Someone", "Anyone", "I"}


def broken(line: str) -> list[str]:
    """The rules a line breaks, as "rule: the words", in order; [] when it keeps them."""
    line = line.replace("\u2019", "'")
    text = line.lower()

    def negated(start: int) -> bool:
        # Negated in the same sentence, and not across "but": "I can't say. Please head to
        # the back yard" breaks one, and so does "I'm not sure, but they'll be right out".
        before = SENTENCE_END.split(text[max(0, start - NEGATION_REACH) : start])
        return bool(NEGATION.search(before[-1]))

    found = []
    for rule, pattern in _COMPILED:
        for m in pattern.finditer(text):
            if not negated(m.start()):
                found.append(f"{rule}: {m.group(0)}")
    for m in NAMES.finditer(line):
        if m.group(1) not in NOT_NAMES and not negated(m.start()):
            found.append(f"names: {m.group(0).lower()}")
    return found
