import pytest

from gatekeeper.listen import Word, join_words, strip_greeting

GREETING = "Hello. This property is monitored. Please state the purpose of your visit."


def words(text: str, start: float = 0.0, step: float = 0.3) -> list[Word]:
    return [
        Word(f" {w}", start + i * step, start + i * step + step * 0.8)
        for i, w in enumerate(text.split())
    ]


@pytest.mark.parametrize(
    ("heard", "answer"),
    [
        # The 2026-10-01 walk-up, as Whisper heard it in Frigate's recording.
        (
            "Hello, this property is monitored. Please state the purpose of your visit. "
            "I have a package.",
            "I have a package.",
        ),
        # Whisper garbles some greeting words.
        (
            "Hello this property is monitor please stay the purpose of your visit "
            "I have a package",
            "I have a package",
        ),
        # Recording started late, after the greeting began.
        ("purpose of your visit. Amazon delivery.", "Amazon delivery."),
        # The visitor talks over the last words of the greeting.
        (
            "Hello, this property is monitored. Please state the purpose I'm with FedEx.",
            "I'm with FedEx.",
        ),
        # The answer repeats greeting words.
        (
            "Hello, this property is monitored. Please state the purpose of your visit. "
            "I'm here to visit Alex, please.",
            "I'm here to visit Alex, please.",
        ),
        # The visitor said nothing.
        (
            "Hello, this property is monitored. Please state the purpose of your visit.",
            "",
        ),
    ],
)
def test_greeting_is_matched_and_removed(heard, answer):
    kept, method = strip_greeting(words(heard), GREETING)
    assert method == "matched"
    assert join_words(kept) == answer


def test_unrecognized_greeting_keeps_everything():
    # Recording started after the greeting ended: all of it is the answer.
    heard = words("I have a package")
    kept, method = strip_greeting(heard, GREETING)
    assert method == "unmatched"
    assert join_words(kept) == "I have a package"


def test_nothing_heard():
    assert strip_greeting([], GREETING) == ([], "unmatched")


def test_single_stray_greeting_word_is_not_a_match():
    # One matching word ("please") isn't enough to locate the greeting.
    heard = words("please leave it by the door")
    kept, method = strip_greeting(heard, GREETING)
    assert method == "unmatched"
    assert join_words(kept) == "please leave it by the door"
