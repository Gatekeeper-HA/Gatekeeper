"""The hub's name and certificate (hubname.py), against the real name service (cloud/hubnames)
and a stand-in for Let's Encrypt that checks every signature and the DNS-01 challenge."""

import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from gatekeeper import hubname, tls  # noqa: E402
from hubnames.api import serve  # noqa: E402
from hubnames.service import NameService  # noqa: E402
from hubnames.store import Store  # noqa: E402

ZONE = "hubs.example"
HOME = "192.168.40.199"
START = 1_800_000_000.0
DAY = 86400
STAGING_ACCOUNTS = "https://acme-staging-v02.api.letsencrypt.org/acme/acct/"


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class DNS:
    """PowerDNS as the name service sees it: the records it was last given."""

    zone = ZONE

    def __init__(self):
        self.records = {}

    def change(self, records):
        for name, kind, _ttl, contents in records:
            if contents:
                self.records[(name, kind)] = contents
            else:
                self.records.pop((name, kind), None)


def _ca(clock):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    now = datetime.datetime.fromtimestamp(clock[0], datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


class LetsEncrypt:
    """Enough of an ACME server (RFC 8555) to issue a 90-day certificate by DNS-01. It
    checks each request's signature, nonce and URL; the challenge's TXT value and the CAA
    record in ``dns``. ``bad_nonce``: refuse the next request's nonce once."""

    def __init__(self, dns, clock):
        self.dns = dns
        self.clock = clock
        self.ca_key, self.ca = _ca(clock)
        self.nonces = set()
        self.accounts = {}  # url: public key
        self.orders = {}
        self.issued = 0
        self.bad_nonce = False
        self.fail_challenge = False
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def directory(self):
        return f"{self.base}/directory"

    def _nonce(self):
        nonce = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
        self.nonces.add(nonce)
        return nonce

    def _verify(self, url, jws):
        protected = json.loads(unb64(jws["protected"]))
        assert protected["alg"] == "ES256" and protected["url"] == url
        if self.bad_nonce:
            self.bad_nonce = False
            return None, "badNonce"
        assert protected["nonce"] in self.nonces, "a nonce used twice or never given"
        self.nonces.discard(protected["nonce"])
        if "jwk" in protected:
            jwk = protected["jwk"]
            key = ec.EllipticCurvePublicNumbers(
                int.from_bytes(unb64(jwk["x"]), "big"),
                int.from_bytes(unb64(jwk["y"]), "big"),
                ec.SECP256R1(),
            ).public_key()
        else:
            key = self.accounts[protected["kid"]]
        raw = unb64(jws["signature"])
        signature = encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )
        key.verify(
            signature,
            f"{jws['protected']}.{jws['payload']}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )
        payload = json.loads(unb64(jws["payload"])) if jws["payload"] else None
        return (protected, key, payload), None

    def _thumbprint(self, key):
        n = key.public_numbers()
        jwk = {
            "crv": "P-256",
            "kty": "EC",
            "x": hubname.b64(n.x.to_bytes(32, "big")),
            "y": hubname.b64(n.y.to_bytes(32, "big")),
        }
        return hubname.b64(
            hashlib.sha256(json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode()).digest()
        )

    def _issue(self, order, csr):
        name = order["name"]
        sans = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert sans.get_values_for_type(x509.DNSName) == [name]
        now = datetime.datetime.fromtimestamp(self.clock[0], datetime.UTC)
        self.issued += 1
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self.ca.subject)
            .public_key(csr.public_key())
            .serial_number(1000 + self.issued)
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(self.ca_key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM) + self.ca.public_bytes(
            serialization.Encoding.PEM
        )

    def _handler(self):
        acme = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, data=None, headers=None, body=None):
                if body is None:
                    body = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                self.send_header("Replay-Nonce", acme._nonce())
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_HEAD(self):
                self._send(200)

            def do_GET(self):
                b = acme.base
                self._send(
                    200,
                    {
                        "newNonce": f"{b}/nonce",
                        "newAccount": f"{b}/account",
                        "newOrder": f"{b}/order",
                    },
                )

            def do_POST(self):
                url = acme.base + self.path
                jws = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                checked, problem = acme._verify(url, jws)
                if problem:
                    return self._send(400, {"type": f"urn:ietf:params:acme:error:{problem}"})
                protected, key, payload = checked
                path = self.path
                if path == "/account":
                    assert payload == {"termsOfServiceAgreed": True}
                    # An account URL like Let's Encrypt's: the name service takes only those.
                    account = f"{STAGING_ACCOUNTS}{len(acme.accounts) + 1}"
                    acme.accounts[account] = key
                    return self._send(201, {"status": "valid"}, {"Location": account})
                account = protected["kid"]
                if path == "/order":
                    n = len(acme.orders) + 1
                    acme.orders[n] = {
                        "name": payload["identifiers"][0]["value"],
                        "account": account,
                        "status": "pending",
                        "authz": "pending",
                    }
                    return self._send(201, acme._order(n), {"Location": f"{acme.base}/order/{n}"})
                kind, n = path.strip("/").split("/")
                order = acme.orders[int(n)]
                if kind == "order":
                    return self._send(200, acme._order(int(n)))
                if kind == "authz":
                    return self._send(200, acme._authz(int(n)))
                if kind == "chall":
                    hub_id = order["name"].split(".")[0]
                    txt = acme.dns.records.get((f"_acme-challenge.{hub_id}", "TXT"), [])
                    want = hubname.b64(
                        hashlib.sha256(f"token-{n}.{acme._thumbprint(key)}".encode()).digest()
                    )
                    caa = " ".join(acme.dns.records.get((hub_id, "CAA"), []))
                    good = txt == [f'"{want}"'] and f"accounturi={account};" in caa
                    order["authz"] = "valid" if good and not acme.fail_challenge else "invalid"
                    if order["authz"] == "valid":
                        order["status"] = "ready"
                    return self._send(200, {"status": "processing"})
                if kind == "finalize":
                    assert order["status"] == "ready"
                    csr = x509.load_der_x509_csr(unb64(payload["csr"]))
                    order["chain"] = acme._issue(order, csr)
                    order["status"] = "valid"
                    return self._send(200, acme._order(int(n)))
                if kind == "cert":
                    assert self.headers["Accept"] == "application/pem-certificate-chain"
                    return self._send(200, body=order["chain"])
                return self._send(404, {"type": "urn:ietf:params:acme:error:malformed"})

        return Handler

    def _order(self, n):
        order = self.orders[n]
        out = {
            "status": order["status"],
            "identifiers": [{"type": "dns", "value": order["name"]}],
            "authorizations": [f"{self.base}/authz/{n}"],
            "finalize": f"{self.base}/finalize/{n}",
        }
        if order["status"] == "valid":
            out["certificate"] = f"{self.base}/cert/{n}"
        return out

    def _authz(self, n):
        state = self.orders[n]["authz"]
        challenge = {"type": "dns-01", "url": f"{self.base}/chall/{n}", "token": f"token-{n}"}
        if state == "invalid":
            challenge["error"] = {"detail": "Incorrect TXT record"}
        return {
            "status": state,
            "challenges": [{"type": "http-01", "url": "-", "token": "-"}, challenge],
        }


