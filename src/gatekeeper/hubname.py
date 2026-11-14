"""The hub's own name, ``<id>.gatekeeperhome.net``, with a Let's Encrypt certificate.

With a claim code (``HUB_NAME_CLAIM``), Gatekeeper registers once with Gatekeeper's name
service (``cloud/hubnames``) and keeps its address on the home network published there.
It gets a certificate for the name from Let's Encrypt (ACME, RFC 8555) with a DNS-01
challenge that the service publishes, and renews it once two thirds of its life has
passed. Phones trust that certificate, so the app installs from ``https://<name>/`` with
no warning.

Everything secret is made here and stays here (``LOG_DIR/hub-name``, readable only by
Gatekeeper): the key the hub signs its requests to the service with (Ed25519), its Let's
Encrypt account's key and the certificate's key (ECDSA P-256). The name's CAA record names
this hub's Let's Encrypt account, so no one else can get a certificate for the name, not
even the name service.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from gatekeeper import __version__

log = logging.getLogger(__name__)

API = "https://api.gatekeeperhome.net"
LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
USER_AGENT = f"Gatekeeper/{__version__}"
TIMEOUT = 30.0
# Renew once this share of the certificate's life has passed (60 of Let's Encrypt's 90 days).
RENEW_AT = 2 / 3
# How often the address is published again, and the certificate checked.
CHECK_SECONDS = 12 * 3600
# After a failure: wait 1 h, then 2, 4, 8, then once a day. Let's Encrypt allows 5 failed
# validations an hour per name.
RETRY_SECONDS = (3600, 2 * 3600, 4 * 3600, 8 * 3600, 24 * 3600)
# After the challenge is published, before Let's Encrypt is asked to check it.
DNS_WAIT = 5.0
POLL_SECONDS = 2.0
POLL_LIMIT = 90.0
HOME_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def home_address(names: list[str]) -> str:
    """The first address in ``names`` on a home network (private IPv4), or ""."""
    for name in names:
        try:
            ip = ipaddress.ip_address(name.strip())
        except ValueError:
            continue
        if ip.version == 4 and any(ip in n for n in HOME_NETWORKS):
            return str(ip)
    return ""


class Stop(Exception):
    """A failure that trying again won't fix (a claim code refused, the hub unknown)."""


