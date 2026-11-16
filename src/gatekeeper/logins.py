"""Logging in to the web page: a login page and a cookie, rather than the browser's own
password box (HTTP Basic), which some browsers never show (DuckDuckGo's, for one) and
which can't be logged out of.

Each login gets a random token, the browser's cookie. Only its SHA-256 is kept, in
``web-logins.json`` next to the visit log (mode 600), so a restart doesn't log anyone
out; a login lasts ``DAYS`` from its last use.

Wrong passwords are limited (``Guard``): a few from one address, or more from every
address together, and passwords are refused for a while.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

COOKIE = "gatekeeper_login"
DAYS = 30
RENEW_SECONDS = 86400.0  # a login's expiry moves on at most once a day (fewer writes)
SAFE_NEXT = re.compile(r"^(?:[\w-]+/)*(?:[\w.-]+)?(?:\?[\w=&%.-]*)?$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Logins:
    """The logins open, by their token's hash: when each ends."""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._ends: dict[str, float] = {}
        if path is not None:
            try:
                self._ends = {
                    k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()
                }
            except FileNotFoundError:
                pass
            except (ValueError, AttributeError, TypeError) as e:
                log.warning("couldn't read %s (%s); everyone logs in again", path.name, e)

    def start(self) -> str:
        """A new login: its token, for the browser's cookie."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._ends = {k: end for k, end in self._ends.items() if end > now}
            self._ends[_hash(token)] = now + DAYS * 86400
            self._save()
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        key = _hash(token)
        with self._lock:
            now = self._clock()
            end = self._ends.get(key, 0.0)
            if end <= now:
                return False
            if end - now < DAYS * 86400 - RENEW_SECONDS:
                self._ends[key] = now + DAYS * 86400
                self._save()
            return True

    def end(self, token: str) -> None:
        with self._lock:
            if self._ends.pop(_hash(token), None) is not None:
                self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ends), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)


class Guard:
    """Wrong passwords, to stop guessing (the login page's and HTTP Basic's alike). Past
    ``PER_ADDRESS`` wrong ones from an address in ``WINDOW`` seconds, its passwords are
    refused, the right one too, until the oldest is ``WINDOW`` old; past ``IN_ALL`` from
    every address together, everyone's are, since behind Tailscale (and later the relay)
    every phone arrives from the same address. Anyone logged in stays logged in: the
    cookie isn't a password. Kept in memory: a restart forgets."""

    WINDOW = 15 * 60.0
    PER_ADDRESS = 5
    IN_ALL = 20

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._wrong: list[tuple[float, str]] = []  # (when, address), oldest first
        self._lock = threading.Lock()

    def _recent(self, now: float) -> list[tuple[float, str]]:
        self._wrong = [w for w in self._wrong if w[0] > now - self.WINDOW]
        return self._wrong

    def wait(self, address: str) -> float:
        """Seconds until a password from ``address`` is checked again; 0: now."""
        with self._lock:
            now = self._clock()
            wrong = self._recent(now)
            mine = [t for t, a in wrong if a == address]
            if len(mine) >= self.PER_ADDRESS:
                return mine[-self.PER_ADDRESS] + self.WINDOW - now
            if len(wrong) >= self.IN_ALL:
                return wrong[-self.IN_ALL][0] + self.WINDOW - now
            return 0.0

    def wrong(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            wrong = self._recent(now)
            wrong.append((now, address))
            mine = sum(1 for _, a in wrong if a == address)
        log.info("web: a wrong password from %s (%d in 15 min)", address, mine)
        if mine == self.PER_ADDRESS:
            log.warning(
                "web: %d wrong passwords from %s; its passwords are refused for 15 min",
                mine,
                address,
            )
        if len(wrong) == self.IN_ALL:
            log.warning(
                "web: %d wrong passwords in 15 min; every password is refused for a while "
                "(logins already open still work)",
                len(wrong),
            )

    def right(self, address: str) -> None:
        """The right password: that address's wrong ones are forgotten."""
        with self._lock:
            self._wrong = [w for w in self._wrong if w[1] != address]


def safe_next(target: str) -> str:
    """Where to go after logging in: a page of this site (relative, no ``..``), or the
    Visits page."""
    if not target or ".." in target or "//" in target or not SAFE_NEXT.match(target):
        return "./"
    return target


def page(error: str = "", target: str = "") -> str:
    from gatekeeper.web import STYLE, app_head

    e = html.escape
    return "\n".join(
        [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Gatekeeper</title><style>{STYLE}</style>{app_head('login')}</head><body>",
            "<h1>Gatekeeper</h1>",
            *([f"<p class=error role=alert>{e(error)}</p>"] if error else []),
            '<form method=post action="login">',
            f'<input type=hidden name=next value="{e(safe_next(target))}">',
            # A user name for password managers to save the password under.
            '<input type=hidden name=username value="gatekeeper" autocomplete=username>',
            "<p><label>Password<br>",
            "<input type=password name=password autocomplete=current-password required "
            "autofocus></label></p>",
            "<p><button type=submit>Log in</button></p>",
            "</form></body></html>",
        ]
    )