@pytest.fixture
def clock():
    return [START]


@pytest.fixture
def dns():
    return DNS()


@pytest.fixture
def service(tmp_path, dns, clock):
    store = Store(tmp_path / "hubnames.db", clock=lambda: clock[0])
    names = NameService(store, dns, clock=lambda: clock[0])
    server = serve(names, "127.0.0.1", 0)
    names.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield names
    server.shutdown()


@pytest.fixture
def acme(dns, clock):
    server = LetsEncrypt(dns, clock)
    yield server
    server.server.shutdown()


def hub(tmp_path, service, acme, clock, claim=None, served=None, **kw):
    served = [] if served is None else served
    return hubname.HubName(
        tmp_path / "hub-name",
        claim or service.store.new_claims(1)[0],
        HOME,
        lambda cert, key, name: served.append((cert.read_bytes(), name)),
        api=service.url,
        acme=acme.directory,
        clock=lambda: clock[0],
        sleep=lambda s: None,
        resolve=kw.pop("resolve", lambda name: [HOME]),
        **kw,
    )


def san(pem):
    cert = x509.load_pem_x509_certificate(pem)
    return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value


def test_the_hub_gets_its_name_and_a_certificate(tmp_path, service, acme, dns, clock, caplog):
    caplog.set_level("INFO")
    served = []
    h = hub(tmp_path, service, acme, clock, served=served)
    wait = h.step()

    hub_id = h.name.split(".")[0]
    assert h.name == f"{hub_id}.{ZONE}"
    assert dns.records[(hub_id, "A")] == [HOME]
    assert (f"_acme-challenge.{hub_id}", "TXT") not in dns.records  # removed afterwards
    account = json.loads((tmp_path / "hub-name" / "account.json").read_text())["url"]
    assert f"accounturi={account};" in dns.records[(hub_id, "CAA")][0]
    assert len(served) == 1 and served[0][1] == h.name
    assert san(served[0][0]).get_values_for_type(x509.DNSName) == [h.name]
    assert wait == hubname.CHECK_SECONDS  # renewal is 60 days away
    if os.name == "posix":
        for secret in ("hub-key.pem", "account-key.pem", "key.pem", "hub.json"):
            assert (tmp_path / "hub-name" / secret).stat().st_mode & 0o777 == 0o600
    text = caplog.text
    assert f"hub name: registered as {h.name}" in text
    assert f"hub name: new certificate for {h.name} until " in text
    until = datetime.datetime.fromtimestamp(START, datetime.UTC).date() + datetime.timedelta(90)
    # The line the daily report reads, at every check.
    assert f"hub name: {h.name} points at {HOME}; certificate until {until}\n" in text


