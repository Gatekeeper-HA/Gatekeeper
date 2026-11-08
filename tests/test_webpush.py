import base64
import contextlib
import json
import os
import re
import struct
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: E402

from gatekeeper import web, webpush  # noqa: E402
from gatekeeper.notify import Notification  # noqa: E402
from gatekeeper.visits import Labels, VisitsPage  # noqa: E402
from gatekeeper.webpush import WebPush, b64, unb64  # noqa: E402

NOW = 1_800_000_000.0
PASSWORD = "porch-light"


def raw(key) -> bytes:
    return webpush._raw(key.public_key())


def decrypt(body: bytes, phone_key, auth: bytes) -> bytes:
    """What the phone does with a message (RFC 8291, the receiving side)."""
    salt, size, length = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_public = body[21 : 21 + length]
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    shared = phone_key.exchange(ec.ECDH(), sender)
    info = b"WebPush: info\0" + raw(phone_key) + as_public
    ikm = HKDF(hashes.SHA256(), 32, auth, info).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\0").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\0").derive(ikm)
    assert len(body) <= size == 4096
    plain = AESGCM(cek).decrypt(nonce, body[21 + length :], None)
    assert plain.endswith(b"\x02")  # the last record, no padding
    return plain[:-1]


class Phone:
    """A phone's browser: its keys, and the subscription it gives the page."""

    def __init__(self, endpoint="https://fcm.example/send/abc"):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.subscription = {
            "endpoint": endpoint,
            "expirationTime": None,
            "keys": {"p256dh": b64(raw(self.key)), "auth": b64(self.auth)},
        }

    def read(self, request) -> dict:
        return json.loads(decrypt(request.data, self.key, self.auth))


