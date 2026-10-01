import pytest

from gatekeeper.classify import classify_response

# Characterization of v0.1 behavior. Rows marked with a backlog id are known
# bugs that the named item will deliberately change.
CASES = [
    # (transcript, classification, reply_key)
    ("", "unknown_uncooperative", "no_answer"),
    ("   ", "unknown_uncooperative", "no_answer"),
    ("hello", "unknown_uncooperative", "no_answer"),
    ("I have a package for you", "likely_delivery", "delivery"),
    ("Amazon delivery", "likely_delivery", "delivery"),
    ("FedEx", "likely_delivery", "delivery"),
    ("DoorDash order for Sam", "likely_delivery", "delivery"),
    ("Uber Eats", "likely_delivery", "delivery"),
    ("I'm canvassing for the campaign", "unknown_cooperative", "sales"),
    ("we're selling solar panels", "unknown_cooperative", "sales"),
    ("I'm here to repair the furnace", "unknown_cooperative", "maintenance"),
    ("the plumber is here", "unknown_cooperative", "maintenance"),
    ("I'm a friend of Alex, is she home", "unknown_cooperative", "generic"),
    # P0-14 (H1): substring matching
    ("we are with a youth groups program", "likely_delivery", "delivery"),
    ("would you like some cups", "likely_delivery", "delivery"),
    ("technically I'm just visiting", "unknown_cooperative", "maintenance"),
    ("I'm selling Amazon gift cards", "likely_delivery", "delivery"),
]


@pytest.mark.parametrize(("text", "classification", "reply_key"), CASES)
def test_classify_response(text, classification, reply_key):
    assert classify_response(text) == (classification, reply_key)
