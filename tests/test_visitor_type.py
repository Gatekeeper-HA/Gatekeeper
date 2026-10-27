import time

import numpy as np

from gatekeeper.visitor_type import (
    DeliveryGuesser,
    Guesses,
    closest_moments,
    decide,
    moves,
)

# A courier: up the walk to the door, then back down it.
UP_AND_BACK = [
    ((0.53, 0.6), 100.0),
    ((0.55, 0.75), 101.0),
    ((0.56, 0.9), 102.0),
    ((0.56, 0.92), 102.3),
    ((0.52, 0.7), 104.0),
]
# The household: up the walk and in at the door.
UP_AND_IN = [((0.45, 0.6), 100.0), ((0.7, 0.85), 102.0), ((0.92, 0.96), 104.0)]


def test_the_closest_moments_are_the_lowest_in_the_frame_half_a_second_apart():
    assert closest_moments(UP_AND_BACK) == [
        ((0.56, 0.92), 102.3),
        ((0.55, 0.75), 101.0),
        ((0.52, 0.7), 104.0),
    ]  # 102.0 is within 0.5 s of 102.3


def test_moves():
    assert moves(UP_AND_BACK) == {
        "start": [0.53, 0.6],
        "end": [0.52, 0.7],
        "lowest": 0.92,
        "left_the_way_they_came": True,
    }
    assert not moves(UP_AND_IN)["left_the_way_they_came"]
    assert moves([]) == {}


def test_the_rule():
    assert decide(None, {"package_left": True}) == "delivery"
    assert decide(None, {}) == "other"  # no good picture, and nothing else
    assert decide(0.35, {}) == "delivery"  # looks like one
    assert decide(0.15, {"left_the_way_they_came": True}) == "delivery"
    assert decide(0.15, {"walked_away": True}) == "delivery"
    assert decide(0.15, {}) == "other"
    assert decide(0.05, {"left_the_way_they_came": True}) == "other"


class FakeLooks:
    made = 0

    def __init__(self):
        FakeLooks.made += 1

    def score(self, crops):
        return 0.2


def guesser(tmp_path, path=UP_AND_BACK, package=False):
    g = DeliveryGuesser(
        Guesses(tmp_path / "visit-guesses.json"),
        path=lambda event_id: path,
        frame=lambda at: None,
        detect=None,
        looks=FakeLooks,
        package_left=lambda event_id: package,
        delay=0,
        clock=lambda: 1800000000.0,
        sleep=lambda s: None,
    )
    g.crops = lambda path: [np.zeros((10, 5, 3), np.uint8)] if path else []
    return g


def test_a_courier_is_guessed_and_kept(tmp_path):
    FakeLooks.made = 0
    g = guesser(tmp_path)
    record = {"event_id": "1799999600.000000-aaa111", "trigger": "person", "details": {}}
    guess = g.guess(record)
    assert guess["guess"] == "delivery" and guess["looks"] == 0.2 and guess["crops"] == 1
    assert guess["features"]["left_the_way_they_came"] and not guess["features"]["answered"]
    assert g.guesses.all()["1799999600.000000-aaa111"] == guess
    g.guess({"event_id": "1799999700.000000-bbb222", "trigger": "person"})
    assert FakeLooks.made == 1  # loaded once


def test_the_household_going_in_is_not(tmp_path):
    g = guesser(tmp_path, path=UP_AND_IN)
    assert g.guess({"event_id": "e", "trigger": "person"})["guess"] == "other"
    # No path at all (Frigate dropped it): no picture, nothing to go on, unless a package.
    assert guesser(tmp_path, path=[]).guess({"event_id": "f"})["looks"] is None
    assert guesser(tmp_path, path=[], package=True).guess({"event_id": "g"})["guess"] == (
        "delivery"
    )


def test_visits_are_guessed_in_the_background_but_not_presses_or_packages(tmp_path):
    g = guesser(tmp_path)
    g.visit_logged({"event_id": "press-1799999800.000", "trigger": "button"})
    g.visit_logged({"event_id": "package-1799999660.000", "trigger": "package"})
    g.visit_logged({"event_id": "1799999600.000000-aaa111", "trigger": "person"})
    for _ in range(100):
        if g.guesses.all():
            break
        time.sleep(0.02)
    assert list(g.guesses.all()) == ["1799999600.000000-aaa111"]
