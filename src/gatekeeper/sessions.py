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
coming out. If nobody else was on camera then, or in the ``came_out_quiet_seconds``
before, that's certain enough: they aren't greeted however long they stay at the door
(locking it, letting the dog out), and when they go back in without leaving the door
zone it's logged as ``stepped_out``. Otherwise (someone else was just there: a
visitor's track can split, and pick up again at the door) they get
``leaving_watch_seconds`` (instead of the dwell) to walk out of the door zone. Either
way, if they walk out of it they aren't greeted, and person events in the next
``leaving_cooldown_seconds`` count as them too (Frigate sometimes splits a track).
Someone who walks up within ``return_window_seconds`` of that is most likely them
coming back in (from the car or the mailbox), and isn't greeted either. If they
stay at the door, they're greeted. A doorbell press always greets.

The door zone also tells when someone goes in: a person whose track ends there
during a visit, with nobody else left on the porch, walked into the house. That's
the household coming home (or someone they let in), so ``on_went_inside`` tells
the visit, which stops if they haven't said anything. Not after a doorbell press.

With face recognition (face_watch.py), a person whose face matched someone the owner
enrolled ``face_min_matches`` times is them: not greeted if their visit hasn't
started (``on_household``), and their visit stops if it has (``on_recognized``).
A doorbell press still greets.

