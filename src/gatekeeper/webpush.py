"""Notifications through the app (web push, RFC 8030), a notify.py backend next to ntfy.

A phone turns them on from the Visits page (the app's ``push.js``): its browser gives
Gatekeeper an address at its push service (Google's for Chrome, Apple's, Mozilla's) and
two keys. Each notification is encrypted for that phone (RFC 8291), so the push service
passes it on without being able to read it, and is signed with Gatekeeper's own key
(VAPID, RFC 8292). There's no account to make: the phone's subscription is the address.

The phones are kept in ``web-push.json`` next to the visit log, and the key in
``web-push-key.pem``: a new key would end every phone's subscription.

A message holds at most 4 KB, so the visitor's snapshot isn't in it: the phone fetches it
from ``push/image/<id>.jpg``, an unguessable address kept in memory for as long as the
notification is (``TTL``), and served without logging in (the phone's notification
fetches it without the app's login).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# Push services take messages of up to 4096 bytes. The header (86 bytes), the
# encryption's tag (16) and the end-of-message byte leave this for the notification.
MAX_PAYLOAD = 4096 - 86 - 16 - 1
RECORD_SIZE = 4096
# An undelivered notification (the phone off) is dropped after this, as ntfy's are.
TTL = 12 * 3600
MAX_PHONES = 20
MAX_IMAGES = 30  # the snapshots kept for the notifications, newest
# Who runs the sender, for the push service (RFC 8292's "sub"; Apple's requires one).
CONTACT = "https://github.com/Gatekeeper-HA/Gatekeeper"
TEST = {
    "title": "Gatekeeper",
    "body": "Notifications work on this phone.",
    "tag": "test",
    "url": "./",
}


class Gone(Exception):
    """The phone's subscription has ended (the app uninstalled, notifications turned off)."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def encrypt(
    plaintext: bytes, ua_public: bytes, auth_secret: bytes, server_key=None, salt: bytes = b""
) -> bytes:
    """``plaintext`` encrypted for one phone (RFC 8291, one aes128gcm record): the body
    to post. ``server_key`` and ``salt`` are new each time unless given."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    as_public = _raw(server_key.public_key())
    phone = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = server_key.exchange(ec.ECDH(), phone)

    def hkdf(salt: bytes, info: bytes, length: int, key: bytes) -> bytes:
        return HKDF(hashes.SHA256(), length, salt, info).derive(key)

    ikm = hkdf(auth_secret, b"WebPush: info\0" + ua_public + as_public, 32, shared)
    cek = hkdf(salt, b"Content-Encoding: aes128gcm\0", 16, ikm)
    nonce = hkdf(salt, b"Content-Encoding: nonce\0", 12, ikm)
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def vapid(endpoint: str, key, now: float) -> str:
    """The Authorization header for a phone's ``endpoint`` (RFC 8292): a token for that
    push service, good for 12 hours, signed with ``key``, and the key's public half."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    url = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{url.scheme}://{url.netloc}", "exp": int(now) + 12 * 3600, "sub": CONTACT}
    unsigned = ".".join(
        b64(json.dumps(part, separators=(",", ":")).encode())
        for part in ({"typ": "JWT", "alg": "ES256"}, claims)
    )
    r, s = decode_dss_signature(key.sign(unsigned.encode(), ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={unsigned}.{b64(signature)}, k={b64(_raw(key.public_key()))}"


def server_key(path: Path):
    """Gatekeeper's push key, made once: the phones' subscriptions are tied to it."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(0o600)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    log.info("made the key for the app's notifications")
    return key


def phone(subscription) -> dict:
    """What's kept of a phone's subscription (the browser's ``PushSubscription``, as
    JSON); ValueError if it isn't one."""
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        endpoint = subscription["endpoint"]
        p256dh, auth = subscription["keys"]["p256dh"], subscription["keys"]["auth"]
        url = urllib.parse.urlsplit(endpoint)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(p256dh))
        ok = url.scheme == "https" and url.netloc and len(endpoint) <= 1024
        ok = ok and len(unb64(auth)) == 16
    except (KeyError, TypeError, ValueError, AttributeError):
        ok = False
    if not ok:
        raise ValueError("That isn't a push subscription.")
    return {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}


def payload(notification, image: str = "") -> bytes:
    """What the app's service worker shows (sw.js): the title and message, the visit's
    tag (its next notification replaces this one), quiet when ntfy's would be (priority
    2 or less), the page a tap opens (Live, like ntfy's) and the snapshot's address."""

    def encoded(message: str) -> bytes:
        return json.dumps(
            {
                "title": notification.title,
                "body": message,
                "tag": notification.tag,
                "silent": notification.priority <= 2,
                "url": "live/",
                **({"image": image} if image else {}),
            },
            ensure_ascii=False,
        ).encode()

    data, cut = encoded(notification.message), len(notification.message)
    while len(data) > MAX_PAYLOAD and cut:
        cut = cut * 3 // 4
        data = encoded(notification.message[:cut].rstrip() + "…")
    return data


class WebPush:
    """The phones that turned the app's notifications on, and sending to them: a
    notify.py backend. ``opener`` is urllib's ``urlopen`` (tests replace it)."""

    name = "web push"

    def __init__(
        self,
        folder: Path,
        clock: Callable[[], float] = time.time,
        timeout: float = 10.0,
        opener: Callable = urllib.request.urlopen,
    ) -> None:
        self.file = folder / "web-push.json"
        self.key = server_key(folder / "web-push-key.pem")
        self.public_key = b64(_raw(self.key.public_key()))  # for the page's switch
        self._clock = clock
        self._timeout = timeout
        self._open = opener
        self._lock = threading.Lock()
        self._phones = self._load()
        self._images: dict[str, tuple[float, bytes]] = {}  # id -> (until, the JPEG)

    @property
    def active(self) -> bool:
        """Any phone to send to (notify.Notifier skips this backend without one)."""
        return bool(self._phones)

    def _load(self) -> list[dict]:
        try:
            kept = json.loads(self.file.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("couldn't read %s: %s", self.file, e)
            return []
        out = []
        for p in kept if isinstance(kept, list) else []:
            try:
                out.append({**phone({"endpoint": p["endpoint"], "keys": p}), "since": p["since"]})
            except (KeyError, TypeError, ValueError):
                pass
        return out

    def _save(self) -> None:
        part = self.file.with_suffix(".part")
        part.touch(0o600)
        part.write_text(json.dumps(self._phones))
        part.replace(self.file)

    def subscribe(self, subscription) -> None:
        """A phone turned notifications on (again: a phone is kept once)."""
        new = phone(subscription)
        with self._lock:
            known = any(p["endpoint"] == new["endpoint"] for p in self._phones)
            self._phones = [p for p in self._phones if p["endpoint"] != new["endpoint"]]
            self._phones = [*self._phones, {**new, "since": self._clock()}][-MAX_PHONES:]
            self._save()
            count = len(self._phones)
        if not known:
            log.info("web push: a phone turned notifications on (%d in all)", count)

    def unsubscribe(self, endpoint: str, why: str = "turned notifications off") -> None:
        with self._lock:
            kept = [p for p in self._phones if p["endpoint"] != endpoint]
            if len(kept) == len(self._phones):
                return
            self._phones = kept
            self._save()
        log.info("web push: a phone %s (%d left)", why, len(kept))

    def image(self, image_id: str) -> bytes | None:
        """A notification's snapshot (``push/image/<id>.jpg``), while it's kept."""
        with self._lock:
            until, jpeg = self._images.get(image_id, (0.0, b""))
        return jpeg if until > self._clock() else None

    def _keep(self, jpeg: bytes) -> str:
        now = self._clock()
        image_id = secrets.token_urlsafe(18)
        with self._lock:
            kept = [(k, v) for k, v in self._images.items() if v[0] > now]
            self._images = dict(kept[-(MAX_IMAGES - 1) :])
            self._images[image_id] = (now + TTL, jpeg)
        return image_id

    def send(self, notification, image: bytes | None) -> None:
        """To every phone, with the snapshot's address. A phone whose subscription ended
        is forgotten; any other failure is raised once all were tried."""
        if not self._phones:
            return
        data = payload(notification, f"push/image/{self._keep(image)}.jpg" if image else "")
        urgency = "high" if notification.priority >= 4 else "normal"
        errors = []
        for p in list(self._phones):
            try:
                self._push(p, data, urgency)
            except Gone:
                self.unsubscribe(p["endpoint"], "has no subscription any more")
            except Exception as e:
                errors.append(f"{urllib.parse.urlsplit(p['endpoint']).netloc}: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def test(self, endpoint: str) -> None:
        """A test notification to one phone (the Visits page's button)."""
        p = next((p for p in self._phones if p["endpoint"] == endpoint), None)
        if p is None:
            raise ValueError("Notifications aren't on for this phone: turn them on again.")
        try:
            self._push(p, json.dumps(TEST).encode(), "normal")
        except Gone:
            self.unsubscribe(endpoint, "has no subscription any more")
            raise ValueError("This phone's subscription has ended: turn them on again.") from None

    def _push(self, p: dict, data: bytes, urgency: str) -> None:
        body = encrypt(data, unb64(p["p256dh"]), unb64(p["auth"]))
        headers = {
            "Authorization": vapid(p["endpoint"], self.key, self._clock()),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL),
            "Urgency": urgency,
        }
        request = urllib.request.Request(p["endpoint"], body, headers, method="POST")
        try:
            with self._open(request, timeout=self._timeout):
                pass
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                raise Gone from e
            raise RuntimeError(f"the push service said {e.code} {e.reason}") from e