class PushService:
    """The push services, as ``urlopen``: what was posted, and their answers."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.posted = []

    def __call__(self, request, timeout):
        self.posted.append(request)
        code = self.answers.get(request.full_url, 201)
        if code >= 400:
            raise urllib.error.HTTPError(request.full_url, code, "No", {}, None)
        return contextlib.nullcontext()


def test_encryption_matches_rfc_8291s_example():
    # RFC 8291, Appendix A: the same keys and salt give the same message, byte for byte.
    as_private = unb64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    as_key = ec.derive_private_key(int.from_bytes(as_private, "big"), ec.SECP256R1())
    ua_public = unb64(
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    )
    body = webpush.encrypt(
        b"When I grow up, I want to be a watermelon",
        ua_public,
        unb64("BTBZMqHH6r4Tts7J_aSIgg"),
        as_key,
        unb64("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    header = unb64(
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmY"
        "WAmS6TlzAC8wEqKK6PBru3jl7A8"
    )
    ciphertext = unb64(
        "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
    )
    assert len(header) == 86 and body == header + ciphertext


def test_a_message_reads_back_on_the_phone():
    phone = Phone()
    body = webpush.encrypt(b"Someone at the front door", raw(phone.key), phone.auth)
    assert decrypt(body, phone.key, phone.auth) == b"Someone at the front door"
    # A new key and salt each time.
    assert webpush.encrypt(b"x", raw(phone.key), phone.auth)[:16] != body[:16]


def test_the_push_service_is_shown_a_signed_token():
    key = ec.generate_private_key(ec.SECP256R1())
    header = webpush.vapid("https://fcm.example/send/abc", key, NOW)
    m = re.fullmatch(r"vapid t=([\w-]+)\.([\w-]+)\.([\w-]+), k=([\w-]+)", header)
    assert json.loads(unb64(m[1])) == {"typ": "JWT", "alg": "ES256"}
    assert json.loads(unb64(m[2])) == {
        "aud": "https://fcm.example",
        "exp": int(NOW) + 12 * 3600,
        "sub": webpush.CONTACT,
    }
    assert unb64(m[4]) == raw(key)
    signature = unb64(m[3])
    r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
    key.public_key().verify(  # raises if it isn't
        encode_dss_signature(r, s), f"{m[1]}.{m[2]}".encode(), ec.ECDSA(hashes.SHA256())
    )


def test_phones_and_the_key_are_kept(tmp_path):
    push = WebPush(tmp_path, clock=lambda: NOW)
    assert not push.active
    phone = Phone()
    push.subscribe(phone.subscription)
    push.subscribe(phone.subscription)  # told again at each visit to the page: kept once
    again = WebPush(tmp_path)
    assert again.active and len(again._phones) == 1
    assert again.public_key == push.public_key  # the phones' subscriptions are tied to it
    again.unsubscribe(phone.subscription["endpoint"])
    assert not again.active and not WebPush(tmp_path).active

    for n in range(webpush.MAX_PHONES + 1):
        push.subscribe(Phone(f"https://fcm.example/send/{n}").subscription)
    assert len(push._phones) == webpush.MAX_PHONES
    assert push._phones[0]["endpoint"] == "https://fcm.example/send/1"  # the oldest went


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "http://fcm.example/send/abc"},  # not HTTPS
        {"endpoint": 7},
        {"keys": {"p256dh": "AAAA", "auth": b64(os.urandom(16))}},  # not a key
        {"keys": {"p256dh": b64(raw(ec.generate_private_key(ec.SECP256R1()))), "auth": "AA"}},
        {"keys": None},
    ],
)
def test_what_isnt_a_subscription_is_refused(tmp_path, change):
    with pytest.raises(ValueError):
        WebPush(tmp_path).subscribe({**Phone().subscription, **change})


def test_a_notification_goes_to_every_phone_encrypted(tmp_path):
    service = PushService()
    push = WebPush(tmp_path, clock=lambda: NOW, opener=service)
    phones = [Phone("https://fcm.example/send/1"), Phone("https://push.example/2")]
    for phone in phones:
        push.subscribe(phone.subscription)
    push.send(Notification("Visitor at the front door", 'Said: "Hi"', priority=4, tag="e1"), None)

    assert [r.full_url for r in service.posted] == [p.subscription["endpoint"] for p in phones]
    for phone, request in zip(phones, service.posted, strict=True):
        assert phone.read(request) == {
            "title": "Visitor at the front door",
            "body": 'Said: "Hi"',
            "tag": "e1",
            "silent": False,
            "url": "live/",
        }
        assert request.get_method() == "POST"
        assert request.get_header("Content-encoding") == "aes128gcm"
        assert request.get_header("Ttl") == str(webpush.TTL)
        assert request.get_header("Urgency") == "high"
        assert request.get_header("Authorization").startswith("vapid t=")
    # Quiet when ntfy's would be: the household coming home.
    push.send(Notification("Alex at the front door", "Recognized.", priority=2), None)
    assert phones[0].read(service.posted[-2])["silent"] is True
    assert service.posted[-1].get_header("Urgency") == "normal"


def test_a_phone_whose_subscription_ended_is_forgotten_and_other_failures_raised(tmp_path):
    gone, broken, fine = (Phone(f"https://{h}.example/s") for h in ("gone", "broken", "fine"))
    service = PushService({"https://gone.example/s": 410, "https://broken.example/s": 500})
    push = WebPush(tmp_path, opener=service)
    for phone in (gone, broken, fine):
        push.subscribe(phone.subscription)
    with pytest.raises(RuntimeError, match=r"broken\.example: the push service said 500"):
        push.send(Notification("Package at the front door", "Left."), None)
    assert len(service.posted) == 3  # every phone was tried
    assert [p["endpoint"] for p in WebPush(tmp_path)._phones] == [
        "https://broken.example/s",
        "https://fine.example/s",
    ]


def test_the_snapshot_is_at_an_unguessable_address_while_the_notification_is_kept(tmp_path):
    now = [NOW]
    service = PushService()
    push = WebPush(tmp_path, clock=lambda: now[0], opener=service)
    push.send(Notification("Package at the front door", "Left."), b"JPEG")
    assert not push._images  # nobody to send it to: nothing kept
    phone = Phone()
    push.subscribe(phone.subscription)

    push.send(Notification("Visitor at the front door", "Hi"), b"JPEG")
    m = re.fullmatch(r"push/image/([\w-]{24})\.jpg", phone.read(service.posted[-1])["image"])
    assert push.image(m[1]) == b"JPEG"
    assert push.image("guess") is None
    now[0] += webpush.TTL + 1
    assert push.image(m[1]) is None  # as long as the push service keeps the notification

    for _ in range(webpush.MAX_IMAGES + 5):
        push.send(Notification("Visitor at the front door", "Hi"), b"JPEG")
    assert len(push._images) == webpush.MAX_IMAGES
    push.send(Notification("Someone went inside by the front door", "In."), None)
    assert "image" not in phone.read(service.posted[-1])


def test_a_long_message_is_cut_to_fit():
    for message in ("x" * 10_000, "é" * 5_000):
        data = webpush.payload(Notification("Law enforcement at the front door", message))
        assert len(data) <= webpush.MAX_PAYLOAD
        assert json.loads(data)["body"].endswith("…")
    short = webpush.payload(Notification("Package at the front door", "Left."))
    assert json.loads(short)["body"] == "Left."


@pytest.fixture
def site(tmp_path):
    """The web page with the Visits page and the app's notifications on."""
    service = PushService()
    push = WebPush(tmp_path / "logs", opener=service)
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    visits = VisitsPage(
        tmp_path / "events.jsonl",
        Labels(tmp_path / "labels.json"),
        snapshot=lambda event_id: None,
        snapshot_dir=tmp_path,
    )
    server = web.serve(faces, PASSWORD, port=0, visits=visits, push=push)
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    yield base, faces.token, push, service
    server.shutdown()


