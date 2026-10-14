import pytest

from conftest import load_fixture
from gatekeeper.frigate import FrigateEvent, inside, parse_event
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


# Is someone still on the porch? (Used before warning an officer a second time.)


def test_person_present_until_frigate_ends_their_event(clock):
    h = Harness(clock, dwell=0)
    assert not h.tracker.person_present()
    h.tracker.handle_event(zoned("A", type_="new"))
    assert h.tracker.person_present()
    h.tracker.handle_event(zoned("A", type_="end"))
    assert not h.tracker.person_present()


def test_person_present_only_counts_people_in_a_trigger_zone_now(clock):
    h = Harness(clock, dwell=0)
    h.tracker.trigger_zones = frozenset({"porch"})
    on_porch = FrigateEvent("new", "A", "front_door", "person", ("porch",), ("porch",))
    h.tracker.handle_event(on_porch)
    assert h.tracker.person_present()
    walked_off = FrigateEvent("update", "A", "front_door", "person", ("porch",), ("walkway",))
    h.tracker.handle_event(walked_off)
    assert not h.tracker.person_present()


def test_a_press_alone_is_not_someone_seen(clock):
    h = Harness(clock, dwell=0)
    h.tracker.press()
    assert not h.tracker.person_present()


# The household coming out of the front door (DOOR_ZONE).

DOOR = [(0.7, 0.85), (1.0, 0.85), (1.0, 1.0), (0.7, 1.0)]


def walk(event_id, *path, type_="update"):
    """A person event on the porch with Frigate's path so far."""
    return FrigateEvent(type_, event_id, "front_door", "person", ("porch",), ("porch",), path)


class DoorHarness:
    def __init__(self, clock, door=lambda point: inside(DOOR, point)):
        self.started: list[tuple[str, str]] = []
        self.not_greeted: list[tuple[str, str]] = []
        self.workers = []
        self.tracker = SessionTracker(
            camera="front_door",
            dwell_seconds=1.0,
            ttl_seconds=120.0,
            trigger_zones=["porch"],
            door=door,
            leaving_watch_seconds=3.0,
            leaving_cooldown_seconds=15.0,
            return_window_seconds=120.0,
            on_not_greeted=lambda event_id, outcome: self.not_greeted.append((event_id, outcome)),
            run_visit=lambda event_id, trigger: self.started.append((event_id, trigger)),
            spawn=self.workers.append,
            clock=clock,
        )

    @property
    def left(self):
        return [e for e, outcome in self.not_greeted if outcome == "left"]

    def run_workers(self):
        while self.workers:
            self.workers.pop(0)()


def test_someone_coming_out_of_the_door_and_walking_away_is_not_greeted(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="new"))
    clock.advance(1.2)  # past the dwell, still at the door: wait and see
    h.tracker.handle_event(walk("A", (0.85, 0.99), (0.78, 0.95)))
    h.tracker.sweep()
    assert h.workers == [] and h.left == []
    clock.advance(0.6)
    h.tracker.handle_event(walk("A", (0.85, 0.99), (0.78, 0.95), (0.62, 0.88)))
    assert h.workers == []
    assert h.left == ["A"]
    assert h.tracker.sessions["A"].handled


def test_a_split_track_right_after_is_them_too(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("A", (0.85, 0.99), (0.6, 0.88)))
    assert h.left == ["A"]

    # Frigate loses them and starts a new event further down the path.
    h.tracker.handle_event(walk("B", (0.58, 0.86), type_="new"))
    clock.advance(1.5)
    h.tracker.sweep()
    assert h.workers == []
    assert h.tracker.sessions["B"].merged_into == "A"

    # After the return window, someone new on the porch is greeted as usual.
    clock.advance(120)
    h.tracker.handle_event(walk("C", (0.45, 0.6), (0.6, 0.9), type_="new"))
    clock.advance(1.0)
    h.tracker.sweep()
    h.run_workers()
    assert h.started == [("C", "person")]


def test_someone_who_stays_at_the_door_is_greeted_after_the_watch(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.9, 0.99), type_="new"))
    clock.advance(2.0)
    h.tracker.handle_event(walk("A", (0.9, 0.99), (0.82, 0.97)))
    h.tracker.sweep()
    assert h.workers == []
    clock.advance(1.0)  # 3 s at the door
    h.tracker.sweep()
    h.run_workers()
    assert h.started == [("A", "person")]
    assert h.left == []


def test_visitors_from_the_walkway_are_greeted_without_waiting(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.45, 0.6), type_="new"))
    clock.advance(1.0)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9)))
    h.run_workers()
    assert h.started == [("A", "person")]


def test_a_press_always_greets(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="new"))
    assert h.tracker.press() == ("started", "A")
    h.run_workers()
    assert h.started == [("A", "button")]

    # Also right after someone left by the door.
    h2 = DoorHarness(clock)
    h2.tracker.handle_event(walk("B", (0.85, 0.99), type_="new"))
    clock.advance(1.5)
    h2.tracker.handle_event(walk("B", (0.85, 0.99), (0.6, 0.88)))
    assert h2.left == ["B"]
    action, visit = h2.tracker.press()
    assert action == "started" and visit.startswith("press-")


def test_without_a_known_door_zone_everyone_is_greeted(clock):
    h = DoorHarness(clock, door=lambda point: None)  # Frigate's config not read yet
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="new"))
    clock.advance(1.0)
    h.tracker.sweep()
    h.run_workers()
    assert h.started == [("A", "person")]


def test_someone_walking_up_soon_after_is_them_coming_back(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.9, 0.98), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("A", (0.9, 0.98), (0.55, 0.86)))  # out to the car
    clock.advance(45)
    h.tracker.handle_event(walk("B", (0.54, 0.86), type_="new"))  # and back to the door
    clock.advance(1.0)
    h.tracker.handle_event(walk("B", (0.54, 0.86), (0.77, 1.0)))
    h.tracker.sweep()
    assert h.workers == []
    assert h.not_greeted == [("A", "left"), ("B", "returned")]


def test_someone_else_coming_out_during_the_window_is_judged_as_before(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.9, 0.98), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("A", (0.9, 0.98), (0.55, 0.86)))
    clock.advance(30)
    h.tracker.handle_event(walk("B", (0.88, 0.99), type_="new"))  # at the door, stays
    clock.advance(3.0)
    h.tracker.sweep()
    h.run_workers()
    assert h.started == [("B", "person")]
    assert h.not_greeted == [("A", "left")]


def test_no_return_window_when_it_is_zero(clock):
    h = DoorHarness(clock)
    h.tracker.return_window_seconds = 0
    h.tracker.handle_event(walk("A", (0.9, 0.98), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("A", (0.9, 0.98), (0.55, 0.86)))
    clock.advance(30)
    h.tracker.handle_event(walk("B", (0.54, 0.86), type_="new"))
    clock.advance(1.0)
    h.tracker.sweep()
    h.run_workers()
    assert h.started == [("B", "person")]
