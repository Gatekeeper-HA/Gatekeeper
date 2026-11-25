import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hubnames import __main__ as cli
from hubnames.api import Strikes, client, serve
from hubnames.powerdns import PowerDNS
from hubnames.service import NameService, Relay, TooMany, b64, message, zone_records
from hubnames.store import Refused, Store

ZONE = "hubs.example"
NOW = 1_800_000_000.0
LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/acme/acct/123456789"
VALUE = "x" * 43  # a DNS-01 challenge's TXT value: base64url of a SHA-256


class PowerDNSAPI:
    """PowerDNS's API as far as the name service uses it: create the zone, and PATCH its
    record sets."""

    def __init__(self):
        self.patches = []
        self.created = []
        self.status = 204
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.path == f"/api/v1/servers/localhost/zones/{ZONE}."
                self.send_response(200 if outer.created else 404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                assert self.path == "/api/v1/servers/localhost/zones"
                assert self.headers["X-API-Key"] == "pdns-key"
                outer.created.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

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
        (f"{hub_id}.{ZONE}.", "AAAA"): "DELETE",  # the relay's, while relayed
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


def test_the_zone_is_created_once_and_its_own_records_set(pdns, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HUBNAMES_ZONE", ZONE)
    monkeypatch.setenv("PDNS_API_URL", pdns.url)
    monkeypatch.setenv("PDNS_API_KEY", "pdns-key")
    monkeypatch.setenv("HUBNAMES_DB", str(tmp_path / "hubnames.db"))
    monkeypatch.setenv("SERVER_IPV4", "203.0.113.5")
    monkeypatch.setenv("SERVER_IPV6", "2001:db8::5")
    cli.main(["zone"])
    cli.main(["zone"])  # again: kept, not created twice
    assert capsys.readouterr().out.count("created") == 1
    [zone] = pdns.created
    assert zone["name"] == f"{ZONE}." and zone["kind"] == "Native"
    rrsets = {(r["name"], r["type"]): [x["content"] for x in r["records"]] for r in zone["rrsets"]}
    assert rrsets == {
        (f"{ZONE}.", "SOA"): [f"ns1.{ZONE}. hostmaster.{ZONE}. 1 10800 3600 604800 60"],
        (f"{ZONE}.", "NS"): [f"ns1.{ZONE}.", f"ns2.{ZONE}."],
    }
    records = {k: [x["content"] for x in r["records"]] for k, r in pdns.last().items()}
    assert (f"{ZONE}.", "SOA") not in records  # its serial is PowerDNS's to keep
    assert records[(f"api.{ZONE}.", "A")] == ["203.0.113.5"]
    assert records[(f"ns2.{ZONE}.", "AAAA")] == ["2001:db8::5"]
    assert records[(f"{ZONE}.", "CAA")] == ['0 issue "letsencrypt.org"', '0 issuewild ";"']
    assert records[(f"{ZONE}.", "MX")] == ["0 ."]  # no email
    assert records[(f"_dmarc.{ZONE}.", "TXT")] == ['"v=DMARC1; p=reject"']
    assert all(r[1] != "AAAA" for r in zone_records(ZONE, "203.0.113.5"))
    with pytest.raises(ValueError):
        zone_records(ZONE, "ns1.example")


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


def test_an_address_with_many_refused_requests_gets_429(names):
    now = [0.0]
    server = serve(names, "127.0.0.1", 0, strikes=Strikes(clock=lambda: now[0]))
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(body, address="8.8.8.8"):
        # Caddy puts the client's address last.
        headers = {"X-Forwarded-For": f"10.9.9.9, {address}"}
        req = urllib.request.Request(base + "/v1/hubs", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), e.headers

    try:
        # Plain errors, not Python's own wording.
        assert post(b"{nope")[1] == {"error": "the body: a JSON object"}
        bad_key = json.dumps({"claim": "AAAA-BBBB-CCCC", "key": "a"}).encode()
        assert post(bad_key)[1] == {"error": "key: an Ed25519 public key, base64url"}
        for _ in range(Strikes.LIMIT - 2):
            assert post(b"[]")[0] == 400
        status, answer, headers = post(b"{}")
        assert status == 429 and "too many refused requests" in answer["error"]
        assert headers["Retry-After"] == "3601"
        assert post(b"{}", address="1.1.1.1")[0] == 400  # another address is answered
        now[0] += 3600  # the first strike is an hour old
        assert post(b"{}")[0] == 400
    finally:
        server.shutdown()


def test_only_internet_addresses_are_limited():
    strikes = Strikes(clock=lambda: 0.0)
    # Docker's proxy (IPv6 clients on a network without IPv6) stands for everyone behind it:
    # counting it would let one client lock all of them out.
    for _ in range(Strikes.LIMIT + 5):
        strikes.add("172.18.0.1")
        strikes.add("::1")
    assert strikes.wait("172.18.0.1") == 0 and strikes.wait("::1") == 0
    for _ in range(Strikes.LIMIT):
        strikes.add("2606:4700:4700::1111")
    assert strikes.wait("2606:4700:4700::1111") > 0
    # An IPv4 client written as IPv6 is the same client.
    assert client("10.9.9.9, ::ffff:8.8.8.8", "172.18.0.5") == "8.8.8.8"
    assert client("", "8.8.8.8") == "8.8.8.8"


FERNET_KEY = "x6lPI2Gk4Kq0wKxHvYp2VfGdV9n8f8l2bq3cS2nT0Ag="  # a test key: 32 bytes, base64url
RELAY = Relay(f"relay.{ZONE}", (FERNET_KEY,), "turn-secret")


@pytest.fixture
def relayed(tmp_path, pdns, clock):
    store = Store(tmp_path / "relayed.db", clock=lambda: clock[0])
    return NameService(store, PowerDNS(pdns.url, "pdns-key", ZONE), lambda: clock[0], RELAY)


def test_relay_passes_need_cloud_plus(relayed, names, clock):
    hub_id, _ = hub(relayed)
    for ask in (relayed.tunnel, relayed.turn):
        with pytest.raises(Refused, match="no Cloud Plus"):
            ask(hub_id)
    relayed.store.set_plus(hub_id, NOW + 86400)
    first, second = relayed.tunnel(hub_id), relayed.tunnel(hub_id)
    assert first["relay"] == f"relay.{ZONE}" and first["port"] == 443
    assert first["until"] == int(NOW) + 3600
    # What the relay (SniTun) reads: the hub's own name, the key the hub got, the cipher.
    content = json.loads(Fernet(FERNET_KEY).decrypt(first["token"].encode()))
    assert content == {
        "valid": int(NOW) + 3600,
        "hostname": f"{hub_id}.{ZONE}",
        "aes_key": first["aes_key"],
        "aes_iv": first["aes_iv"],
        "protocol_version": 2,
        "cipher": "aes-gcm-siv",
    }
    assert len(bytes.fromhex(first["aes_key"])) == 32 and len(bytes.fromhex(first["aes_iv"])) == 16
    assert first["aes_key"] != second["aes_key"]  # a new key for every connection
    assert "alias" not in content  # only its own name

    other_id, _ = hub(names)
    names.store.set_plus(other_id, NOW + 10 * 86400)
    with pytest.raises(LookupError, match="no relay"):  # a service without a relay
        names.tunnel(other_id)
    clock[0] = NOW + 86400  # Cloud Plus has ended
    with pytest.raises(Refused, match="no Cloud Plus"):
        relayed.tunnel(hub_id)


def test_turn_credentials_are_coturns_rest_format(relayed):
    hub_id, _ = hub(relayed)
    relayed.store.set_plus(hub_id, NOW + 86400)
    turn = relayed.turn(hub_id)
    until = int(NOW) + 4 * 3600
    assert turn["username"] == f"{until}:{hub_id}" and turn["until"] == until
    digest = hmac.new(b"turn-secret", turn["username"].encode(), hashlib.sha1).digest()
    assert turn["credential"] == base64.b64encode(digest).decode()
    assert turn["urls"] == [
        f"stun:relay.{ZONE}:3478",
        f"turn:relay.{ZONE}:3478?transport=udp",
        f"turn:relay.{ZONE}:3478?transport=tcp",
    ]


def test_a_new_fernet_key_goes_first(relayed):
    hub_id, _ = hub(relayed)
    relayed.store.set_plus(hub_id, NOW + 86400)
    new = Fernet.generate_key().decode()
    relayed.relay = Relay(RELAY.name, (new, FERNET_KEY), RELAY.turn_secret)
    token = relayed.tunnel(hub_id)["token"].encode()
    assert json.loads(Fernet(new).decrypt(token))["hostname"].startswith(hub_id)
    with pytest.raises(InvalidToken):
        Fernet(FERNET_KEY).decrypt(token)


def test_relay_passes_over_http(relayed):
    server = serve(relayed, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    hub_id, key = hub(relayed)

    def call(method, path, sign=True):
        headers = {"Authorization": signed(key, method, path, b"{}")} if sign else {}
        req = urllib.request.Request(base + path, b"{}", method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    try:
        status, answer = call("POST", f"/v1/hubs/{hub_id}/tunnel")
        assert status == 403 and answer == {"error": "this hub has no Cloud Plus"}
        relayed.store.set_plus(hub_id, NOW + 86400)
        status, answer = call("POST", f"/v1/hubs/{hub_id}/tunnel")
        assert status == 200 and answer["cipher"] == "aes-gcm-siv" and answer["token"]
        status, answer = call("POST", f"/v1/hubs/{hub_id}/turn")
        assert status == 200 and answer["username"].endswith(f":{hub_id}")
        assert call("POST", f"/v1/hubs/{hub_id}/tunnel", sign=False)[0] == 403
        assert call("PUT", f"/v1/hubs/{hub_id}/tunnel")[0] == 404
        assert call("POST", f"/v1/hubs/{hub_id}/address")[0] == 404
    finally:
        server.shutdown()


def test_cloud_plus_from_the_command_line(tmp_path, pdns, monkeypatch, capsys):
    monkeypatch.setenv("HUBNAMES_ZONE", ZONE)
    monkeypatch.setenv("PDNS_API_URL", pdns.url)
    monkeypatch.setenv("PDNS_API_KEY", "pdns-key")
    monkeypatch.setenv("HUBNAMES_DB", str(tmp_path / "hubnames.db"))
    names = cli.service()
    assert names.relay is None  # no relay without its keys
    hub_id = register(names, names.store.new_claims(1)[0], Ed25519PrivateKey.generate(),
                      when=time.time())["id"]  # fmt: skip
    cli.main(["plus", hub_id, "90"])
    first = names.store.plus_until(hub_id)
    assert abs(first - (time.time() + 90 * 86400)) < 60
    cli.main(["plus", hub_id, "30"])  # more: from the end of what it has
    assert names.store.plus_until(hub_id) == pytest.approx(first + 30 * 86400)
    cli.main(["plus"])
    out = capsys.readouterr().out
    assert f"{hub_id}: Cloud Plus until " in out and f"{hub_id}  until " in out
    cli.main(["plus", hub_id, "off"])
    assert names.store.plus_until(hub_id) is None
    for wrong in (["plus", hub_id, "0"], ["plus", hub_id, "soon"], ["plus", "nohubhere", "9"]):
        with pytest.raises(SystemExit):
            cli.main(wrong)

    monkeypatch.setenv("RELAY_FERNET_KEYS", f" {FERNET_KEY} ,")
    monkeypatch.setenv("TURN_SECRET", "turn-secret")
    assert cli.service().relay == RELAY
    monkeypatch.delenv("TURN_SECRET")
    with pytest.raises(SystemExit, match="TURN_SECRET"):
        cli.service()


def test_removing_a_hub_ends_its_cloud_plus(relayed):
    hub_id, _ = hub(relayed)
    relayed.store.set_plus(hub_id, NOW + 86400)
    relayed.remove(hub_id)
    assert relayed.store.plus_until(hub_id) is None


def test_the_relay_has_a_name_in_the_zone():
    records = {(r[0], r[1]): r[3] for r in zone_records(ZONE, "203.0.113.5", "", "203.0.113.9",
                                                         "2001:db8::9")}  # fmt: skip
    assert records[("relay", "A")] == ["203.0.113.9"]
    assert records[("relay", "AAAA")] == ["2001:db8::9"]
    assert ("relay", "A") not in {(r[0], r[1]) for r in zone_records(ZONE, "203.0.113.5")}
    with pytest.raises(ValueError):
        zone_records(ZONE, "203.0.113.5", relay_ipv4="relay.example")


ROUTED = Relay(f"relay.{ZONE}", (FERNET_KEY,), "turn-secret", ipv4="203.0.113.9",
               ipv6="2001:db8::9")  # fmt: skip


def records(pdns):
    return {k: [x["content"] for x in r.get("records", [])] for k, r in pdns.last().items()}


def test_a_hubs_name_follows_its_tunnel(relayed, pdns, clock):
    relayed.relay = ROUTED
    hub_id, _ = hub(relayed)
    name = f"{hub_id}.{ZONE}."
    relayed.address(hub_id, "192.168.1.20")
    assert records(pdns)[(name, "A")] == ["192.168.1.20"]
    with pytest.raises(Refused, match="no Cloud Plus"):
        relayed.route(hub_id, "relay")
    relayed.store.set_plus(hub_id, NOW + 86400)

    relayed.route(hub_id, "relay")  # the hub's tunnel is up
    assert records(pdns) == {(name, "A"): ["203.0.113.9"], (name, "AAAA"): ["2001:db8::9"]}
    patches = len(pdns.patches)
    relayed.address(hub_id, "192.168.1.21")  # its 12-hourly update: the name stays relayed
    assert len(pdns.patches) == patches
    assert relayed.store.route(hub_id) == ("192.168.1.21", True)

    relayed.route(hub_id, "home")  # the tunnel closed
    assert records(pdns) == {(name, "A"): ["192.168.1.21"], (name, "AAAA"): []}
    assert relayed.store.route(hub_id) == ("192.168.1.21", False)
    with pytest.raises(ValueError, match="via"):
        relayed.route(hub_id, "elsewhere")


def test_the_sweep_sends_a_name_home_once_cloud_plus_ends(relayed, pdns, clock):
    relayed.relay = ROUTED
    hub_id, _ = hub(relayed)
    relayed.address(hub_id, "192.168.1.20")
    relayed.store.set_plus(hub_id, NOW + 3600)
    relayed.route(hub_id, "relay")
    relayed.sweep()  # still Cloud Plus: stays
    assert relayed.store.route(hub_id) == ("192.168.1.20", True)
    clock[0] = NOW + 3601
    relayed.sweep()
    assert records(pdns)[(f"{hub_id}.{ZONE}.", "A")] == ["192.168.1.20"]
    assert relayed.store.route(hub_id) == ("192.168.1.20", False)
    assert relayed.store.relayed_hubs() == []


def test_the_route_over_http(relayed, pdns):
    relayed.relay = ROUTED
    server = serve(relayed, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    hub_id, key = hub(relayed)
    relayed.store.set_plus(hub_id, NOW + 86400)

    def put(data):
        path = f"/v1/hubs/{hub_id}/route"
        body = json.dumps(data).encode()
        headers = {"Authorization": signed(key, "PUT", path, body)}
        req = urllib.request.Request(base + path, body, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert put({"via": "relay"}) == 204
        assert relayed.store.route(hub_id)[1] is True
        assert put({"via": "home"}) == 204 and relayed.store.route(hub_id)[1] is False
        assert put({"via": "moon"}) == 400
    finally:
        server.shutdown()