def call(base, path, body=None, token="", password=PASSWORD):
    headers = {"X-Token": token, "Content-Type": "application/json"}
    if password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"a:{password}".encode()).decode()
    data = body if body is None or isinstance(body, bytes) else json.dumps(body).encode()
    method = "GET" if body is None else "POST"
    req = urllib.request.Request(base + path, data, headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_the_visits_page_has_the_switch(site):
    base, token, push, _ = site
    status, page = call(base, "")
    assert status == 200
    assert f'data-key="{push.public_key}"'.encode() in page
    assert b'<script src="push.js" defer></script>' in page
    # The script and the badge hold nothing private: no login, like the app's other files.
    status, js = call(base, "push.js", password="")
    assert status == 200 and b"pushManager.subscribe" in js
    status, png = call(base, "badge-96.png", password="")
    assert status == 200 and struct.unpack(">II", png[16:24]) == (96, 96)
    status, js = call(base, "sw.js", password="")
    assert b'addEventListener("push"' in js and b'addEventListener("notificationclick"' in js


def test_a_phone_turns_them_on_tests_them_and_turns_them_off(site):
    base, token, push, service = site
    phone = Phone()
    endpoint = {"endpoint": phone.subscription["endpoint"]}
    assert call(base, "push/subscribe", phone.subscription, token, password="")[0] == 401
    assert call(base, "push/subscribe", phone.subscription, "stale")[0] == 403
    assert call(base, "push/subscribe", b"{not json", token)[0] == 400
    assert call(base, "push/subscribe", [phone.subscription], token)[0] == 400
    assert call(base, "push/test", endpoint, token)[0] == 400  # not on yet
    assert not push.active

    assert call(base, "push/subscribe", phone.subscription, token)[0] == 204
    assert push.active
    assert call(base, "push/test", endpoint, token)[0] == 204
    assert phone.read(service.posted[-1])["body"] == "Notifications work on this phone."

    # The snapshot, as the phone's notification fetches it: no login, the address is the key.
    push.send(Notification("Visitor at the front door", "Hi"), b"JPEG")
    image = phone.read(service.posted[-1])["image"]
    assert call(base, image, password="") == (200, b"JPEG")
    assert call(base, "api/hassio_ingress/abc/" + image, password="")[0] == 200
    assert call(base, "push/image/guess.jpg", password="")[0] == 404

    service.answers[endpoint["endpoint"]] = 500
    status, said = call(base, "push/test", endpoint, token)
    assert status == 502 and b"500" in said
    service.answers[endpoint["endpoint"]] = 410  # the phone dropped it
    status, said = call(base, "push/test", endpoint, token)
    assert status == 400 and b"ended" in said and not push.active

    assert call(base, "push/subscribe", phone.subscription, token)[0] == 204
    assert call(base, "push/unsubscribe", endpoint, token)[0] == 204
    assert not push.active


def test_without_them_there_is_no_switch(tmp_path):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    visits = VisitsPage(
        tmp_path / "events.jsonl",
        Labels(tmp_path / "labels.json"),
        snapshot=lambda event_id: None,
        snapshot_dir=tmp_path,
    )
    server = web.serve(faces, PASSWORD, port=0, visits=visits)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/"
        status, page = call(base, "")
        assert status == 200 and b"id=push" not in page
        assert call(base, "push/subscribe", Phone().subscription, faces.token)[0] != 204
    finally:
        server.shutdown()
