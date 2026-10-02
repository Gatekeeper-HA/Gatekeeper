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
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from gatekeeper.frigate import FrigateEvent

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
        spawn: Callable[[Callable[[], None]], None] = spawn_thread,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.camera = camera
        self.dwell_seconds = dwell_seconds
        self.ttl_seconds = ttl_seconds
        self.cooldown_seconds = cooldown_seconds
        self.trigger_zones = frozenset(trigger_zones)
        self._run_visit = run_visit
        self._spawn = spawn
        self._clock = clock
        self._lock = threading.Lock()
        self.sessions: dict[str, Session] = {}
        self._active_visit: str | None = None
        self._last_visit: str | None = None
        self._cooldown_until = 0.0

    def handle_event(self, event: FrigateEvent) -> None:
        if event.camera != self.camera or event.label != "person":
            return

        if event.type in {"new", "update"}:
            now = self._clock()
            with self._lock:
                session = self.sessions.get(event.id)
                if session is None:
                    session = self.sessions[event.id] = Session(first_seen=now, last_seen=now)
                else:
                    session.last_seen = now
                zones = session.zones | frozenset(event.entered_zones)
                if self._in_trigger_zone(zones) and not self._in_trigger_zone(session.zones):
                    session.first_seen = now  # dwell counts from entering the zone
                session.zones = zones
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
            session.in_progress = True
            self._active_visit = event_id

        trigger = session.trigger

        def worker() -> None:
            try:
                self._run_visit(event_id, trigger)
            finally:
                self._finish(event_id)

        self._spawn(worker)
        return True

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
