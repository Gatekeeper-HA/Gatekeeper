"""What the name service does, apart from HTTP.

Every request after registration is signed with the hub's Ed25519 key:
``Authorization: Hub <unix time>.<signature>``, the signature (base64url) over
``"<METHOD> <path> <unix time> <sha256 of the body, hex>"``. Registration is signed the same
way with the key it registers, which proves the hub holds it. A signed request is good for
five minutes either side of the service's clock; each one sets values outright, so replaying
it within that window changes nothing.

With Cloud Plus (store.py), a hub also gets passes for the relay (``Relay``):

- **a tunnel pass** for SniTun, the relay: a Fernet token only the relay can read, naming the
  hub's own name and holding a new AES key, good for ``PASS_SECONDS``. The hub asks for a new
  one every time it connects: the cipher's nonces start again with each connection, so a key
  is never used twice. The relay closes a tunnel once its pass has run out.
- **TURN credentials** for Live (coturn's REST format), good for ``TURN_SECONDS``. The secret
  stays on our servers, so only we can make them.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, MultiFernet
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
PASS_SECONDS = 3600
TURN_SECONDS = 4 * 3600  # a Live view that's still open needs its TURN allocation refreshed
# SniTun's cipher and protocol: AES-GCM-SIV (authenticated, and safe even if a nonce came
# round twice), and protocol 2 (the phone's address encrypted inside the tunnel, IPv6 too).
CIPHER = "aes-gcm-siv"
PROTOCOL = 2
TURN_PORT = 3478


@dataclass(frozen=True)
class Relay:
    """The relay this service gives passes for: its name, the Fernet keys it shares with
    SniTun (the first makes new passes; more let a key be changed without cutting tunnels),
    and coturn's secret."""

    name: str
    keys: tuple[str, ...]
    turn_secret: str
    port: int = 443
    # Its addresses, for the names of hubs whose tunnel is up (``route``).
    ipv4: str = ""
    ipv6: str = ""


