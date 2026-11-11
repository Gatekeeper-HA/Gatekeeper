"""What the name service does, apart from HTTP.

Every request after registration is signed with the hub's Ed25519 key:
``Authorization: Hub <unix time>.<signature>``, the signature (base64url) over
``"<METHOD> <path> <unix time> <sha256 of the body, hex>"``. Registration is signed the same
way with the key it registers, which proves the hub holds it. A signed request is good for
five minutes either side of the service's clock; each one sets values outright, so replaying
it within that window changes nothing.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import ipaddress
import logging
import re
import threading
import time
from collections.abc import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from hubnames.powerdns import PowerDNS
from hubnames.store import Refused, Store

log = logging.getLogger(__name__)

CLOCK_SKEW = 300.0
CHALLENGE_SECONDS = 600.0  # a challenge's TXT record is removed after this
WRITES_PER_HOUR = 60
TTL = 60  # seconds; short, so a name can move between the home and the relay
CAA_TTL = 300
# The ACME accounts a hub's CAA record may name: Let's Encrypt's, and its staging server's.
ACCOUNT = re.compile(r"^https://acme(-staging)?-v02\.api\.letsencrypt\.org/acme/acct/\d{1,20}$")
CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # base64url of a SHA-256 (RFC 8555, 8.4)
HOME_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class TooMany(Exception):
    """The hub asked too often."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def message(method: str, path: str, when: int, body: bytes) -> bytes:
    return f"{method} {path} {when} {hashlib.sha256(body).hexdigest()}".encode()


def verify(key: bytes, header: str, method: str, path: str, body: bytes, now: float) -> None:
    """Raises Refused unless ``header`` is the hub's signature of this request, made now."""
    try:
        scheme, value = header.split(" ", 1)
        when, signature = value.split(".", 1)
        when_s = int(when)
        if scheme != "Hub":
            raise ValueError(scheme)
        sig = unb64(signature)
    except ValueError:
        raise Refused("no signature (Authorization: Hub <time>.<signature>)") from None
    if abs(now - when_s) > CLOCK_SKEW:
        raise Refused("the request's time is off by more than 5 minutes; check the hub's clock")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(sig, message(method, path, when_s, body))
    except (InvalidSignature, ValueError):
        raise Refused("bad signature") from None


class NameService:
    def __init__(self, store: Store, dns: PowerDNS, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.dns = dns
        self.zone = dns.zone.rstrip(".")
        self._clock = clock
        self._writes: dict[str, collections.deque] = {}
        self._writes_lock = threading.Lock()

    def register(self, claim: str, key: bytes, header: str, path: str, body: bytes) -> dict:
        if len(key) != 32:
            raise ValueError("key: an Ed25519 public key, 32 bytes, base64url")
        verify(key, header, "POST", path, body, self._clock())
        hub = self.store.register(claim, key)
        log.info("hub %s registered", hub.id)
        return {"id": hub.id, "name": f"{hub.id}.{self.zone}"}

    def check(self, hub_id: str, header: str, method: str, path: str, body: bytes) -> None:
        """Raises LookupError for an unknown hub, Refused for a bad signature, TooMany past
        WRITES_PER_HOUR."""
        hub = self.store.hub(hub_id)
        if hub is None:
            raise LookupError(f"no hub {hub_id}")
        now = self._clock()
        verify(hub.key, header, method, path, body, now)
        with self._writes_lock:
            writes = self._writes.setdefault(hub_id, collections.deque())
            while writes and writes[0] < now - 3600:
                writes.popleft()
            if len(writes) >= WRITES_PER_HOUR:
                raise TooMany(f"more than {WRITES_PER_HOUR} changes in an hour")
            writes.append(now)
        self.store.seen(hub_id)

    def address(self, hub_id: str, address: str) -> None:
        """Point the hub's name at its address on the home network. Only private IPv4
        addresses, so a name can't front a public site."""
        try:
            if not isinstance(address, str):
                raise ValueError(address)
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            raise ValueError("address: an IPv4 address") from None
        if not any(ip in n for n in HOME_NETWORKS):
            raise ValueError("address: only a home network's (10.x, 172.16-31.x, 192.168.x)")
        self.dns.change([(hub_id, "A", TTL, [str(ip)])])

    def challenge(self, hub_id: str, values: list[str]) -> None:
        """Publish the DNS-01 challenge's TXT values (at most 2); none removes them. They're
        removed after CHALLENGE_SECONDS anyway (``sweep``)."""
        if len(values) > 2 or not all(isinstance(v, str) and CHALLENGE.match(v) for v in values):
            raise ValueError("values: at most 2 DNS-01 challenge values (43 characters each)")
        self.dns.change([(f"_acme-challenge.{hub_id}", "TXT", TTL, [f'"{v}"' for v in values])])
        self.store.challenge_until(hub_id, self._clock() + CHALLENGE_SECONDS if values else None)

    def caa(self, hub_id: str, account: str) -> None:
        """Let only the hub's ACME account get certificates for its name (RFC 8657), by
        DNS-01 only, and no wildcard."""
        if not isinstance(account, str) or not ACCOUNT.match(account):
            raise ValueError("account: a Let's Encrypt account URL")
        issue = f'0 issue "letsencrypt.org; accounturi={account}; validationmethods=dns-01"'
        self.dns.change([(hub_id, "CAA", CAA_TTL, [issue, '0 issuewild ";"'])])

    def remove(self, hub_id: str) -> None:
        self.dns.change(
            [
                (hub_id, "A", TTL, []),
                (hub_id, "CAA", CAA_TTL, []),
                (f"_acme-challenge.{hub_id}", "TXT", TTL, []),
            ]
        )
        self.store.remove(hub_id)
        with self._writes_lock:
            self._writes.pop(hub_id, None)
        log.info("hub %s removed", hub_id)

    def sweep(self) -> None:
        """Remove the challenge records left longer than CHALLENGE_SECONDS."""
        for hub_id in self.store.challenges_due():
            try:
                self.dns.change([(f"_acme-challenge.{hub_id}", "TXT", TTL, [])])
            except OSError as e:
                log.warning("couldn't remove hub %s's challenge: %s", hub_id, e)
                continue
            self.store.challenge_until(hub_id, None)
