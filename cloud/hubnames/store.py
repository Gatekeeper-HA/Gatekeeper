"""The hubs and the claim codes, in SQLite.

A claim code lets one hub register. We issue them (printed with a Hub, or asked for by a
do-it-yourself user), so the zone can't be used as a free name service. Only each code's
SHA-256 is kept.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# The zone's own names, never a hub's.
RESERVED = frozenset({"api", "relay", "ns1", "ns2", "www", "mail"})


class Refused(Exception):
    """The request can't be allowed: an unknown or used claim code, a bad signature."""


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").replace(" ", "").upper().encode()).hexdigest()


def new_id() -> str:
    """10 random base32 characters (50 bits): a DNS label that says nothing about the home."""
    return base64.b32encode(secrets.token_bytes(7)).decode().lower()[:10]


@dataclass(frozen=True)
class Hub:
    id: str
    key: bytes  # its Ed25519 public key, raw
    created: float


class Store:
    def __init__(self, path: Path | str, clock: Callable[[], float] = time.time) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._clock = clock
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS hubs (
                id TEXT PRIMARY KEY, key BLOB UNIQUE NOT NULL,
                created REAL NOT NULL, seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS claims (
                hash TEXT PRIMARY KEY, created REAL NOT NULL, hub TEXT);
            CREATE TABLE IF NOT EXISTS challenges (
                hub TEXT PRIMARY KEY, until REAL NOT NULL);
            """
        )

    def new_claims(self, count: int) -> list[str]:
        """``count`` new claim codes, like ``ABCD-EFGH-JKLM``. Only their hashes are kept."""
        codes = []
        with self._lock:
            for _ in range(count):
                raw = base64.b32encode(secrets.token_bytes(8)).decode()[:12]
                code = "-".join(raw[i : i + 4] for i in range(0, 12, 4))
                self._db.execute(
                    "INSERT INTO claims (hash, created) VALUES (?, ?)",
                    (_code_hash(code), self._clock()),
                )
                codes.append(code)
        return codes

    def register(self, claim: str, key: bytes) -> Hub:
        """The hub for ``key``, registered with ``claim``. Registering again with the same
        code and key gives the same hub, so a hub that lost the answer can ask again."""
        with self._lock:
            row = self._db.execute(
                "SELECT hub FROM claims WHERE hash = ?", (_code_hash(claim),)
            ).fetchone()
            if row is None:
                raise Refused("unknown claim code")
            if row[0] is not None:
                hub = self._hub(row[0])
                if hub is not None and hub.key == key:
                    return hub
                raise Refused("claim code already used")
            if self._db.execute("SELECT 1 FROM hubs WHERE key = ?", (key,)).fetchone():
                raise Refused("key already registered")
            hub_id = new_id()
            while hub_id in RESERVED or self._hub(hub_id) is not None:
                hub_id = new_id()
            now = self._clock()
            self._db.execute("BEGIN")
            try:
                self._db.execute(
                    "INSERT INTO hubs (id, key, created, seen) VALUES (?, ?, ?, ?)",
                    (hub_id, key, now, now),
                )
                self._db.execute(
                    "UPDATE claims SET hub = ? WHERE hash = ?", (hub_id, _code_hash(claim))
                )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            return Hub(hub_id, key, now)

    def hub(self, hub_id: str) -> Hub | None:
        with self._lock:
            return self._hub(hub_id)

    def _hub(self, hub_id: str) -> Hub | None:
        row = self._db.execute(
            "SELECT id, key, created FROM hubs WHERE id = ?", (hub_id,)
        ).fetchone()
        return Hub(row[0], bytes(row[1]), row[2]) if row else None

    def seen(self, hub_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE hubs SET seen = ? WHERE id = ?", (self._clock(), hub_id))

    def remove(self, hub_id: str) -> None:
        """Forget the hub. Its claim code stays used."""
        with self._lock:
            self._db.execute("DELETE FROM hubs WHERE id = ?", (hub_id,))
            self._db.execute("DELETE FROM challenges WHERE hub = ?", (hub_id,))

    def challenge_until(self, hub_id: str, until: float | None) -> None:
        """When the hub's challenge record is to be removed (None: it's gone)."""
        with self._lock:
            if until is None:
                self._db.execute("DELETE FROM challenges WHERE hub = ?", (hub_id,))
            else:
                self._db.execute(
                    "INSERT OR REPLACE INTO challenges (hub, until) VALUES (?, ?)",
                    (hub_id, until),
                )

    def challenges_due(self) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT hub FROM challenges WHERE until <= ?", (self._clock(),)
            ).fetchall()
        return [r[0] for r in rows]
