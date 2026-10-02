"""Separating the visitor's answer from the greeting's echo.

Recording starts when the greeting starts playing, so the clip holds the
greeting (picked up by the doorbell's own mic) followed by the visitor's
answer. The greeting's end is found by matching its known words in the
transcript, which tolerates the variable talkback and RTSP-connect delays.

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


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


def strip_greeting(words: list[Word], greeting: str) -> tuple[list[Word], str]:
    """Return ``(answer_words, method)``; method is "matched" or "unmatched"."""
    spoken = [_norm(w.text) for w in words]
    expected = [n for n in (_norm(w) for w in greeting.split()) if n]

    matcher = difflib.SequenceMatcher(a=spoken, b=expected, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    if blocks:
        matched = sum(b.size for b in blocks)
        last = blocks[-1]
        ends_greeting = last.b + last.size == len(expected) and last.size >= 2
        if ends_greeting or matched >= max(2, len(expected) // 2):
            return words[last.a + last.size :], "matched"

    return words, "unmatched"


def join_words(words: list[Word]) -> str:
    return " ".join(w.text.strip() for w in words).strip()
