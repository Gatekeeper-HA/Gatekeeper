import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hubnames import __main__ as cli
from hubnames.api import serve
from hubnames.powerdns import PowerDNS
from hubnames.service import NameService, TooMany, b64, message
from hubnames.store import Refused, Store

ZONE = "hubs.example"
NOW = 1_800_000_000.0
LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/acme/acct/123456789"
VALUE = "x" * 43  # a DNS-01 challenge's TXT value: base64url of a SHA-256


class PowerDNSAPI:
    """PowerDNS's API as far as the name service uses it: PATCH a zone's record sets."""

    def __init__(self):
        self.patches = []
        self.status = 204
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_PATCH(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.headers["X-API-Key"] == "pdns-key"
                assert self.path == f"/api/v1/servers/localhost/zones/{ZONE}."
                if outer.status == 204:
                    outer.patches.append(body["rrsets"])
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def last(self):
        return {(r["name"], r["type"]): r for r in self.patches[-1]}


@pytest.fixture
def pdns():
    api = PowerDNSAPI()
    yield api
    api.server.shutdown()


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def names(tmp_path, pdns, clock):
    store = Store(tmp_path / "hubnames.db", clock=lambda: clock[0])
    return NameService(store, PowerDNS(pdns.url, "pdns-key", ZONE), clock=lambda: clock[0])


def public(key):
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def signed(key, method, path, body=b"", when=NOW):
    return f"Hub {int(when)}.{b64(key.sign(message(method, path, int(when), body)))}"


def register(names, claim, key, signer=None, when=NOW):
    body = json.dumps({"claim": claim, "key": b64(public(key))}).encode()
    header = signed(signer or key, "POST", "/v1/hubs", body, when)
    return names.register(claim, public(key), header, "/v1/hubs", body)


def test_a_claim_code_registers_one_hub(names):
    code, other_code = names.store.new_claims(2)
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}", code)
    key = Ed25519PrivateKey.generate()
    hub = register(names, code, key)
    assert re.fullmatch(r"[a-z2-7]{10}", hub["id"]) and hub["name"] == f"{hub['id']}.{ZONE}"
    # Asking again (the answer was lost) gives the same hub; the code is typed loosely.
    assert register(names, code.lower().replace("-", " "), key) == hub
    with pytest.raises(Refused, match="already used"):
        register(names, code, Ed25519PrivateKey.generate())
    with pytest.raises(Refused, match="unknown"):
        register(names, "AAAA-BBBB-CCCC", Ed25519PrivateKey.generate())
    with pytest.raises(Refused, match="key already registered"):
        register(names, other_code, key)
    with pytest.raises(Refused, match="bad signature"):  # it must hold the key it registers
        register(names, other_code, Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate())
    with pytest.raises(ValueError, match="32 bytes"):
        names.register(other_code, b"short", "", "/v1/hubs", b"")


def hub(names):
    key = Ed25519PrivateKey.generate()
    return register(names, names.store.new_claims(1)[0], key)["id"], key


def test_changes_need_the_hubs_signature_made_now(names, clock):
    hub_id, key = hub(names)
    path = f"/v1/hubs/{hub_id}/address"
    body = b'{"address": "192.168.1.20"}'
    names.check(hub_id, signed(key, "PUT", path, body), "PUT", path, body)
    stranger = Ed25519PrivateKey.generate()
    with pytest.raises(Refused, match="bad signature"):
        names.check(hub_id, signed(stranger, "PUT", path, body), "PUT", path, body)
    with pytest.raises(Refused, match="bad signature"):  # signed for another body
        names.check(hub_id, signed(key, "PUT", path, b"{}"), "PUT", path, body)
    with pytest.raises(Refused, match="clock"):
        names.check(hub_id, signed(key, "PUT", path, body, NOW - 301), "PUT", path, body)
    with pytest.raises(Refused, match="no signature"):
        names.check(hub_id, "Bearer x", "PUT", path, body)
    with pytest.raises(LookupError):
        names.check("aaaaaaaaaa", signed(key, "PUT", path, body), "PUT", path, body)


def test_a_name_points_only_at_a_home_network(names, pdns):
    hub_id, _ = hub(names)
    names.address(hub_id, "192.168.1.20")
    record = pdns.last()[(f"{hub_id}.{ZONE}.", "A")]
    assert record["changetype"] == "REPLACE" and record["ttl"] == 60
    assert record["records"] == [{"content": "192.168.1.20", "disabled": False}]
    names.address(hub_id, "10.0.0.5")
    names.address(hub_id, "172.31.255.1")
    for address in ("8.8.8.8", "172.32.0.1", "127.0.0.1", "fd00::1", "router", None):
        with pytest.raises(ValueError, match="address"):
            names.address(hub_id, address)
    assert len(pdns.patches) == 3


def test_a_challenge_is_published_and_swept(names, pdns, clock):
    hub_id, _ = hub(names)
    names.challenge(hub_id, [VALUE])
    record = pdns.last()[(f"_acme-challenge.{hub_id}.{ZONE}.", "TXT")]
    assert record["records"] == [{"content": f'"{VALUE}"', "disabled": False}]
    for values in ([VALUE] * 3, ["short"], ['"quoted"' + "x" * 35], [7]):
        with pytest.raises(ValueError, match="values"):
            names.challenge(hub_id, values)
    clock[0] += 599
    names.sweep()
    assert len(pdns.patches) == 1
    clock[0] += 2
    names.sweep()
    assert pdns.last()[(f"_acme-challenge.{hub_id}.{ZONE}.", "TXT")]["changetype"] == "DELETE"
    names.sweep()
    assert len(pdns.patches) == 2  # swept once
    names.challenge(hub_id, [VALUE])
    names.challenge(hub_id, [])  # the hub removes it itself
    clock[0] += 3600
    names.sweep()
    assert len(pdns.patches) == 4


