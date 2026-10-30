import pytest

from conftest import load_fixture
from gatekeeper.frigate import parse_event
from gatekeeper.perception import PersonEvent, inside
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
    tracker.handle_event(PersonEvent("new", EID, "front_door", "person"))
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
    return PersonEvent(type_, event_id, "front_door", "person")


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


def test_a_knock_starts_a_visit_like_a_press(clock):
    # Someone knocked, maybe out of view: a visit, told apart from the doorbell.
    h = TriggerHarness(clock, dwell=3)
    action, visit_id = h.tracker.press("knock")
    assert action == "started" and visit_id == f"press-{clock.now:.3f}"
    h.run_workers()
    assert h.triggers == [(visit_id, "knock")]
    # Like a press, the household rules (went in, recognized) don't cut it short.
    assert h.tracker._active_pressed is True


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
    return PersonEvent(type_, event_id, "front_door", "person", tuple(zones))


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
    on_porch = PersonEvent("new", "A", "front_door", "person", ("porch",), ("porch",))
    h.tracker.handle_event(on_porch)
    assert h.tracker.person_present()
    walked_off = PersonEvent("update", "A", "front_door", "person", ("porch",), ("walkway",))
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
    return PersonEvent(type_, event_id, "front_door", "person", ("porch",), ("porch",), path)


class DoorHarness:
    def __init__(self, clock, door=lambda point: inside(DOOR, point)):
        self.started: list[tuple[str, str]] = []
        self.not_greeted: list[tuple[str, str]] = []
        self.inside: list[str] = []
        self.household: list[tuple[str, str]] = []
        self.recognized: list[tuple[str, str]] = []
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
            on_went_inside=self.inside.append,
            face_min_matches=2,
            on_household=lambda event_id, name: self.household.append((event_id, name)),
            on_recognized=lambda visit_id, name: self.recognized.append((visit_id, name)),
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


def test_someone_who_stays_at_the_door_just_after_someone_else_is_greeted(clock):
    # Someone was just on camera: this could be their track, split, picked up at the door.
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("V", (0.45, 0.6), type_="new"))
    h.tracker.handle_event(walk("V", (0.45, 0.6), type_="end"))
    clock.advance(10)
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


def greeted(h, clock, event_id="A"):
    """Someone walks up the walkway and is greeted; the visit is still running."""
    h.tracker.handle_event(walk(event_id, (0.45, 0.6), type_="new"))
    clock.advance(1.0)
    h.tracker.handle_event(walk(event_id, (0.45, 0.6), (0.6, 0.9)))
    assert len(h.workers) == 1  # started, not run: the visit is in progress


def test_someone_whose_track_ends_at_the_door_went_inside(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    clock.advance(5)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h.inside == ["A"]


def test_walking_away_is_not_going_inside(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    clock.advance(20)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9), (0.45, 0.62), type_="end"))
    assert h.inside == []


