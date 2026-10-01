import pytest

from conftest import load_fixture
from gatekeeper.frigate import FrigateEvent, parse_event
from gatekeeper.sessions import SessionTracker

EID = "1727712000.123456-abc123"


class Harness:
    """A tracker whose visit workers are queued instead of run on threads."""

    def __init__(self, clock, dwell=1.0, ttl=120.0):
        self.started: list[str] = []
        self.workers = []
        self.tracker = SessionTracker(
            camera="front_door",
            dwell_seconds=dwell,
            ttl_seconds=ttl,
            run_visit=self.started.append,
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
    def boom(_eid):
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