def zone_records(
    zone: str, ipv4: str, ipv6: str = "", relay_ipv4: str = "", relay_ipv6: str = ""
) -> list[tuple[str, str, int, list[str]]]:
    """The zone's own records. Its nameservers and the API are this server, ``relay`` the
    relay's (when it has one). Only Let's Encrypt may issue certificates in it (a hub's own
    CAA record narrows that to its account), and it sends no email, so nobody can send any in
    its name."""
    ipaddress.IPv4Address(ipv4)
    if relay_ipv4:
        ipaddress.IPv4Address(relay_ipv4)
    for address in (ipv6, relay_ipv6):
        if address:
            ipaddress.IPv6Address(address)
    z = zone.rstrip(".") + "."
    records = [
        ("@", "SOA", 3600, [f"ns1.{z} hostmaster.{z} 1 10800 3600 604800 60"]),
        ("@", "NS", 3600, [f"ns1.{z}", f"ns2.{z}"]),
        ("@", "CAA", 3600, ['0 issue "letsencrypt.org"', '0 issuewild ";"']),
        ("@", "MX", 3600, ["0 ."]),
        ("@", "TXT", 3600, ['"v=spf1 -all"']),
        ("_dmarc", "TXT", 3600, ['"v=DMARC1; p=reject"']),
    ]
    for name in ("ns1", "ns2", "api"):
        records.append((name, "A", 3600, [ipv4]))
        if ipv6:
            records.append((name, "AAAA", 3600, [ipv6]))
    if relay_ipv4:
        records.append(("relay", "A", 3600, [relay_ipv4]))
    if relay_ipv6:
        records.append(("relay", "AAAA", 3600, [relay_ipv6]))
    return records


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
    def __init__(
        self,
        store: Store,
        dns: PowerDNS,
        clock: Callable[[], float] = time.time,
        relay: Relay | None = None,
    ) -> None:
        self.store = store
        self.dns = dns
        self.zone = dns.zone.rstrip(".")
        self.relay = relay
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
        self.store.set_route(hub_id, home=str(ip))
        if not self.store.route(hub_id)[1]:  # while relayed, the name stays the relay's
            self.dns.change([(hub_id, "A", TTL, [str(ip)])])

    def route(self, hub_id: str, via: str) -> None:
        """Point the hub's name at the relay while its tunnel is up (``via`` "relay",
        Cloud Plus only), or back at its home address ("home"). The hub says which."""
        if via == "relay":
            relay = self._plus(hub_id)
            if not relay.ipv4:
                raise LookupError("this service has no relay")
            self.dns.change(
                [
                    (hub_id, "A", TTL, [relay.ipv4]),
                    (hub_id, "AAAA", TTL, [relay.ipv6] if relay.ipv6 else []),
                ]
            )
            self.store.set_route(hub_id, relayed=True)
        elif via == "home":
            self._home(hub_id)
        else:
            raise ValueError('via: "relay" or "home"')

    def _home(self, hub_id: str) -> None:
        home, _ = self.store.route(hub_id)
        self.dns.change([(hub_id, "A", TTL, [home] if home else []), (hub_id, "AAAA", TTL, [])])
        self.store.set_route(hub_id, relayed=False)

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

    def _plus(self, hub_id: str) -> Relay:
        """The relay, if this hub has Cloud Plus now; else LookupError (no relay here) or
        Refused."""
        if self.relay is None:
            raise LookupError("this service has no relay")
        until = self.store.plus_until(hub_id)
        if until is None or until <= self._clock():
            raise Refused("this hub has no Cloud Plus")
        return self.relay

    def tunnel(self, hub_id: str) -> dict:
        """A pass for the relay (SniTun): a token only it can read, for the hub's own name,
        and the new AES key the hub proves it holds. Asked for each time the hub connects."""
        relay = self._plus(hub_id)
        now = self._clock()
        until = int(now + PASS_SECONDS)
        aes_key, aes_iv = secrets.token_bytes(32), secrets.token_bytes(16)
        content = {
            "valid": until,
            "hostname": f"{hub_id}.{self.zone}",
            "aes_key": aes_key.hex(),
            "aes_iv": aes_iv.hex(),
            "protocol_version": PROTOCOL,
            "cipher": CIPHER,
        }
        fernet = MultiFernet([Fernet(k) for k in relay.keys])
        token = fernet.encrypt_at_time(json.dumps(content).encode(), int(now))
        return {
            "relay": relay.name,
            "port": relay.port,
            "token": token.decode(),
            "aes_key": aes_key.hex(),
            "aes_iv": aes_iv.hex(),
            "protocol_version": PROTOCOL,
            "cipher": CIPHER,
            "until": until,
        }

    def turn(self, hub_id: str) -> dict:
        """TURN credentials for the hub's Live page, in coturn's REST format: the user
        name is ``<until>:<hub>``, the password its HMAC-SHA1 with the secret."""
        relay = self._plus(hub_id)
        until = int(self._clock() + TURN_SECONDS)
        username = f"{until}:{hub_id}"
        digest = hmac.new(relay.turn_secret.encode(), username.encode(), hashlib.sha1).digest()
        where = f"{relay.name}:{TURN_PORT}"
        return {
            "urls": [f"stun:{where}", f"turn:{where}?transport=udp", f"turn:{where}?transport=tcp"],
            "username": username,
            "credential": base64.b64encode(digest).decode(),
            "until": until,
        }

    def remove(self, hub_id: str) -> None:
        self.dns.change(
            [
                (hub_id, "A", TTL, []),
                (hub_id, "AAAA", TTL, []),
                (hub_id, "CAA", CAA_TTL, []),
                (f"_acme-challenge.{hub_id}", "TXT", TTL, []),
            ]
        )
        self.store.remove(hub_id)
        with self._writes_lock:
            self._writes.pop(hub_id, None)
        log.info("hub %s removed", hub_id)

    def sweep(self) -> None:
        """Remove the challenge records left longer than CHALLENGE_SECONDS, and send home
        the names of hubs whose Cloud Plus has ended (a hub that's off can't say so)."""
        for hub_id in self.store.challenges_due():
            try:
                self.dns.change([(f"_acme-challenge.{hub_id}", "TXT", TTL, [])])
            except OSError as e:
                log.warning("couldn't remove hub %s's challenge: %s", hub_id, e)
                continue
            self.store.challenge_until(hub_id, None)
        now = self._clock()
        for hub_id in self.store.relayed_hubs():
            if (self.store.plus_until(hub_id) or 0) > now and self.relay is not None:
                continue
            try:
                self._home(hub_id)
            except OSError as e:
                log.warning("couldn't send hub %s's name home: %s", hub_id, e)
                continue
            log.info("hub %s: Cloud Plus has ended; its name points home again", hub_id)
