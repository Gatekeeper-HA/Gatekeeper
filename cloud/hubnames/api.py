"""The name service's HTTP API. It runs behind a proxy that gives it HTTPS.

    GET    /v1/health
    POST   /v1/hubs                    {"claim": ..., "key": <Ed25519 public key, base64url>}
    PUT    /v1/hubs/<id>/address       {"address": "192.168.1.20"}
    PUT    /v1/hubs/<id>/challenge     {"values": [<DNS-01 TXT value>, ...]}
    PUT    /v1/hubs/<id>/caa           {"account": <its Let's Encrypt account URL>}
    DELETE /v1/hubs/<id>

Every request but the health check is signed (service.py). Errors are ``{"error": ...}``.

An address whose requests were refused or malformed ``Strikes.LIMIT`` times in an hour gets
429 until the oldest is an hour old. Behind Caddy, the address is the one it puts last in
``X-Forwarded-For``: the API itself isn't published, so only Caddy reaches it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hubnames.service import NameService, TooMany, unb64
from hubnames.store import Refused

log = logging.getLogger(__name__)

MAX_BODY = 4096
HUB_PATH = re.compile(r"^/v1/hubs/([a-z2-7]{10})(?:/(address|challenge|caa))?$")


class Strikes:
    """Requests refused or malformed (4xx), per address, over the last hour."""

    LIMIT = 20
    WINDOW = 3600.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._seen: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def wait(self, address: str) -> float:
        """Seconds until ``address`` is answered again; 0: now."""
        with self._lock:
            now = self._clock()
            times = [t for t in self._seen.get(address, []) if t > now - self.WINDOW]
            if len(times) < self.LIMIT:
                return 0.0
            return times[-self.LIMIT] + self.WINDOW - now

    def add(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._seen) > 10_000:  # forget addresses quiet for an hour
                self._seen = {
                    a: ts for a, ts in self._seen.items() if ts and ts[-1] > now - self.WINDOW
                }
            times = [t for t in self._seen.get(address, []) if t > now - self.WINDOW]
            times.append(now)
            self._seen[address] = times
        if len(times) == self.LIMIT:
            log.warning(
                "%d refused requests from %s in an hour; it gets 429 for now", self.LIMIT, address
            )


def serve(
    service: NameService,
    host: str = "0.0.0.0",
    port: int = 8080,
    strikes: Strikes | None = None,
) -> ThreadingHTTPServer:
    """Serve the API on ``port`` in a daemon thread."""
    strikes = strikes or Strikes()

    class Handler(BaseHTTPRequestHandler):
        def parse_request(self):
            """Each request's headers: refused (429) if its address struck out."""
            if not super().parse_request():
                return False
            wait = strikes.wait(self._address())
            if wait > 0:
                self._send(
                    429,
                    {"error": "too many refused requests from this address; try again later"},
                    {"Retry-After": str(int(wait) + 1)},
                )
                return False
            return True

        def _address(self) -> str:
            forwarded = self.headers.get("X-Forwarded-For", "")
            return forwarded.split(",")[-1].strip() or self.client_address[0]

        def do_GET(self):
            if self.path == "/v1/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/hubs":
                return self._send(404, {"error": "not found"})
            self._handle(lambda body, data: self._register(body, data), created=True)

        def do_PUT(self):
            match = HUB_PATH.match(self.path)
            if not match or not match.group(2):
                return self._send(404, {"error": "not found"})
            hub_id, what = match.groups()

            def put(body, data):
                service.check(hub_id, self._auth(), "PUT", self.path, body)
                if what == "address":
                    service.address(hub_id, data.get("address"))
                elif what == "challenge":
                    values = data.get("values")
                    if not isinstance(values, list):
                        raise ValueError("values: a list")
                    service.challenge(hub_id, values)
                else:
                    service.caa(hub_id, data.get("account"))

            self._handle(put)

        def do_DELETE(self):
            match = HUB_PATH.match(self.path)
            if not match or match.group(2):
                return self._send(404, {"error": "not found"})
            hub_id = match.group(1)

            def delete(body, data):
                service.check(hub_id, self._auth(), "DELETE", self.path, body)
                service.remove(hub_id)

            self._handle(delete)

        def _register(self, body, data):
            claim, key = data.get("claim"), data.get("key")
            if not isinstance(claim, str) or not isinstance(key, str):
                raise ValueError("claim and key: strings")
            try:
                raw = unb64(key)
            except ValueError:
                raise ValueError("key: an Ed25519 public key, base64url") from None
            return service.register(claim, raw, self._auth(), self.path, body)

        def _auth(self) -> str:
            return self.headers.get("Authorization", "")

        def _handle(self, action, created=False):
            try:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    raise ValueError(f"the body: at most {MAX_BODY} bytes")
                body = self.rfile.read(length)
                try:
                    data = json.loads(body or b"{}")
                except ValueError:  # its own wording stays here
                    data = None
                if not isinstance(data, dict):
                    raise ValueError("the body: a JSON object")
                answer = action(body, data)
            except Refused as e:
                return self._send(403, {"error": str(e)})
            except LookupError as e:
                return self._send(404, {"error": str(e)})
            except TooMany as e:
                return self._send(429, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except OSError as e:
                log.warning("PowerDNS: %s", e)
                return self._send(502, {"error": "the DNS server couldn't be reached"})
            if answer is None:
                self._send(204)
            else:
                self._send(201 if created else 200, answer)

        def _send(self, status, answer=None, headers=None):
            if 400 <= status < 500 and status != 429:
                strikes.add(self._address())
            body = json.dumps(answer).encode() if answer is not None else b""
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            log.debug("%s " + format, self.address_string(), *args)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="api", daemon=True).start()
    return server
