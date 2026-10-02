import pytest

from conftest import load_fixture
from gatekeeper.frigate import FrigateEvent, parse_event
from gatekeeper.sessions import SessionTracker

EID = "1727712000.123456-abc123"


class Harness:
    """A tracker whose visit workers are queued instead of run on threads."""

    def __init__(self, clock, dwell=1.0, ttl=120.0, cooldown=0.0):
        self.started: list[str] = []
        self.workers = []
        self.tracker = SessionTracker(
            camera="front_door",
            dwell_seconds=dwell,
            ttl_seconds=ttl,
            cooldown_seconds=cooldown,
            run_visit=lambda event_id, trigger: self.started.append(event_id),
            spawn=self.workers.append,
            clock=clock,
        )

    def run_workers(self):
        while self.workers:
            self.workers.pop(0)()

    def send(self, fixture):
        self.tracker.handle_event(parse_event(load_fixture(f"frigate/{fixture}.json")))


@pytest.fixture
def h(clock):
    return Harness(clock)


def test_waits_for_dwell_then_starts_once(h, clock):
    h.send("person_new")
    assert h.workers == []

    clock.advance(0.5)
    h.send("person_update")
    assert h.workers == []

    clock.advance(0.6)
    h.tracker.sweep()
    assert len(h.workers) == 1
    h.run_workers()
    assert h.started == [EID]

    # Later updates for the same event never re-trigger.
    clock.advance(5)
    h.send("person_update")
    h.tracker.sweep()
    assert h.workers == []
    assert h.tracker.sessions[EID].handled


def test_update_after_dwell_starts_without_sweep(h, clock):
    h.send("person_new")
    clock.advance(1.0)
    h.send("person_update")
    assert len(h.workers) == 1


def test_no_second_start_while_in_progress(h, clock):
    h.send("person_new")
    clock.advance(2)
    h.tracker.sweep()
    h.tracker.sweep()
    h.send("person_update")
    assert len(h.workers) == 1
    assert h.tracker.sessions[EID].in_progress


def test_session_marked_handled_even_if_visit_raises(clock):
    def boom(_eid, _trigger):
        raise RuntimeError("visit failed")

    workers = []
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120,
        run_visit=boom, spawn=workers.append, clock=clock,
    )  # fmt: skip
    tracker.handle_event(FrigateEvent("new", EID, "front_door", "person"))
    with pytest.raises(RuntimeError):
        workers.pop()()
    assert tracker.sessions[EID].handled
    assert not tracker.sessions[EID].in_progress


def test_ignores_other_cameras_and_labels(h, clock):
    h.send("car_new")
    h.send("other_camera_new")
    clock.advance(10)
    h.tracker.sweep()
    assert h.tracker.sessions == {}
    assert h.workers == []


def test_end_event_drops_session(h, clock):
    h.send("person_new")
    h.send("person_end")
    clock.advance(10)
    h.tracker.sweep()
    assert h.tracker.sessions == {}
    assert h.workers == []


def test_sweep_expires_stale_sessions(clock):
    h = Harness(clock, dwell=1000, ttl=120)
    h.send("person_new")
    clock.advance(119)
    h.tracker.sweep()
    assert EID in h.tracker.sessions
    clock.advance(2)
    h.tracker.sweep()
    assert h.tracker.sessions == {}


def person(event_id, type_="new"):
    return FrigateEvent(type_, event_id, "front_door", "person")


# P0-17: one visit per camera at a time, plus a cooldown after it.


def test_event_during_a_visit_is_merged(clock):
    h = Harness(clock, dwell=0, cooldown=90)
    h.tracker.handle_event(person("A"))
    assert len(h.workers) == 1  # A's visit is running
    clock.advance(5)
    h.tracker.handle_event(person("B"))
    h.tracker.sweep()
    assert len(h.workers) == 1
    assert h.tracker.sessions["B"].merged_into == "A"
    assert h.tracker.sessions["B"].handled


def test_event_within_cooldown_is_merged(clock):
    h = Harness(clock, dwell=0, cooldown=90)
    h.tracker.handle_event(person("A"))
    clock.advance(25)  # the visit takes 25 s
    h.run_workers()
    clock.advance(89)
    h.tracker.handle_event(person("B"))
    assert h.workers == []
    assert h.tracker.sessions["B"].merged_into == "A"


