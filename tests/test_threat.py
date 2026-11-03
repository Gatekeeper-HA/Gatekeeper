import pytest

from gatekeeper.threat import LATE_NIGHT, Sign, assess, level, word_signs


@pytest.mark.parametrize(
    ("said", "signs"),
    [
        # Ordinary visits: nothing.
        ("I have a package for you", []),
        ("Hey, it's Mike! Is Alex home?", []),  # a friend asking for someone by name
        ("Trick or treat!", []),
        ("Hi, we're with the church down the street", []),
        ("Can you sign for this?", []),
        # Casing.
        ("Is anyone home?", ["asks_occupancy"]),
        ("anybody home? hello?", ["asks_occupancy"]),
        ("Are your parents home?", ["asks_occupancy"]),
        ("When will they be back?", ["asks_schedule"]),
        ("What time does the owner usually get home?", ["asks_schedule"]),
        ("Are they on vacation or something?", ["asks_schedule"]),
        # Getting in.
        ("Can I come inside real quick?", ["asks_entry"]),
        ("Just open the door, it's heavy", ["asks_entry"]),
        ("Could I use your bathroom?", ["asks_entry"]),
        ("What's the gate code?", ["asks_code"]),
        ("I need you to turn off the alarm", ["asks_code"]),
        # Scams.
        ("This is your final notice, we're shutting off your power today", ["scam_markers"]),
        ("You can pay in gift cards", ["scam_markers"]),
        ("We're doing a free roof inspection in the area", ["scam_markers"]),
        # Hostility and threats.
        ("Open the damn door!", ["hostile"]),
        ("I know you're in there", ["hostile"]),
        ("Let me in right now", ["asks_entry", "hostile"]),
        ("this is fucking ridiculous", ["swearing"]),
        ("I'm gonna kick in the door", ["threat"]),
        ("Open up or I'll break the window", ["asks_entry", "threat"]),
        ("I've got a knife", ["threat"]),
    ],
)
def test_the_words(said, signs):
    assert [s.name for s in word_signs(said)] == signs


def sign(weight):
    return Sign(f"a {weight} sign", weight, "")


@pytest.mark.parametrize(
    ("weights", "expected"),
    [
        ([], 0),
        (["weak"], 0),  # late at night alone is nothing
        (["moderate"], 1),
        (["moderate", "weak"], 1),
        (["moderate", "moderate"], 2),
        (["strong"], 2),
        (["strong", "moderate"], 2),
        (["strong", "moderate", "moderate"], 3),
        (["strong", "strong"], 3),
        (["urgent"], 3),
    ],
)
def test_the_levels(weights, expected):
    assert level([sign(w) for w in weights]) == expected


def test_late_at_night_raises_the_other_signs():
    said = ["Is anyone home?", "Can I come in?"]
    assert assess(said, hour=14).level == 2
    late = assess(said, hour=2)
    assert late.level == 2 and late.signs[-1] == LATE_NIGHT
    assert assess(["Is anyone home?"], hour=23).level == 1
    assert assess(["I have a package"], hour=2).signs == ()  # nothing to raise


def test_the_record_gives_the_reasons():
    found = assess(["Is anyone home? Can I come in?"], hour=12)
    assert found.record(acted=True) == {
        "level": 2,
        "signs": ["asks_occupancy", "asks_entry"],
        "reasons": ["asked whether anyone's home", "asked to come in"],
        "acted": True,
    }


def logged(event_id, outcome="completed", trigger="person", transcript=""):
    return {"event_id": event_id, "outcome": outcome, "trigger": trigger, "transcript": transcript}


def test_a_package_taken_while_only_visitors_came():
    from gatekeeper.threat import package_taken

    visitor = logged("1800000000.0-abc", transcript="UPS, return pickup")
    taken = package_taken([visitor])
    assert taken["level"] == 2 and taken["signs"] == ["package_taken"]
    assert taken["said"] == "UPS, return pickup" and taken["visitor"] == "1800000000.0-abc"
    # The household was there (went in, recognized, came out of the door, came back): no.
    for outcome in ("entered", "household", "left", "stepped_out", "returned"):
        assert package_taken([visitor, logged("1800000010.0-x", outcome=outcome)]) is None
    # Nobody greeted (only package records, or nothing): no.
    assert package_taken([logged("package-1.0", "package_delivered", "package")]) is None
    assert package_taken([]) is None
    # A press or a knock is a visitor too.
    assert package_taken([logged("press-1800000000.0", trigger="button")])["said"] == ""
