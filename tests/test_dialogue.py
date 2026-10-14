import pytest

from gatekeeper.dialogue import has_warrant, judge_signed, yes_no


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes.", True),
        ("Yeah, we do", True),
        ("No.", False),
        ("Nope", False),
        ("We don't", False),
        ("I'd like to talk to the homeowner", None),
        ("", None),
    ],
)
def test_yes_no(text, expected):
    assert yes_no(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We have a warrant for this address", True),
        ("Yes, we have a search warrant", True),
        ("We don't have a warrant, we just want to talk", False),
        ("No warrant, we have a few questions", False),
        ("We're not here with a warrant", False),
        ("Yes", True),
        ("No", False),
        ("We'd like to ask you about a car in the neighborhood", None),
    ],
)
def test_has_warrant(text, expected):
    assert has_warrant(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes, it's signed by Judge Martinez", True),
        ("It was signed by the court this morning", True),
        ("Yes", True),
        ("It's an administrative warrant", False),
        ("It's not signed by a judge", False),
        ("No", False),
        ("Here it is", None),
    ],
)
def test_judge_signed(text, expected):
    assert judge_signed(text) is expected