def test_the_certificate_is_renewed_at_two_thirds_of_its_life(tmp_path, service, acme, clock):
    served = []
    h = hub(tmp_path, service, acme, clock, served=served)
    h.step()
    clock[0] += 30 * DAY
    h.step()
    assert acme.issued == 1 and len(served) == 1  # kept, and not served again
    clock[0] += 31 * DAY  # 61 of 90 days
    h.step()
    assert acme.issued == 2 and len(served) == 2 and served[1][0] != served[0][0]


def test_a_restart_serves_the_certificate_it_has(tmp_path, service, acme, clock):
    h = hub(tmp_path, service, acme, clock)
    h.step()
    served = []
    again = hub(tmp_path, service, acme, clock, claim="USED-CODE-XXXX", served=served)
    again.step()
    assert acme.issued == 1 and len(acme.accounts) == 1  # no new order, account or name
    assert again.name == h.name and len(served) == 1


def test_a_refused_nonce_is_tried_again(tmp_path, service, acme, clock):
    acme.bad_nonce = True
    h = hub(tmp_path, service, acme, clock)
    h.step()
    assert acme.issued == 1


def test_a_failed_challenge_is_tried_again_later(tmp_path, service, acme, dns, clock, caplog):
    acme.fail_challenge = True
    h = hub(tmp_path, service, acme, clock)
    with pytest.raises(hubname.Failed, match="Incorrect TXT record"):
        h.step()
    assert not any(kind == "TXT" for _, kind in dns.records)

    class Stop(threading.Event):
        waits = []

        def wait(self, timeout=None):
            self.waits.append(timeout)
            self.set()

    stop = Stop()
    h.run(stop)
    assert stop.waits == [hubname.RETRY_SECONDS[0]]
    assert "hub name: couldn't get the name or its certificate: Let's Encrypt" in caplog.text


def test_a_refused_claim_code_gives_up(tmp_path, service, acme, clock, caplog):
    h = hub(tmp_path, service, acme, clock, claim="NOPE-NOPE-NOPE")
    h.run()  # returns
    assert "hub name: the name service refused: " in caplog.text
    assert "giving up until Gatekeeper restarts" in caplog.text
    assert acme.issued == 0


def test_a_router_that_drops_the_name_is_warned_about(tmp_path, service, acme, clock, caplog):
    h = hub(tmp_path, service, acme, clock, resolve=lambda name: [])
    h.step()
    assert f"hub name: {h.name} doesn't resolve to {HOME} on this network" in caplog.text


def test_a_home_address_is_a_private_ipv4():
    assert hubname.home_address(["", "example.com", "8.8.8.8", HOME]) == HOME
    assert hubname.home_address(["10.1.2.3"]) == "10.1.2.3"
    assert hubname.home_address(["172.32.0.1", "fd00::1"]) == ""


def _peer(port, server_name):
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE
    with socket.create_connection(("127.0.0.1", port)) as raw:
        with client.wrap_socket(raw, server_hostname=server_name) as conn:
            der = conn.getpeercert(binary_form=True)
    return x509.load_der_x509_certificate(der)


def test_the_name_gets_its_certificate_and_the_address_the_self_signed_one(tmp_path, clock):
    base = tls.server_context(*tls.certificate(tmp_path / "tls", ["127.0.0.1"]))
    named = tls.NamedCertificate(base)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stopping = threading.Event()

    def accept():
        while not stopping.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:  # closed at the end of the test
                return
            try:
                with base.wrap_socket(conn, server_side=True):
                    pass
            except (ssl.SSLError, OSError):
                pass

    threading.Thread(target=accept, daemon=True).start()
    ca_key, ca = _ca(clock)

    def issue(serial):
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hub")]))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("abc.hubs.example")]), False)
            .sign(ca_key, hashes.SHA256())
        )
        cert_file, key_file = tmp_path / f"cert{serial}.pem", tmp_path / f"key{serial}.pem"
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(hubname._pem(key))
        return cert_file, key_file

    try:
        assert _peer(port, "abc.hubs.example").issuer.rfc4514_string() == "CN=Gatekeeper"
        named.use(*issue(7), "ABC.hubs.example")
        assert _peer(port, "abc.hubs.example").serial_number == 7
        assert _peer(port, "other.example").issuer.rfc4514_string() == "CN=Gatekeeper"
        named.use(*issue(8), "abc.hubs.example")  # renewed: new connections get it
        assert _peer(port, "abc.hubs.example").serial_number == 8
    finally:
        stopping.set()
        listener.close()