def test_cooldown_runs_from_the_end_of_the_visit(clock):
    h = Harness(clock, dwell=0, cooldown=90)
    h.tracker.handle_event(person("A"))
    clock.advance(25)
    h.run_workers()
    clock.advance(91)
    h.tracker.handle_event(person("B"))
    assert len(h.workers) == 1
    h.run_workers()
    assert h.started == ["A", "B"]


def test_merge_survives_the_visit_event_ending(clock):
    # Frigate may end the first event before the visit finishes.
    h = Harness(clock, dwell=0, cooldown=90)
    h.tracker.handle_event(person("A"))
    h.tracker.handle_event(person("A", "end"))
    h.tracker.handle_event(person("B"))
    assert h.tracker.sessions["B"].merged_into == "A"
    h.run_workers()
    clock.advance(10)
    h.tracker.handle_event(person("C"))
    assert h.tracker.sessions["C"].merged_into == "A"


# P0-21: doorbell presses.


class TriggerHarness(Harness):
    def __init__(self, clock, **kw):
        super().__init__(clock, **kw)
        self.triggers = []
        self.tracker._run_visit = lambda eid, trigger: self.triggers.append((eid, trigger))


def test_press_starts_the_waiting_visit_without_dwell(clock):
    h = TriggerHarness(clock, dwell=3)
    h.send("person_new")
    assert h.workers == []
    assert h.tracker.press() == ("started", EID)
    h.run_workers()
    assert h.triggers == [(EID, "button")]


def test_press_with_nobody_detected_starts_a_new_visit(clock):
    h = TriggerHarness(clock, dwell=3)
    action, visit_id = h.tracker.press()
    assert action == "started" and visit_id == f"press-{clock.now:.3f}"
    h.run_workers()
    assert h.triggers == [(visit_id, "button")]


def test_press_during_a_visit_or_cooldown_is_merged(clock):
    h = TriggerHarness(clock, dwell=0, cooldown=90)
    h.send("person_new")  # starts the visit (dwell 0)
    assert h.tracker.press() == ("merged", EID)
    h.run_workers()
    clock.advance(30)
    assert h.tracker.press() == ("merged", EID)
    clock.advance(61)
    action, _ = h.tracker.press()
    assert action == "started"


def test_person_visit_trigger(clock):
    h = TriggerHarness(clock, dwell=0)
    h.send("person_new")
    h.run_workers()
    assert h.triggers == [(EID, "person")]


# P0-18: trigger zones.


def zoned(event_id, zones=(), type_="update"):
    return FrigateEvent(type_, event_id, "front_door", "person", tuple(zones))


def test_without_zones_configured_anywhere_counts(clock):
    h = Harness(clock, dwell=0)
    h.tracker.handle_event(zoned("A", type_="new"))
    assert len(h.workers) == 1


def test_sidewalk_passer_by_never_triggers(clock):
    h = Harness(clock, dwell=1)
    h.tracker.trigger_zones = frozenset({"porch"})
    h.tracker.handle_event(zoned("A", type_="new"))
    for _ in range(10):
        clock.advance(1)
        h.tracker.handle_event(zoned("A", ["sidewalk"]))
        h.tracker.sweep()
    assert h.workers == []


def test_dwell_counts_from_entering_the_porch(clock):
    h = Harness(clock, dwell=2)
    h.tracker.trigger_zones = frozenset({"porch"})
    h.tracker.handle_event(zoned("A", type_="new"))
    clock.advance(30)  # walking up the path
    h.tracker.handle_event(zoned("A", ["walkway"]))
    clock.advance(5)
    h.tracker.handle_event(zoned("A", ["walkway", "porch"]))
    assert h.workers == []  # just arrived; dwell restarts here
    clock.advance(2)
    h.tracker.sweep()
    assert len(h.workers) == 1


def test_press_ignores_zones(clock):
    h = Harness(clock, dwell=2)
    h.tracker.trigger_zones = frozenset({"porch"})
    h.tracker.handle_event(zoned("A", type_="new"))
    assert h.tracker.press() == ("started", "A")
    assert len(h.workers) == 1