A delivery often takes several trips (groceries, a pile of parcels). Once one was seen
(``delivered``: a package left, or a visitor who said they're delivering), the next
people to walk up within ``delivery_trip_seconds`` of it, or of the last trip, are the
courier again: logged as ``delivery_trip``, not greeted. Short and ended by the pickup
(``picked_up``), as thieves follow couriers; whoever takes the package still shows in
the pickup alert. The door rules come first (the household coming out for the bags),
and a doorbell press still greets.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from gatekeeper.perception import PersonEvent, Point

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
    lowest: float = 0.0  # the lowest they've been in the frame: the closest to the door
    faces: dict[str, int] = field(default_factory=dict)  # face matches by name
    known: str | None = None  # who they are, once their face matched often enough
    alone: bool = False  # nobody else on camera when they appeared, nor just before


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
        return_window_seconds: float = 120.0,
        came_out_quiet_seconds: float = 30.0,
        delivery_trip_seconds: float = 180.0,
        on_not_greeted: Callable[[str, str], None] = lambda event_id, outcome: None,
        on_went_inside: Callable[[str], None] = lambda visit_id: None,
        face_min_matches: int = 2,
        on_household: Callable[[str, str], None] = lambda event_id, name: None,
        on_recognized: Callable[[str, str], None] = lambda visit_id, name: None,
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
        self.return_window_seconds = return_window_seconds
        self.came_out_quiet_seconds = came_out_quiet_seconds
        self.delivery_trip_seconds = delivery_trip_seconds
        # Called with (event_id, outcome) for people not greeted: "left", "returned",
        # "stepped_out", "delivery_trip".
        self._on_not_greeted = on_not_greeted
        # Called with the visit's id when the person it's for went in by the door.
        self._on_went_inside = on_went_inside
        self.face_min_matches = face_min_matches
        # Called with (event id, name) for someone recognized before their greeting,
        # and (visit id, name) for someone recognized during their visit.
        self._on_household = on_household
        self._on_recognized = on_recognized
        self._run_visit = run_visit
        self._spawn = spawn
        self._clock = clock
        self._lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self._active_visit: str | None = None
        self._active_pressed = False  # the doorbell was pressed for the active visit
        self._last_visit: str | None = None
        self._cooldown_until = 0.0
        self._last_left: str | None = None
        self._left_until = 0.0
        self._left_at = 0.0
        self._return_until = 0.0
        self._last_person = float("-inf")  # when any person event last came
        self._last_household: str | None = None
        self._household_until = 0.0
        self._delivery: str | None = None  # the delivery whose courier may come back
        self._delivery_until = 0.0

    def handle_event(self, event: PersonEvent) -> None:
        if event.camera != self.camera or event.label != "person":
            return

        if event.type in {"new", "update"}:
            now = self._clock()
            with self._lock:
                session = self.sessions.get(event.id)
                if session is None:
                    others = any(not eid.startswith("press-") for eid in self.sessions)
                    session = self.sessions[event.id] = Session(
                        first_seen=now,
                        last_seen=now,
                        created=now,
                        alone=not others and now - self._last_person >= self.came_out_quiet_seconds,
                    )
                else:
                    session.last_seen = now
                self._last_person = now
                if event.path:  # the whole path so far, in every message
                    session.path_start, session.path_now = event.path[0], event.path[-1]
                    session.lowest = max(session.lowest, *(y for _, y in event.path))
                zones = session.zones | frozenset(event.entered_zones)
                if self._in_trigger_zone(zones) and not self._in_trigger_zone(session.zones):
                    session.first_seen = now  # dwell counts from entering the zone
                session.zones = zones
                session.current_zones = frozenset(event.current_zones)
            self.maybe_start(event.id)

        elif event.type == "end":
            now = self._clock()
            with self._lock:
                session = self.sessions.pop(event.id, None)
                self._last_person = now
                stepped = self._stepped_out(event, session)
                why = self._why_not_started(session, now) if session and not stepped else None
                visit = self._went_inside(event, session)
            if stepped:
                log.info(
                    "person event %s came out of the door and went back in: not greeted", event.id
                )
                self._on_not_greeted(event.id, "stepped_out")
            if why:
                log.info("person event %s ended before a visit: %s", event.id, why)
            if visit:
                log.info("person event %s went in by the door during visit %s", event.id, visit)
                self._on_went_inside(visit)

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
            if session.trigger != "person":
                verdict = "greet"
            elif session.known:
                verdict = "household"
            elif now < self._household_until:
                verdict = "after_household"
            else:
                verdict = self._door_verdict(session, now)
                if verdict == "greet" and now < self._delivery_until:
                    verdict = "delivery_trip"
            if verdict == "watch":
                return False
            if verdict == "after_household":
                session.handled = True
                session.merged_into = self._last_household
                log.info(
                    "person event %s merged into %s, who was just recognized",
                    event_id,
                    self._last_household,
                )
                return False
            if verdict == "household":
                session.handled = True
                self._last_household = event_id
                self._household_until = now + self.leaving_cooldown_seconds
            elif verdict == "after_left":
                session.handled = True
                session.merged_into = self._last_left
                log.info(
                    "person event %s merged into %s, who just left by the door",
                    event_id,
                    self._last_left,
                )
                return False
            elif verdict == "delivery_trip":
                session.handled = True
                session.merged_into = self._delivery
                self._delivery_until = now + self.delivery_trip_seconds
            elif verdict in ("left", "returned"):
                session.handled = True
                if verdict == "left":
                    self._last_left, self._left_at = event_id, now
                    self._left_until = now + self.leaving_cooldown_seconds
                    self._return_until = now + self.return_window_seconds
            else:
                session.in_progress = True
                self._active_visit = event_id
                self._active_pressed = session.trigger == "button"

        if verdict == "left":
            log.info("person event %s came out of the door and walked away: not greeted", event_id)
        if verdict == "returned":
            log.info(
                "person event %s came back %.0f s after someone left by the door: not greeted",
                event_id,
                now - self._left_at,
            )
        if verdict == "delivery_trip":
            log.info(
                "person event %s is another trip of delivery %s: not greeted",
                event_id,
                self._delivery,
            )
        if verdict in ("left", "returned", "delivery_trip"):
            self._on_not_greeted(event_id, verdict)
            return False
        if verdict == "household":
            log.info("person event %s is %s: not greeted", event_id, session.known)
            self._on_household(event_id, session.known)
            return False

        trigger = session.trigger

        def worker() -> None:
            try:
                self._run_visit(event_id, trigger)
            finally:
                self._finish(event_id)

        self._spawn(worker)
        return True

    def delivered(self, delivery_id: str) -> None:
        """A delivery was just seen (a package left, or a visitor who said they're
        delivering): its courier may come back with more."""
        with self._lock:
            self._delivery = delivery_id
            self._delivery_until = self._clock() + self.delivery_trip_seconds
        log.info(
            "delivery %s: further trips for %.0f s aren't greeted",
            delivery_id,
            self.delivery_trip_seconds,
        )

    def picked_up(self) -> None:
        """The package was taken in: the delivery is over."""
        with self._lock:
            self._delivery_until = 0.0

    def people(self) -> list[tuple[str, float, float | None]]:
        """Everyone Frigate sees now (for face_watch): their event id, when Frigate first
        reported them, and the x of where they are (a fraction of the width, or None)."""
        with self._lock:
            return [
                (eid, s.created, s.path_now[0] if s.path_now else None)
                for eid, s in self.sessions.items()
                if not eid.startswith("press-")
            ]

    def face_seen(self, event_id: str, name: str | None) -> None:
        """face_watch: a face of person event ``event_id``, matching ``name`` (None: no
        one enrolled). Once it has matched the same name ``face_min_matches`` times,
        they're that person."""
        if not name:
            return
        with self._lock:
            session = self.sessions.get(event_id)
            if session is None or session.known or session.trigger != "person":
                return
            session.faces[name] = session.faces.get(name, 0) + 1
            if session.faces[name] < self.face_min_matches:
                return
            session.known = name
            visit = self._active_visit
            during = (
                visit is not None
                and visit in (event_id, session.merged_into)
                and not self._active_pressed
            )
        log.info("person event %s recognized as %s", event_id, name)
        if during:
            self._on_recognized(visit, name)
        else:
            self.maybe_start(event_id)  # due a greeting now? Then they won't get one

    def walking_away(self, margin: float = 0.08) -> bool:
        """Is everyone on camera walking away, or gone? A doorbell camera looks out from the
        door, so someone leaving moves up the frame: true once each person's feet are
        ``margin`` above the lowest (closest) they came. A courier who dropped something
        off is on their way back by the time the greeting gets no answer."""
        with self._lock:
            people = [s for eid, s in self.sessions.items() if not eid.startswith("press-")]
            return all(
                s.path_now is not None and s.lowest - s.path_now[1] >= margin for s in people
            )

    def person_present(self) -> bool:
        """Does Frigate still see someone (in a trigger zone, if any are set)?
        Its "end" event drops a person a few seconds after they've left."""
        with self._lock:
            return self._present()

    def _present(self) -> bool:
        # "press-..." sessions are doorbell presses with no one seen yet.
        sessions = [s for eid, s in self.sessions.items() if not eid.startswith("press-")]
        if not self.trigger_zones:
            return bool(sessions)
        return any(self.trigger_zones & s.current_zones for s in sessions)

    def _went_inside(self, event: PersonEvent, session: Session | None) -> str | None:
        """The active visit's id if the person whose event just ended went in by the
        door: their track ended in the door zone, they were this visit's (its own
        event, or one merged into it), nobody pressed the doorbell, and nobody else
        is left on the porch."""
        visit = self._active_visit
        if session is None or visit is None or self.door is None or self._active_pressed:
            return None
        if visit not in (event.id, session.merged_into):
            return None
        last = event.path[-1] if event.path else session.path_now
        if last is None or not self.door(last) or self._present():
            return None
        return visit

    def _door_verdict(self, session: Session, now: float) -> str:
        """For a person due a greeting: "greet"; "watch" (they appeared at the door:
        wait to see whether they walk away); "left" (they did); "after_left"
        (someone just left by the door, and this is most likely them again); or
        "returned" (walking up soon after someone left: most likely them coming back)."""
        if self.door is None:
            return "greet"
        if now < self._left_until:
            return "after_left"
        if session.path_start is not None and self.door(session.path_start):
            if session.path_now is not None and self.door(session.path_now) is False:
                return "left"
            if session.alone or now - session.created < self.leaving_watch_seconds:
                return "watch"
            return "greet"  # they stayed at the door, and someone else was just there
        if now < self._return_until:
            return "returned"
        return "greet"

    def _stepped_out(self, event: PersonEvent, session: Session | None) -> bool:
        """Did the person whose event just ended come out of the door and go back in,
        never greeted? (Their track started and ended in the door zone.)"""
        if session is None or session.handled or session.in_progress or self.door is None:
            return False
        if session.trigger != "person" or session.path_start is None:
            return False
        last = event.path[-1] if event.path else session.path_now
        if not self.door(session.path_start) or last is None or not self.door(last):
            return False
        session.handled = True
        return True

    def _in_trigger_zone(self, zones: frozenset[str]) -> bool:
        return not self.trigger_zones or bool(zones & self.trigger_zones)

    def _why_not_started(self, session: Session, now: float) -> str | None:
        """Why a person in a trigger zone was dropped without a visit (their event ended
        or expired), for the daily report's arrivals not greeted. None if they weren't
        due one: already handled, not a person, or never in a trigger zone."""
        if session.handled or session.in_progress or session.trigger != "person":
            return None
        if not self._in_trigger_zone(session.zones):
            return None
        at_door = (
            self.door is not None
            and session.path_start is not None
            and self.door(session.path_start)
        )
        if at_door and now - session.created < self.leaving_watch_seconds:
            return (
                f"appeared at the door {now - session.created:.1f} s before, "
                "still watching whether they walk out"
            )
        in_zone = now - session.first_seen
        if in_zone < self.dwell_seconds:
            return f"{in_zone:.1f} s in the trigger zone, under the {self.dwell_seconds:g} s dwell"
        return f"{in_zone:.1f} s in the trigger zone, not started"

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
                if merge_into == self._active_visit:
                    self._active_pressed = True
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
            dropped = [(eid, self._why_not_started(self.sessions.pop(eid), now)) for eid in expired]
            pending = list(self.sessions)

        for eid, why in dropped:
            if why:
                log.info(
                    "person event %s ended before a visit: no news from Frigate for %.0f s; %s",
                    eid,
                    self.ttl_seconds,
                    why,
                )

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
