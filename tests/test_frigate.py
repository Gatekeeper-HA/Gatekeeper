import json

from conftest import load_fixture
from gatekeeper.frigate import FrigateEvent, parse_event


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
