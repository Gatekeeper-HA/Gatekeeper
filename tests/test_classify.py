import pytest

from gatekeeper.classify import classify_response

DELIVERY = ("likely_delivery", "delivery")
SOLICITOR = ("solicitor", "sales")
SERVICE = ("service_visit", "maintenance")
OTHER = ("cooperative_other", "generic")
NONE = ("no_response", "no_answer")

CASES = [
    # No answer
    ("", NONE),
    ("   ", NONE),
    ("hello", NONE),
    ("Hi.", NONE),
    ("...", NONE),
    # Deliveries
    ("I have a package.", DELIVERY),
    ("I have a package for you", DELIVERY),
    ("Amazon delivery", DELIVERY),
    ("Amazon.", DELIVERY),
    ("FedEx", DELIVERY),
    ("Fed Ex, I need a signature", DELIVERY),
    ("UPS, dropping off a box", DELIVERY),
    ("It's the mailman with a parcel", DELIVERY),
    ("DoorDash order for Sam", DELIVERY),
    ("Uber Eats", DELIVERY),
    ("uber-eats for Jordan", DELIVERY),
    ("I've got your groceries from Instacart", DELIVERY),
    ("pizza delivery", DELIVERY),
    ("Someone needs to sign for this", DELIVERY),
    ("Delivered it to the porch", DELIVERY),
    # Solicitors
    ("I'm canvassing for the campaign", SOLICITOR),
    ("we're selling solar panels", SOLICITOR),
    ("Would you sign our petition?", SOLICITOR),
    ("I'm collecting donations for charity", SOLICITOR),
    ("Can I talk to you about our church?", SOLICITOR),
    ("We're doing a quick survey of the neighborhood", SOLICITOR),
    ("I'm selling Amazon gift cards", SOLICITOR),
    ("We're offering a free estimate on your roof", SOLICITOR),
    # Service visits
    ("I'm here to repair the furnace", SERVICE),
    ("the plumber is here", SERVICE),
    ("I'm the electrician", SERVICE),
    ("Lawn service", SERVICE),
    ("I'm from the gas company to read the meter", SERVICE),
    ("I have an appointment to install your internet", SERVICE),
    ("Tech support, I'm the cable tech", SERVICE),
    ("HVAC maintenance", SERVICE),
    # Cooperative, nothing recognized
    ("I'm a friend of Alex, is she home", OTHER),
    ("I'm your neighbor from across the street", OTHER),
    ("we are with a youth groups program", OTHER),
    ("would you like some cups", OTHER),
    ("technically I'm just visiting", OTHER),
    ("Is this the right house for the party?", OTHER),
    ("your dog got out", OTHER),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_classify_response(text, expected):
    assert classify_response(text) == expected


def test_at_least_40_labeled_phrases():
    assert len(CASES) >= 40
