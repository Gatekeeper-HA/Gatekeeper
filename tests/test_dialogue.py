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


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("I'm a police officer looking for Joe Schmo.", True),
        ("Police, we're investigating a break-in", True),
        ("Sheriff's office, we have a warrant", True),
        ("Officer Kim, we'd like to ask you a few questions", True),
        ("We're here about a noise complaint", True),
        ("Police", False),
        ("Police department", False),
        ("Sheriff's office, open the door", False),
        ("I'm Officer Dana Smith with the Springfield police", False),
    ],
)
def test_gave_reason(text, reason):
    from gatekeeper.dialogue import gave_reason

    assert gave_reason(text) is reason


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("My name is Sam. I'm a volunteer with Pat Lee and the Democrats.", True),
        ("I'm Pat with the League of Women Voters", True),
        ("Hi, my name's Alex, I'm running for city council", True),
        ("This is Jordan from the Smith campaign", True),
        ("My name is Sam", False),  # no group: still asked who they're with
        ("I'm with the Smith campaign", False),  # no name
        ("Hi, I'm Chris Doe, I'm running for state senate", True),
        ("I'm a volunteer with the Smith campaign", False),  # no name
        ("Just asking people to register to vote", False),
    ],
)
def test_introduced(text, said):
    from gatekeeper.dialogue import introduced

    assert introduced(text) is said