def test_not_while_someone_else_is_still_on_the_porch(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    h.tracker.handle_event(walk("B", (0.5, 0.8), type_="new"))
    clock.advance(5)
    h.tracker.sweep()
    assert h.tracker.sessions["B"].merged_into == "A"
    h.tracker.handle_event(walk("A", (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h.inside == []
    # Then the second one goes in too: their event was merged into the visit.
    h.tracker.handle_event(walk("B", (0.5, 0.8), (0.88, 0.98), type_="end"))
    assert h.inside == ["A"]


def test_not_after_a_doorbell_press(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    assert h.tracker.press() == ("merged", "A")
    clock.advance(5)
    h.tracker.handle_event(walk("A", (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h.inside == []

    # A visit the press started.
    h2 = DoorHarness(clock)
    h2.tracker.handle_event(walk("C", (0.45, 0.6), (0.6, 0.9), type_="new"))
    assert h2.tracker.press() == ("started", "C")
    h2.tracker.handle_event(walk("C", (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h2.inside == []


def test_not_once_the_visit_is_over(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    h.run_workers()  # the visit finished
    h.tracker.handle_event(walk("A", (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h.inside == []


def test_not_while_the_door_zone_is_unknown(clock):
    h = DoorHarness(clock, door=lambda point: None)
    greeted(h, clock)
    h.tracker.handle_event(walk("A", (0.6, 0.9), (0.85, 0.97), type_="end"))
    assert h.inside == []


# Why a person in a trigger zone was dropped without a visit (the report's missed arrivals).


def ended_lines(caplog):
    return [r.getMessage() for r in caplog.records if "ended before a visit" in r.getMessage()]


def test_a_person_gone_before_the_dwell_is_logged(clock, caplog):
    caplog.set_level("INFO", logger="gatekeeper.sessions")
    h = Harness(clock, dwell=2)
    h.tracker.trigger_zones = frozenset({"porch"})
    h.tracker.handle_event(zoned("A", ["porch"], type_="new"))
    clock.advance(0.5)
    h.tracker.handle_event(zoned("A", ["porch"], type_="end"))
    assert h.workers == []
    assert ended_lines(caplog) == [
        "person event A ended before a visit: 0.5 s in the trigger zone, under the 2 s dwell"
    ]


def test_people_never_due_a_visit_are_not_logged(clock, caplog):
    caplog.set_level("INFO", logger="gatekeeper.sessions")
    h = Harness(clock, dwell=0)
    h.tracker.trigger_zones = frozenset({"porch"})
    h.tracker.handle_event(zoned("A", ["sidewalk"], type_="new"))  # never on the porch
    h.tracker.handle_event(zoned("A", ["sidewalk"], type_="end"))
    h.tracker.handle_event(zoned("B", ["porch"], type_="new"))  # greeted
    h.run_workers()
    h.tracker.handle_event(zoned("B", ["porch"], type_="end"))
    assert h.started == ["B"]
    assert ended_lines(caplog) == []


def test_someone_at_the_door_whose_event_ends_during_the_watch_is_logged(clock, caplog):
    caplog.set_level("INFO", logger="gatekeeper.sessions")
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("A", (0.85, 0.99), type_="end"))
    # Came out and went straight back in: logged, so it isn't an arrival "not greeted".
    assert h.workers == [] and h.not_greeted == [("A", "stepped_out")]
    assert ended_lines(caplog) == []
    assert "came out of the door and went back in" in caplog.text


def test_an_expired_session_is_logged(clock, caplog):
    caplog.set_level("INFO", logger="gatekeeper.sessions")
    h = Harness(clock, dwell=1000, ttl=120)
    h.tracker.handle_event(zoned("A", type_="new"))
    clock.advance(121)
    h.tracker.sweep()
    assert h.tracker.sessions == {}
    assert ended_lines(caplog) == [
        "person event A ended before a visit: no news from Frigate for 120 s; "
        "121.0 s in the trigger zone, under the 1000 s dwell"
    ]


# Faces (face_watch.py).


def test_someone_recognized_before_the_greeting_is_not_greeted(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.45, 0.6), type_="new"))  # on the walkway
    h.tracker.face_seen("A", "Alex")
    assert h.household == []  # one match isn't enough
    h.tracker.face_seen("A", "Alex")
    clock.advance(1.0)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9)))  # on the porch, due
    assert h.workers == [] and h.household == [("A", "Alex")]
    # Frigate splits their track: still them.
    h.tracker.handle_event(walk("B", (0.62, 0.92), type_="new"))
    clock.advance(1.0)
    h.tracker.sweep()
    assert h.workers == [] and h.tracker.sessions["B"].merged_into == "A"


def test_recognized_once_due_means_not_greeted_at_once(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.45, 0.6), type_="new"))
    h.tracker.face_seen("A", "Alex")
    h.tracker.face_seen("A", "Sam")  # matches for different people don't add up
    assert h.household == []
    h.tracker.face_seen("A", "Alex")
    assert h.household == []  # still on the walkway: not due yet
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9)))
    clock.advance(1.0)
    h.tracker.sweep()
    assert h.household == [("A", "Alex")]


def test_someone_recognized_during_their_visit_stops_it(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    h.tracker.face_seen("A", "Alex")
    h.tracker.face_seen("A", "Alex")
    assert h.recognized == [("A", "Alex")] and h.household == []


def test_a_face_never_overrides_a_doorbell_press(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9), type_="new"))
    assert h.tracker.press() == ("started", "A")
    h.tracker.face_seen("A", "Alex")
    h.tracker.face_seen("A", "Alex")
    assert h.recognized == [] and h.household == []

    h2 = DoorHarness(clock)
    greeted(h2, clock)
    assert h2.tracker.press() == ("merged", "A")
    h2.tracker.face_seen("A", "Alex")
    h2.tracker.face_seen("A", "Alex")
    assert h2.recognized == []


def test_unknown_faces_change_nothing(clock):
    h = DoorHarness(clock)
    greeted(h, clock)
    for _ in range(5):
        h.tracker.face_seen("A", None)
    h.tracker.face_seen("gone", "Alex")
    assert h.recognized == [] and h.household == []


def test_people_lists_everyone_frigate_sees(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9), type_="new"))
    h.tracker.press()  # a press with someone there: no extra "person"
    h2 = DoorHarness(clock)
    h2.tracker.press()  # a press with nobody seen
    assert h.tracker.people() == [("A", clock(), 0.6)]
    assert h2.tracker.people() == []


# Coming out of the door with nobody else around (came_out_quiet_seconds).


def test_someone_who_comes_out_and_lingers_at_the_door_is_never_greeted(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.91, 0.98), type_="new"))  # nobody else around
    for _ in range(12):  # locking the door, letting the dog out: 12 s at the door
        clock.advance(1.0)
        h.tracker.handle_event(walk("A", (0.91, 0.98), (0.85, 0.95)))
        h.tracker.sweep()
    assert h.workers == []
    h.tracker.handle_event(walk("A", (0.91, 0.98), (0.85, 0.95), (0.6, 0.85)))  # walks off
    assert h.workers == [] and h.left == ["A"]


