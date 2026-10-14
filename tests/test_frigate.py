import json

import pytest

from conftest import load_fixture
from gatekeeper.frigate import FrigateEvent, Zone, inside, parse_event, zone_polygon


def test_parses_new_person_event():
    event = parse_event(load_fixture("frigate/person_new.json"))
    assert event == FrigateEvent(
        type="new",
        id="1727712000.123456-abc123",
        camera="front_door",
        label="person",
        entered_zones=(),
    )


def test_update_uses_after_snapshot():
    event = parse_event(load_fixture("frigate/person_update.json"))
    assert event.type == "update"
    assert event.entered_zones == ("porch",)
    assert event.current_zones == ("porch",)  # where they are now, for presence


def test_end_event():
    event = parse_event(load_fixture("frigate/person_end.json"))
    assert event.type == "end"
    assert event.id == "1727712000.123456-abc123"


def test_falls_back_to_before_when_after_missing():
    raw = json.dumps({"type": "end", "before": {"id": "x1", "camera": "c", "label": "person"}})
    assert parse_event(raw.encode()).id == "x1"


def test_rejects_unusable_payloads():
    for raw in [
        b"not json",
        b"[1, 2, 3]",
        b"{}",
        json.dumps({"type": "new", "after": {"camera": "front_door"}}).encode(),
        json.dumps({"type": "new", "after": "oops"}).encode(),
    ]:
        assert parse_event(raw) is None, raw


def test_path_is_where_the_person_has_been():
    raw = json.dumps(
        {
            "type": "update",
            "after": {
                "id": "E",
                "camera": "front_door",
                "label": "person",
                "path_data": [[[0.85, 0.99], 1.0], [[0.7, 0.9], 1.5], ["junk"], [[0.6, 0.86], 2.0]],
            },
        }
    ).encode()
    assert parse_event(raw).path == ((0.85, 0.99), (0.7, 0.9), (0.6, 0.86))


@pytest.mark.parametrize(
    "zone",
    [
        {"coordinates": "0.7,0.85,1,0.85,1,1,0.7,1"},  # fractions, as a string
        {"coordinates": ["0.7,0.85", "1,0.85", "1,1", "0.7,1"]},  # as a list
        {"coordinates": "448,408,640,408,640,480,448,480"},  # pixels of the detect size
    ],
)
def test_zone_polygon_in_fractions_of_the_frame(zone):
    config = {
        "cameras": {
            "front_door": {"detect": {"width": 640, "height": 480}, "zones": {"door": zone}}
        }
    }
    polygon = zone_polygon(config, "front_door", "door")
    assert polygon == pytest.approx([(0.7, 0.85), (1.0, 0.85), (1.0, 1.0), (0.7, 1.0)])


def test_missing_or_broken_zones_are_none():
    config = {"cameras": {"front_door": {"zones": {"bad": {"coordinates": "0.1,0.2,0.3"}}}}}
    assert zone_polygon(config, "front_door", "door") is None
    assert zone_polygon(config, "front_door", "bad") is None
    assert zone_polygon(config, "back_door", "door") is None
    assert zone_polygon({}, "front_door", "door") is None


def test_inside_counts_points_on_the_frame_edge():
    door = [(0.7, 0.85), (1.0, 0.85), (1.0, 1.0), (0.7, 1.0)]
    assert inside(door, (0.85, 0.95))
    assert inside(door, (0.85, 1.0))  # feet out of view: on the bottom edge
    assert inside(door, (1.0, 0.9))
    assert not inside(door, (0.62, 0.99))  # a visitor at the camera, left of the door
    assert not inside(door, (0.85, 0.6))


def test_zone_loads_in_the_background_and_retries():
    config = {
        "cameras": {"front_door": {"zones": {"door": {"coordinates": "0.7,0.85,1,0.85,1,1,0.7,1"}}}}
    }
    answers = [OSError("Frigate not up yet"), {"cameras": {}}, config]

    def load():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    zone = Zone("door", "front_door", load)
    assert zone.contains((0.9, 0.95)) is None  # unknown until loaded
    waits = []
    zone.load_until_known(retry_seconds=60, sleep=waits.append)
    assert waits == [60, 60]
    assert zone.contains((0.9, 0.95)) is True
    assert zone.contains((0.5, 0.95)) is False


def test_zone_loading_stays_quiet_while_frigate_restarts(caplog):
    # A deploy restarts Frigate together with Gatekeeper: not reaching it for a
    # while is normal, not a warning for the daily report.
    config = {
        "cameras": {"front_door": {"zones": {"door": {"coordinates": "0.7,0.85,1,0.85,1,1,0.7,1"}}}}
    }
    now = [0.0]
    answers = [OSError("refused")] * 3 + [config]

    def load():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def sleep(seconds):
        now[0] += seconds

    caplog.set_level("INFO", logger="gatekeeper.frigate")
    Zone("door", "front_door", load).load_until_known(sleep=sleep, clock=lambda: now[0])
    assert [r.levelname for r in caplog.records if "couldn't read" in r.getMessage()] == [
        "INFO",
        "INFO",
        "INFO",
    ]

    # Still unreachable after 5 minutes: that's worth a warning.
    caplog.clear()
    now[0], answers[:] = 0.0, [OSError("refused")] * 40 + [config]
    Zone("door", "front_door", load).load_until_known(sleep=sleep, clock=lambda: now[0])
    levels = [r.levelname for r in caplog.records if "couldn't read" in r.getMessage()]
    assert levels[:30] == ["INFO"] * 30 and set(levels[30:]) == {"WARNING"}
