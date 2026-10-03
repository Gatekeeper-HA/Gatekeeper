"""Separating the visitor's answer from the greeting's echo.

Recording starts when the greeting starts playing, so the clip holds the
greeting (picked up by the doorbell's own mic) followed by the visitor's
answer. The greeting is found by matching its known words in the transcript,
which tolerates the variable talkback and RTSP-connect delays. It counts as
found if 3+ consecutive greeting words were heard, or its last 2 words, so a
greeting whose start or end was cut off is still recognized. If its last words
were misheard ("...state the perfect living room"), the greeting's end is
estimated from the speaking rate of the words that were recognized, and
anything heard before then is dropped as greeting too.

If the greeting can't be found (e.g. recording started after it ended),
every word is kept: a stray greeting word only turns "no answer" into a
"please wait" reply, while cutting by guessed timing can drop a real answer.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # seconds from the start of the clip
    end: float


# Words starting this close before the estimated end of the greeting still
# count as the visitor's (the estimate is approximate).
END_SLACK_SECONDS = 0.3


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


def strip_greeting(words: list[Word], greeting: str) -> tuple[list[Word], str]:
    """Return ``(answer_words, method)``; method is "matched" or "unmatched"."""
    spoken = [_norm(w.text) for w in words]
    expected = [n for n in (_norm(w) for w in greeting.split()) if n]

    matcher = difflib.SequenceMatcher(a=spoken, b=expected, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    strong = [
        b for b in blocks if b.size >= 3 or (b.size >= 2 and b.b + b.size == len(expected))
    ]
    if strong or sum(b.size for b in blocks) >= max(2, len(expected) // 2):
        # Cut after the last run of 2+ greeting words: a lone matching word
        # further on is more likely the visitor's ("please") than the greeting's.
        anchor = ([b for b in blocks if b.size >= 2] or blocks)[-1]
        rest = words[anchor.a + anchor.size :]
        unheard = len(expected) - (anchor.b + anchor.size)  # greeting words after the anchor
        if unheard and rest:
            first, last = blocks[0], anchor
            span_words = last.b + last.size - first.b
            span_time = words[last.a + last.size - 1].end - words[first.a].start
            if span_words > 0 and span_time > 0:
                greeting_end = words[last.a + last.size - 1].end + unheard * span_time / span_words
                rest = [w for w in rest if w.start >= greeting_end - END_SLACK_SECONDS]
        return rest, "matched"

    return words, "unmatched"


def join_words(words: list[Word]) -> str:
    return " ".join(w.text.strip() for w in words).strip()
