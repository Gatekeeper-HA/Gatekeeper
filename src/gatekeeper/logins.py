"""Logging in to the web page: a login page and a cookie, rather than the browser's own
password box (HTTP Basic), which some browsers never show (DuckDuckGo's, for one) and
which can't be logged out of.

Each login gets a random token, the browser's cookie. Only its SHA-256 is kept, in
``web-logins.json`` next to the visit log (mode 600), so a restart doesn't log anyone
out; a login lasts ``DAYS`` from its last use.
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


def safe_next(target: str) -> str:
    """Where to go after logging in: a page of this site (relative, no ``..``), or the
    Visits page."""
    if not target or ".." in target or "//" in target or not SAFE_NEXT.match(target):
        return "./"
    return target


def page(error: str = "", target: str = "") -> str:
    from gatekeeper.web import STYLE

    e = html.escape
    return "\n".join(
        [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            f"<title>Gatekeeper</title><style>{STYLE}</style></head><body>",
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