def test_caa_names_the_hubs_own_account(names, pdns):
    hub_id, _ = hub(names)
    names.caa(hub_id, LETS_ENCRYPT)
    record = pdns.last()[(f"{hub_id}.{ZONE}.", "CAA")]
    assert [r["content"] for r in record["records"]] == [
        f'0 issue "letsencrypt.org; accounturi={LETS_ENCRYPT}; validationmethods=dns-01"',
        '0 issuewild ";"',
    ]
    names.caa(hub_id, "https://acme-staging-v02.api.letsencrypt.org/acme/acct/42")
    for account in ("https://ca.example/acme/acct/1", LETS_ENCRYPT + '"; x', None):
        with pytest.raises(ValueError, match="account"):
            names.caa(hub_id, account)


def test_a_hub_can_change_its_records_60_times_an_hour(names, clock):
    hub_id, key = hub(names)
    path = f"/v1/hubs/{hub_id}/challenge"
    for _ in range(60):
        names.check(hub_id, signed(key, "PUT", path, when=clock[0]), "PUT", path, b"")
    with pytest.raises(TooMany):
        names.check(hub_id, signed(key, "PUT", path, when=clock[0]), "PUT", path, b"")
    clock[0] += 3601
    names.check(hub_id, signed(key, "PUT", path, when=clock[0]), "PUT", path, b"")


def test_removing_a_hub_takes_its_records_down(names, pdns):
    hub_id, _ = hub(names)
    names.remove(hub_id)
    removed = pdns.last()
    assert {k: r["changetype"] for k, r in removed.items()} == {
        (f"{hub_id}.{ZONE}.", "A"): "DELETE",
        (f"{hub_id}.{ZONE}.", "CAA"): "DELETE",
        (f"_acme-challenge.{hub_id}.{ZONE}.", "TXT"): "DELETE",
    }
    assert names.store.hub(hub_id) is None


def test_the_api_over_http(names, pdns):
    server = serve(names, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    key = Ed25519PrivateKey.generate()

    def call(method, path, data=None, sign=True):
        body = json.dumps(data).encode() if data is not None else b""
        headers = {"Authorization": signed(key, method, path, body)} if sign else {}
        req = urllib.request.Request(base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    try:
        assert call("GET", "/v1/health") == (200, {"ok": True})
        claim = names.store.new_claims(1)[0]
        status, answer = call("POST", "/v1/hubs", {"claim": claim, "key": b64(public(key))})
        assert status == 201 and answer["name"].endswith(f".{ZONE}")
        hub_id = answer["id"]
        assert call("PUT", f"/v1/hubs/{hub_id}/address", {"address": "192.168.1.20"}) == (
            204,
            None,
        )
        assert call("PUT", f"/v1/hubs/{hub_id}/challenge", {"values": [VALUE]})[0] == 204
        assert call("PUT", f"/v1/hubs/{hub_id}/caa", {"account": LETS_ENCRYPT})[0] == 204
        status, answer = call("PUT", f"/v1/hubs/{hub_id}/address", {"address": "1.1.1.1"})
        assert status == 400 and "home network" in answer["error"]
        assert call("PUT", f"/v1/hubs/{hub_id}/challenge", {"values": VALUE})[0] == 400
        assert call("PUT", f"/v1/hubs/{hub_id}/address", [1])[0] == 400
        status, answer = call("PUT", f"/v1/hubs/{hub_id}/address", {}, sign=False)
        assert status == 403 and "signature" in answer["error"]
        assert call("PUT", f"/v1/hubs/{hub_id}/name", {})[0] == 404
        unknown = {"claim": "AAAA-BBBB-CCCC", "key": b64(public(key))}
        assert call("POST", "/v1/hubs", unknown)[0] == 403
        pdns.status = 422
        status, answer = call("PUT", f"/v1/hubs/{hub_id}/address", {"address": "10.0.0.2"})
        assert status == 502 and "DNS" in answer["error"]
        pdns.status = 204
        assert call("DELETE", f"/v1/hubs/{hub_id}") == (204, None)
        assert call("PUT", f"/v1/hubs/{hub_id}/address", {"address": "10.0.0.2"})[0] == 404
    finally:
        server.shutdown()


def test_claim_codes_from_the_command_line(tmp_path, pdns, monkeypatch, capsys):
    monkeypatch.setenv("HUBNAMES_ZONE", ZONE)
    monkeypatch.setenv("PDNS_API_URL", pdns.url)
    monkeypatch.setenv("PDNS_API_KEY", "pdns-key")
    monkeypatch.setenv("HUBNAMES_DB", str(tmp_path / "hubnames.db"))
    cli.main(["claims", "3"])
    codes = capsys.readouterr().out.split()
    assert len(codes) == 3
    names = cli.service()
    hub_id = register(names, codes[0], Ed25519PrivateKey.generate(), when=time.time())["id"]
    cli.main(["remove", hub_id])
    assert names.store.hub(hub_id) is None
    monkeypatch.delenv("PDNS_API_KEY")
    with pytest.raises(SystemExit, match="PDNS_API_KEY"):
        cli.main([])