def test_someone_who_comes_out_and_goes_back_in_is_logged_as_stepping_out(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.91, 0.98), type_="new"))
    clock.advance(20)
    h.tracker.handle_event(walk("A", (0.91, 0.98), (0.8, 0.9)))
    h.tracker.sweep()
    clock.advance(5)
    h.tracker.handle_event(walk("A", (0.91, 0.98), (0.8, 0.9), (0.92, 0.96), type_="end"))
    assert h.workers == [] and h.not_greeted == [("A", "stepped_out")]


def test_a_doorbell_press_still_greets_someone_who_came_out(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("A", (0.91, 0.98), type_="new"))
    clock.advance(10)
    assert h.tracker.press() == ("started", "A")
    h.run_workers()
    assert h.started == [("A", "button")]


def test_coming_out_needs_30_quiet_seconds_before(clock):
    h = DoorHarness(clock)
    h.tracker.handle_event(walk("V", (0.45, 0.6), type_="new"))
    h.tracker.handle_event(walk("V", (0.45, 0.6), type_="end"))
    clock.advance(31)  # long enough: alone
    h.tracker.handle_event(walk("A", (0.91, 0.98), type_="new"))
    clock.advance(10)
    h.tracker.sweep()
    assert h.workers == []


def walk_up(h, clock, event_id):
    """Someone walking up from the walkway to the porch, then out of sight."""
    h.tracker.handle_event(walk(event_id, (0.45, 0.6), type_="new"))
    clock.advance(1.0)
    h.tracker.handle_event(walk(event_id, (0.45, 0.6), (0.6, 0.9)))
    h.run_workers()
    h.tracker.handle_event(walk(event_id, (0.45, 0.6), (0.6, 0.9), (0.45, 0.6), type_="end"))


def test_a_couriers_further_trips_arent_greeted(clock):
    # A grocery delivery in four trips in three minutes, greeted twice before.
    h = DoorHarness(clock)
    walk_up(h, clock, "A")  # the first trip: greeted
    clock.advance(30)
    h.tracker.delivered("package-1")  # the doorstep check sees the bags
    clock.advance(20)
    walk_up(h, clock, "B")  # back with more
    clock.advance(170)
    walk_up(h, clock, "C")  # the window runs from the last trip
    clock.advance(200)
    walk_up(h, clock, "D")  # long after: a new visitor
    assert h.started == [("A", "person"), ("D", "person")]
    assert h.not_greeted == [("B", "delivery_trip"), ("C", "delivery_trip")]


def test_the_pickup_ends_the_delivery(clock):
    h = DoorHarness(clock)
    h.tracker.delivered("package-1")
    h.tracker.picked_up()
    clock.advance(10)
    walk_up(h, clock, "A")
    assert h.started == [("A", "person")] and h.not_greeted == []


def test_during_a_delivery_a_press_greets_and_the_household_is_the_household(clock):
    h = DoorHarness(clock)
    h.tracker.delivered("package-1")
    clock.advance(40)
    # Someone comes out of the door for the bags and walks away: they left, not a trip.
    h.tracker.handle_event(walk("B", (0.9, 0.98), type_="new"))
    clock.advance(1.5)
    h.tracker.handle_event(walk("B", (0.9, 0.98), (0.55, 0.86)))
    assert h.not_greeted == [("B", "left")]
    # A doorbell press always greets.
    clock.advance(60)
    h.tracker.handle_event(walk("C", (0.45, 0.6), type_="new"))
    assert h.tracker.press() == ("started", "C")
    h.run_workers()
    assert h.started == [("C", "button")]


def test_no_delivery_trips_when_it_is_zero(clock):
    h = DoorHarness(clock)
    h.tracker.delivery_trip_seconds = 0
    h.tracker.delivered("package-1")
    walk_up(h, clock, "A")
    assert h.started == [("A", "person")]


def test_walking_away_is_back_up_the_frame_from_the_closest_point(clock):
    h = DoorHarness(clock)
    assert h.tracker.walking_away()  # nobody on camera
    h.tracker.handle_event(walk("A", (0.45, 0.6), type_="new"))
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.55, 0.85)))
    assert not h.tracker.walking_away()  # coming up the walk
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.55, 0.85), (0.55, 0.82)))
    assert not h.tracker.walking_away()  # a step back isn't leaving
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.55, 0.85), (0.5, 0.74)))
    assert h.tracker.walking_away()  # back down the walk
    h.tracker.handle_event(walk("B", (0.45, 0.6), type_="new"))
    assert not h.tracker.walking_away()  # but someone else is still there


def test_in_visit(clock):
    h = DoorHarness(clock)
    assert not h.tracker.in_visit()
    h.tracker.handle_event(walk("A", (0.45, 0.6), type_="new"))
    clock.advance(1.0)
    h.tracker.handle_event(walk("A", (0.45, 0.6), (0.6, 0.9)))
    assert h.tracker.in_visit()  # started, its worker not run yet
    h.run_workers()
    assert not h.tracker.in_visit()
