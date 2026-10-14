"""Liveness: a /healthz endpoint, a probe for Docker's HEALTHCHECK, and a
watchdog that exits the process on an internal hang.

Healthy means: connected to MQTT, the session sweep loop is running, and no
visit has been running far longer than its timeout. Docker (compose) doesn't
restart unhealthy containers, so on an internal hang (sweep stalled or a visit
stuck) that lasts ``hang_exit_seconds`` the watchdog exits and
``restart: unless-stopped`` brings Gatekeeper back. An MQTT outage only
reports unhealthy: paho reconnects by itself.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

DEFAULT_PORT = 8099


class Health:
    def __init__(
        self,
        *,
        sweep_max_age: float,
        visit_max_age: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sweep_max_age = sweep_max_age
        self.visit_max_age = visit_max_age
        self._clock = clock
        self._lock = threading.Lock()
        self.mqtt_connected = False
        self._last_sweep: float | None = None
        self._visit_started: float | None = None
        # Set when the audio tap runs: is it receiving audio?
        self.tap_ok: Callable[[], bool] | None = None

    def sweep_done(self) -> None:
        self._last_sweep = self._clock()

    def visit_started(self) -> None:
        with self._lock:
            self._visit_started = self._clock()

    def visit_finished(self) -> None:
        with self._lock:
            self._visit_started = None

    def _ages(self) -> tuple[float | None, float | None]:
        now = self._clock()
        sweep_age = None if self._last_sweep is None else now - self._last_sweep
        visit_age = None if self._visit_started is None else now - self._visit_started
        return sweep_age, visit_age

    def _hangs(self, sweep_age: float | None, visit_age: float | None) -> list[str]:
        problems = []
        if sweep_age is None or sweep_age > self.sweep_max_age:
            problems.append("session sweep stalled")
        if visit_age is not None and visit_age > self.visit_max_age:
            problems.append("visit stuck")
        return problems

    def internal_hang(self) -> bool:
        return bool(self._hangs(*self._ages()))

    def check(self) -> tuple[bool, dict]:
        """Return ``(healthy, details)``."""
        sweep_age, visit_age = self._ages()
        problems = self._hangs(sweep_age, visit_age)
        if not self.mqtt_connected:
            problems.append("mqtt disconnected")
        details = {
            "status": "unhealthy" if problems else "ok",
            "problems": problems,
            "mqtt_connected": self.mqtt_connected,
            "sweep_age_seconds": None if sweep_age is None else round(sweep_age, 1),
            "visit_age_seconds": None if visit_age is None else round(visit_age, 1),
            # Informational: without the tap, visits record a fixed window instead.
            "audio_tap": "off" if self.tap_ok is None else ("ok" if self.tap_ok() else "down"),
        }
        return not problems, details

    def watchdog(
        self,
        hang_exit_seconds: float,
        interval: float = 10.0,
        exit_fn: Callable[[int], None] = os._exit,
    ) -> None:
        """Exit the process once an internal hang has lasted ``hang_exit_seconds``."""
        hung_since = None
        while True:
            if self.internal_hang():
                now = self._clock()
                hung_since = hung_since if hung_since is not None else now
                if now - hung_since >= hang_exit_seconds:
                    log.critical("internal hang for %.0f s: %s; exiting", now - hung_since,
                                 self.check()[1]["problems"])
                    exit_fn(1)
                    return
            else:
                hung_since = None
            time.sleep(interval)


def serve(health: Health, port: int) -> ThreadingHTTPServer:
    """Serve GET /healthz on ``port`` in a daemon thread."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.split("?")[0] != "/healthz":
                self.send_error(404)
                return
            healthy, details = health.check()
            body = json.dumps(details).encode()
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            pass  # probes every 30 s; don't log them

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("health endpoint on :%d/healthz", server.server_address[1])
    return server


def probe(port: int | None = None, timeout: float = 4.0) -> int:
    """Exit code for Docker's HEALTHCHECK: 0 if /healthz answers 200."""
    port = port or int(os.environ.get("HEALTH_PORT") or DEFAULT_PORT)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout):
            return 0
    except (urllib.error.URLError, OSError) as e:
        print(f"unhealthy: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(probe())
