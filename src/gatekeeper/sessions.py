"""Per-Frigate-event session tracking and dwell gating.

A session is created for each Frigate person event on the configured camera.
Once the person has been seen for ``dwell_seconds`` the visit is started on a
worker; each session triggers at most one visit.

Only one visit runs per camera. A session that becomes due while a visit is
running, or within ``cooldown_seconds`` after one ended, is merged into that
visit instead of greeting again (a person stepping out of frame and back, or
Frigate splitting one person's track into several events).

With ``trigger_zones`` set, a person event only starts a visit once it has
entered one of those Frigate zones (e.g. the porch, not the sidewalk), and the
dwell time counts from that entry.

A doorbell press starts the waiting visit at once (skipping the dwell), or a
new one if Frigate hasn't seen anyone yet; during a visit or its cooldown it is
reported back as merged so the caller can still notify.

With a ``door`` zone, someone who appears at the front door is the household
coming out. They get ``leaving_watch_seconds`` (instead of the dwell) to walk out
of the door zone; if they do, they aren't greeted, and person events in the next
``leaving_cooldown_seconds`` count as them too (Frigate sometimes splits a track).
If they stay at the door, they're greeted. A doorbell press always greets.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from gatekeeper.frigate import FrigateEvent, Point

log = logging.getLogger(__name__)


@dataclass
class Session:
    first_seen: float
    last_seen: float
    handled: bool = False
    in_progress: bool = False
    merged_into: str | None = None
    trigger: str = "person"  # or "button"
    zones: frozenset[str] = frozenset()  # Frigate zones the person has entered
    current_zones: frozenset[str] = frozenset()  # where they are now
    created: float = 0.0  # when Frigate first reported the person
    path_start: Point | None = None  # where they appeared
    path_now: Point | None = None  # where they are now


def spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class SessionTracker:
    def __init__(
        self,
        *,
        camera: str,
        dwell_seconds: float,
        ttl_seconds: float,
        run_visit: Callable[[str, str], None],
        cooldown_seconds: float = 0.0,
        trigger_zones: Iterable[str] = (),
        door: Callable[[Point], bool | None] | None = None,
        leaving_watch_seconds: float = 3.0,
        leaving_cooldown_seconds: float = 15.0,
        on_left: Callable[[str], None] = lambda event_id: None,
        spawn: Callable[[Callable[[], None]], None] = spawn_thread,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.camera = camera
        self.dwell_seconds = dwell_seconds
        self.ttl_seconds = ttl_seconds
        self.cooldown_seconds = cooldown_seconds
        self.trigger_zones = frozenset(trigger_zones)
        # Is a point in the door zone? None while the zone is unknown.
        self.door = door
        self.leaving_watch_seconds = leaving_watch_seconds
        self.leaving_cooldown_seconds = leaving_cooldown_seconds
        self._on_left = on_left
        self._run_visit = run_visit
        self._spawn = spawn
        self._clock = clock
        self._lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self._active_visit: str | None = None
        self._last_visit: str | None = None
        self._cooldown_until = 0.0
        self._last_left: str | None = None
        self._left_until = 0.0

    def handle_event(self, event: FrigateEvent) -> None:
        if event.camera != self.camera or event.label != "person":
            return

        if event.type in {"new", "update"}:
            now = self._clock()
            with self._lock:
                session = self.sessions.get(event.id)
                if session is None:
                    session = self.sessions[event.id] = Session(
                        first_seen=now, last_seen=now, created=now
                    )
                else:
                    session.last_seen = now
                if event.path:  # the whole path so far, in every message
                    session.path_start, session.path_now = event.path[0], event.path[-1]
                zones = session.zones | frozenset(event.entered_zones)
                if self._in_trigger_zone(zones) and not self._in_trigger_zone(session.zones):
                    session.first_seen = now  # dwell counts from entering the zone
                session.zones = zones
                session.current_zones = frozenset(event.current_zones)
            self.maybe_start(event.id)

        elif event.type == "end":
            with self._lock:
                self.sessions.pop(event.id, None)

    def maybe_start(self, event_id: str) -> bool:
        """Start the visit for ``event_id`` if its dwell time has passed."""
        with self._lock:
            session = self.sessions.get(event_id)
            if not session or session.handled or session.in_progress:
                return False
            now = self._clock()
            if session.trigger == "person" and not self._in_trigger_zone(session.zones):
                return False
            if now - session.first_seen < self.dwell_seconds:
                return False
            merge_into = self._active_visit or (
                self._last_visit if now < self._cooldown_until else None
            )
            if merge_into:
                session.handled = True
                session.merged_into = merge_into
                log.info("person event %s merged into visit %s", event_id, merge_into)
                return False
            verdict = self._door_verdict(session, now) if session.trigger == "person" else "greet"
            if verdict == "watch":
                return False
            if verdict == "after_left":
                session.handled = True
                session.merged_into = self._last_left
                log.info(
                    "person event %s merged into %s, who just left by the door",
                    event_id,
                    self._last_left,
                )
                return False
            if verdict == "left":
                session.handled = True
                self._last_left = event_id
                self._left_until = now + self.leaving_cooldown_seconds
            else:
                session.in_progress = True
                self._active_visit = event_id

        if verdict == "left":
            log.info("person event %s came out of the door and walked away: not greeted", event_id)
            self._on_left(event_id)
            return False

        trigger = session.trigger

        def worker() -> None:
            try:
                self._run_visit(event_id, trigger)
            finally:
                self._finish(event_id)

        self._spawn(worker)
        return True

    def person_present(self) -> bool:
        """Does Frigate still see someone (in a trigger zone, if any are set)?
        Its "end" event drops a person a few seconds after they've left."""
        with self._lock:
            # "press-..." sessions are doorbell presses with no one seen yet.
            sessions = [s for eid, s in self.sessions.items() if not eid.startswith("press-")]
        if not self.trigger_zones:
            return bool(sessions)
        return any(self.trigger_zones & s.current_zones for s in sessions)

    def _door_verdict(self, session: Session, now: float) -> str:
        """For a person due a greeting: "greet"; "watch" (they appeared at the door:
        wait to see whether they walk away); "left" (they did); or "after_left"
        (someone just left by the door, and this is most likely them again)."""
        if self.door is None:
            return "greet"
        if now < self._left_until:
            return "after_left"
        if session.path_start is None or not self.door(session.path_start):
            return "greet"
        if session.path_now is not None and self.door(session.path_now) is False:
            return "left"
        if now - session.created < self.leaving_watch_seconds:
            return "watch"
        return "greet"  # they stayed at the door

    def _in_trigger_zone(self, zones: frozenset[str]) -> bool:
        return not self.trigger_zones or bool(zones & self.trigger_zones)

    def press(self) -> tuple[str, str]:
        """Handle a doorbell press. Returns ``("started", visit_id)`` if a visit
        starts now, or ``("merged", visit_id)`` if one is running or just ended."""
        now = self._clock()
        with self._lock:
            merge_into = self._active_visit or (
                self._last_visit if now < self._cooldown_until else None
            )
            if merge_into:
                log.info("doorbell pressed during visit %s", merge_into)
                return "merged", merge_into
            waiting = [
                (s.first_seen, eid)
                for eid, s in self.sessions.items()
                if not s.handled and not s.in_progress
            ]
            if waiting:
                event_id = max(waiting)[1]  # the most recent person
            else:
                event_id = f"press-{now:.3f}"
                self.sessions[event_id] = Session(first_seen=now, last_seen=now)
            session = self.sessions[event_id]
            session.first_seen = min(session.first_seen, now - self.dwell_seconds)
            session.trigger = "button"
        self.maybe_start(event_id)
        return "started", event_id

    def _finish(self, event_id: str) -> None:
        with self._lock:
            session = self.sessions.get(event_id)
            if session:
                session.handled = True
                session.in_progress = False
            if self._active_visit == event_id:
                self._active_visit = None
            self._last_visit = event_id
            self._cooldown_until = self._clock() + self.cooldown_seconds

    def sweep(self) -> None:
        """Expire stale sessions and start any whose dwell time has now passed."""
        now = self._clock()
        with self._lock:
            expired = [
                eid for eid, s in self.sessions.items() if now - s.last_seen > self.ttl_seconds
            ]
            for eid in expired:
                del self.sessions[eid]
            pending = list(self.sessions)

        for eid in pending:
            self.maybe_start(eid)

    def sweep_forever(self, interval: float, on_sweep: Callable[[], None] | None = None) -> None:
        while True:
            try:
                self.sweep()
                if on_sweep:
                    on_sweep()
            except Exception:
                log.exception("session sweep error")
            time.sleep(interval)
