"""Per-Frigate-event session tracking and dwell gating.

A session is created for each Frigate person event on the configured camera.
Once the person has been seen for ``dwell_seconds`` the visit is started on a
worker; each session triggers at most one visit.

Only one visit runs per camera. A session that becomes due while a visit is
running, or within ``cooldown_seconds`` after one ended, is merged into that
visit instead of greeting again (a person stepping out of frame and back, or
Frigate splitting one person's track into several events).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
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


def spawn_thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class SessionTracker:
    def __init__(
        self,
        *,
        camera: str,
        dwell_seconds: float,
        ttl_seconds: float,
        run_visit: Callable[[str], None],
        cooldown_seconds: float = 0.0,
        spawn: Callable[[Callable[[], None]], None] = spawn_thread,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.camera = camera
        self.dwell_seconds = dwell_seconds
        self.ttl_seconds = ttl_seconds
        self.cooldown_seconds = cooldown_seconds
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
                    self.sessions[event.id] = Session(first_seen=now, last_seen=now)
                else:
                    session.last_seen = now
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

        def worker() -> None:
            try:
                self._run_visit(event_id)
            finally:
                self._finish(event_id)

        self._spawn(worker)
        return True

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

    def sweep_forever(self, interval: float) -> None:
        while True:
            try:
                self.sweep()
            except Exception:
                log.exception("session sweep error")
            time.sleep(interval)