class Failed(Exception):
    """A request that failed but may work later (the service or Let's Encrypt down, busy,
    or refusing for now)."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _http(
    method: str, url: str, body: bytes | None = None, headers: dict | None = None
) -> tuple[int, dict, bytes]:
    request = urllib.request.Request(
        url, data=body, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as answer:
            return answer.status, dict(answer.headers), answer.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _retry_after(headers: dict) -> float:
    try:
        return float(headers.get("Retry-After", 0))
    except ValueError:
        return 0.0


class NameService:
    """Gatekeeper's name service (``cloud/hubnames``). Every request is signed with the
    hub's own key (Ed25519): ``Authorization: Hub <time>.<signature>``, over the method,
    path, time and the body's SHA-256."""

    def __init__(self, api: str, key, hub_id: str = "", clock=time.time) -> None:
        self.api = api.rstrip("/")
        self.key = key
        self.hub_id = hub_id
        self._clock = clock

    def _send(self, method: str, path: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        when = int(self._clock())
        message = f"{method} {path} {when} {hashlib.sha256(body).hexdigest()}".encode()
        headers = {
            "Authorization": f"Hub {when}.{b64(self.key.sign(message))}",
            "Content-Type": "application/json",
        }
        status, answer_headers, answer = _http(method, self.api + path, body, headers)
        if status < 300:
            return _json(answer)
        why = _json(answer).get("error") or f"HTTP {status}"
        if status in (403, 404):
            raise Stop(f"the name service refused: {why}")
        raise Failed(f"the name service answered {status}: {why}", _retry_after(answer_headers))

    def register(self, claim: str) -> dict:
        """``{"id", "name"}``: this hub's name, registered with the claim code ``claim``.
        Registering again with the same code and key gives the same name."""
        from cryptography.hazmat.primitives import serialization

        public = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        answer = self._send("POST", "/v1/hubs", {"claim": claim, "key": b64(public)})
        self.hub_id = answer["id"]
        return answer

    def address(self, address: str) -> None:
        self._send("PUT", f"/v1/hubs/{self.hub_id}/address", {"address": address})

    def challenge(self, values: list[str]) -> None:
        self._send("PUT", f"/v1/hubs/{self.hub_id}/challenge", {"values": values})

    def caa(self, account: str) -> None:
        self._send("PUT", f"/v1/hubs/{self.hub_id}/caa", {"account": account})


class Acme:
    """A small ACME client (RFC 8555): an account, and a certificate for one name by DNS-01.
    ``key``: the account's key (ECDSA P-256, ES256); ``account``: its URL once made."""

    def __init__(self, directory: str, key, account: str = "", sleep=time.sleep) -> None:
        self.directory_url = directory
        self.key = key
        self.account = account
        self._sleep = sleep
        self._directory: dict = {}
        self._nonce = ""

    def jwk(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {
            "crv": "P-256",
            "kty": "EC",
            "x": b64(numbers.x.to_bytes(32, "big")),
            "y": b64(numbers.y.to_bytes(32, "big")),
        }

    def thumbprint(self) -> str:
        """The account key's JWK thumbprint (RFC 7638), part of every key authorization."""
        canonical = json.dumps(self.jwk(), sort_keys=True, separators=(",", ":"))
        return b64(hashlib.sha256(canonical.encode()).digest())

    def dns_value(self, token: str) -> str:
        """The TXT value for a DNS-01 challenge's ``token`` (RFC 8555 §8.4)."""
        return b64(hashlib.sha256(f"{token}.{self.thumbprint()}".encode()).digest())

    def _url(self, what: str) -> str:
        if not self._directory:
            status, headers, body = _http("GET", self.directory_url)
            if status != 200:
                raise Failed(f"Let's Encrypt's directory answered {status}", _retry_after(headers))
            self._directory = _json(body)
        return self._directory[what]

    def _signed(self, url: str, payload: dict | None, nonce: str) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        protected: dict = {"alg": "ES256", "nonce": nonce, "url": url}
        if self.account:
            protected["kid"] = self.account
        else:
            protected["jwk"] = self.jwk()
        head = b64(json.dumps(protected).encode())
        body = "" if payload is None else b64(json.dumps(payload).encode())
        r, s = decode_dss_signature(
            self.key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        )
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return json.dumps(
            {"protected": head, "payload": body, "signature": b64(signature)}
        ).encode()

    def post(self, url: str, payload: dict | None, accept: str = "") -> tuple[dict, bytes]:
        """POST a signed request (``payload`` None: POST-as-GET); its headers and body. A
        rejected nonce is retried once with a fresh one."""
        for attempt in range(2):
            if not self._nonce:
                status, headers, _ = _http("HEAD", self._url("newNonce"))
                self._nonce = headers.get("Replay-Nonce", "")
            nonce, self._nonce = self._nonce, ""
            headers = {"Content-Type": "application/jose+json"}
            if accept:
                headers["Accept"] = accept
            status, answer_headers, body = _http(
                "POST", url, self._signed(url, payload, nonce), headers
            )
            self._nonce = answer_headers.get("Replay-Nonce", "")
            if status < 300:
                return answer_headers, body
            problem = _json(body)
            kind = str(problem.get("type", "")).rsplit(":", 1)[-1]
            if kind == "badNonce" and attempt == 0:
                continue
            raise Failed(
                f"Let's Encrypt refused ({kind or status}): {problem.get('detail', '')}".rstrip(
                    ": "
                ),
                _retry_after(answer_headers),
            )
        raise AssertionError("unreachable")

    def register(self) -> str:
        """Make the account (agreeing to Let's Encrypt's terms), or find it again; its URL."""
        headers, _ = self.post(self._url("newAccount"), {"termsOfServiceAgreed": True})
        self.account = headers["Location"]
        return self.account

    def _poll(self, url: str, waiting: tuple[str, ...]) -> dict:
        waited = 0.0
        while True:
            headers, body = self.post(url, None)
            state = _json(body)
            if state.get("status") not in waiting or waited >= POLL_LIMIT:
                return state
            pause = min(max(_retry_after(headers), POLL_SECONDS), 10.0)
            self._sleep(pause)
            waited += pause

    def certificate(self, name: str, key, publish: Callable[[list[str]], None]) -> str:
        """A certificate for ``name`` with ``key`` (PEM, with its chain), proving control of
        the name by DNS-01: ``publish`` puts the challenge's TXT values in DNS ([] removes
        them)."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.x509.oid import NameOID

        headers, body = self.post(
            self._url("newOrder"), {"identifiers": [{"type": "dns", "value": name}]}
        )
        order_url, order = headers["Location"], _json(body)
        for authorization_url in order.get("authorizations", []):
            authorization = _json(self.post(authorization_url, None)[1])
            if authorization.get("status") == "valid":
                continue
            challenge = next(
                (c for c in authorization.get("challenges", []) if c.get("type") == "dns-01"), None
            )
            if challenge is None:
                raise Failed("Let's Encrypt offered no DNS-01 challenge")
            publish([self.dns_value(challenge["token"])])
            try:
                self._sleep(DNS_WAIT)
                self.post(challenge["url"], {})
                authorization = self._poll(authorization_url, ("pending", "processing"))
            finally:
                publish([])
            if authorization.get("status") != "valid":
                errors = [
                    c["error"].get("detail", "")
                    for c in authorization.get("challenges", [])
                    if isinstance(c.get("error"), dict)
                ]
                raise Failed(
                    f"Let's Encrypt couldn't check the name ({authorization.get('status')})"
                    + (f": {errors[0]}" if errors else "")
                )
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        self.post(order["finalize"], {"csr": b64(csr.public_bytes(serialization.Encoding.DER))})
        order = self._poll(order_url, ("pending", "ready", "processing"))
        if order.get("status") != "valid" or not order.get("certificate"):
            raise Failed(f"Let's Encrypt didn't issue the certificate ({order.get('status')})")
        return self.post(order["certificate"], None, "application/pem-certificate-chain")[
            1
        ].decode()


def _write(path: Path, data: bytes, private: bool = False) -> None:
    """Write ``path`` whole or not at all; ``private``: readable only by Gatekeeper."""
    new = path.with_name(path.name + ".new")
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(new, path)


def _key(path: Path, make: Callable[[], object]):
    """The private key in ``path``, made (with ``make``) and kept if missing."""
    from cryptography.hazmat.primitives import serialization

    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = make()
    _write(path, _pem(key), private=True)
    return key


def _pem(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _p256():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


def _ed25519():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return Ed25519PrivateKey.generate()


class HubName:
    """Keeps the hub's name registered, pointing at ``address``, and its certificate fresh.
    ``use(cert_file, key_file, name)``: serve the certificate (tls.NamedCertificate.use)."""

    def __init__(
        self,
        folder: Path,
        claim: str,
        address: str,
        use: Callable[[Path, Path, str], None],
        api: str = API,
        acme: str = LETS_ENCRYPT,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.folder = folder
        self.claim = claim
        self.address = address
        self.use = use
        self.api = api
        self.acme = acme
        self._clock = clock
        self._sleep = sleep
        self._resolve = resolve or _resolve
        self.name = ""
        self._published = ""
        self._served = ""  # the certificate's serial, once served
        self._failures = 0
        self.thread: threading.Thread | None = None

    @property
    def staging(self) -> bool:
        return "staging" in self.acme

    def _state(self, file: str) -> dict:
        return _json((self.folder / file).read_bytes()) if (self.folder / file).exists() else {}

    def _save(self, file: str, state: dict) -> None:
        _write(self.folder / file, json.dumps(state, indent=1).encode(), private=True)

    def step(self) -> float:
        """Register, publish the address, and get or renew the certificate, as needed.
        Seconds until the next check."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        service = NameService(
            self.api, _key(self.folder / "hub-key.pem", _ed25519), clock=self._clock
        )
        hub = self._state("hub.json")
        if hub.get("api") != self.api or not hub.get("id"):
            hub = {**service.register(self.claim), "api": self.api}
            self._save("hub.json", hub)
            log.info("hub name: registered as %s", hub["name"])
        service.hub_id, self.name = hub["id"], hub["name"]

        service.address(self.address)
        if self._published != self.address:
            self._published = self.address
            self._check_resolves()

        account = self._state("account.json")
        acme = Acme(
            self.acme,
            _key(self.folder / "account-key.pem", _p256),
            account.get("url", "") if account.get("directory") == self.acme else "",
            sleep=self._sleep,
        )
        if not acme.account or not account.get("caa"):
            url = acme.register()
            service.caa(url)
            self._save("account.json", {"directory": self.acme, "url": url, "caa": True})
            log.info(
                "hub name: Let's Encrypt%s account %s, named in the CAA record",
                " staging" if self.staging else "",
                url,
            )

        cert = self._certificate()
        if cert is None or cert[0] <= self._clock():
            key = _p256()
            chain = acme.certificate(self.name, key, service.challenge)
            _write(self.folder / "key.pem", _pem(key), private=True)
            _write(self.folder / "cert.pem", chain.encode())
            self._save("cert.json", {"directory": self.acme, "name": self.name})
            cert = self._certificate()
            assert cert is not None
            log.info("hub name: new certificate for %s until %s", self.name, cert[1])
        renew_at, until = cert
        # Every check, so the daily report always has it.
        log.info(
            "hub name: %s points at %s; certificate until %s%s",
            self.name,
            self.address,
            until,
            " (Let's Encrypt staging)" if self.staging else "",
        )
        return max(60.0, min(CHECK_SECONDS, renew_at - self._clock()))

    def _certificate(self) -> tuple[float, str] | None:
        """When the certificate in the folder is due for renewal, and the day it runs out,
        serving it if it isn't yet; None if there's none for this name and Let's Encrypt."""
        from cryptography import x509

        meta = self._state("cert.json")
        cert_file, key_file = self.folder / "cert.pem", self.folder / "key.pem"
        if meta.get("directory") != self.acme or meta.get("name") != self.name:
            return None
        try:
            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        except (OSError, ValueError):
            return None
        start = cert.not_valid_before_utc.timestamp()
        end = cert.not_valid_after_utc.timestamp()
        if str(cert.serial_number) != self._served and end > self._clock():
            self.use(cert_file, key_file, self.name)
            self._served = str(cert.serial_number)
        return start + RENEW_AT * (end - start), cert.not_valid_after_utc.date().isoformat()

    def _check_resolves(self) -> None:
        """Warn when this network's DNS doesn't give the name's address: some routers drop
        public names that point at a home address (DNS rebinding protection)."""
        self._sleep(2)
        try:
            found = self._resolve(self.name)
        except OSError as e:
            found, why = [], str(e)
        else:
            why = f"got {', '.join(found)}" if found else "no address"
        if self.address not in found:
            log.warning(
                "hub name: %s doesn't resolve to %s on this network (%s); a router that "
                "blocks DNS rebinding needs it allowed (README, Hub name)",
                self.name,
                self.address,
                why,
            )

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                wait = self.step()
                self._failures = 0
            except Stop as e:
                log.error("hub name: %s; giving up until Gatekeeper restarts", e)
                return
            except (Failed, OSError, ValueError, KeyError) as e:
                wait = max(
                    RETRY_SECONDS[min(self._failures, len(RETRY_SECONDS) - 1)],
                    getattr(e, "retry_after", 0.0),
                )
                self._failures += 1
                log.warning(
                    "hub name: couldn't get the name or its certificate: %s; trying again in "
                    "%.0f h",
                    e,
                    wait / 3600,
                )
            stop.wait(wait)

    def start(self) -> HubName:
        self.thread = threading.Thread(target=self.run, name="hub-name", daemon=True)
        self.thread.start()
        return self


def _resolve(name: str) -> list[str]:
    return sorted({a[4][0] for a in socket.getaddrinfo(name, 443, proto=socket.IPPROTO_TCP)})
