import pytest

from gatekeeper.classify import classify_response

DELIVERY = ("likely_delivery", "delivery")
SOLICITOR = ("solicitor", "sales")
SERVICE = ("service_visit", "maintenance")
OTHER = ("cooperative_other", "generic")
NONE = ("no_response", "no_answer")
EMERGENCY = ("emergency", "emergency")
POLICE = ("law_enforcement", "law_enforcement")
CIVIC = ("civic", "civic")

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
    # Emergencies come first, even from an officer
    ("We got a 911 call from this address", EMERGENCY),
    ("Police, we're doing a welfare check", EMERGENCY),
    ("Your garage is on fire!", EMERGENCY),
    ("Fire department, is anyone injured?", EMERGENCY),
    ("There's a gas leak on the street", EMERGENCY),
    # Law enforcement
    ("Police, we need to talk to you", POLICE),
    ("Sheriff's office, open the door", POLICE),
    ("I'm Deputy Johnson with the county", POLICE),
    ("Officer Smith, Minneapolis Police Department", POLICE),
    ("We have a warrant", POLICE),
    ("Special agent with the FBI", POLICE),
    ("We're with homeland security", POLICE),
    ("I'm a real estate agent selling homes nearby", SOLICITOR),  # not "agent"
    ("Do you want some ice cream?", OTHER),  # not "ice"
    # Civic: candidates, campaigns, canvassers
    ("Hi, I'm Jim Abeler, your senator, just came by to say hello and leave a note", CIVIC),
    ("Just asking people to register to vote, election day is November 3rd", CIVIC),
    ("I'm canvassing for the campaign", CIVIC),
    ("Would you sign our petition?", CIVIC),
    ("I'm running for city council", CIVIC),
    ("I'm a volunteer with the DFL", CIVIC),
    ("I'm selling election signs", CIVIC),
    # Solicitors
    ("we're selling solar panels", SOLICITOR),
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
