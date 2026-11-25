"""The web page away from home through the relay (tunnel.py)."""

import asyncio
import base64
import logging
import socket
import ssl
import sys
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("snitun")

from cryptography.fernet import Fernet  # noqa: E402

from gatekeeper import app, tls, tunnel, web  # noqa: E402
from gatekeeper.hubname import Stop  # noqa: E402
from gatekeeper.logins import Guard  # noqa: E402

PASSWORD = "a long enough password"
NO_PLUS = "the name service refused: this hub has no Cloud Plus"


def test_a_password_fit_for_the_internet():
    assert not tunnel.strong("porch-light")  # 11 characters
    assert tunnel.strong("porch-lights")
    assert tunnel.strong(PASSWORD)


def test_the_tunnel_needs_a_hub_name_relay_on_and_a_strong_password(monkeypatch, caplog):
    started = []

    class Tunnel:
        def __init__(self, service, hand_over):
            started.append((service, hand_over))

        def start(self):
            return self

    monkeypatch.setattr(tunnel, "Tunnel", Tunnel)
    hub_name = SimpleNamespace(service=lambda: None)
    server = SimpleNamespace(process_request=lambda sock, address: None)

    def settings(password=PASSWORD, relay=True):
        return SimpleNamespace(web_password=SimpleNamespace(get_secret_value=lambda: password),
                               relay=relay)  # fmt: skip

    assert app.start_tunnel(settings(), None, server) is None  # no hub name
    assert app.start_tunnel(settings(relay=False), hub_name, server) is None  # RELAY=0
    assert app.start_tunnel(settings("porch-light"), hub_name, server) is None
    assert "relay: off: the web page's password is shorter than 12 characters" in caplog.text
    assert not started
    assert app.start_tunnel(settings(), hub_name, server) is not None
    assert started == [(hub_name.service, server.process_request)]


class Peer:
    """SniTun's ClientPeer, standing in: what it was started with, and closing on demand."""

    started = []

    def __init__(self, host, port):
        self.where = (host, port)
        self.closed = asyncio.get_running_loop().create_future()

    async def start(self, connector, token, aes_key, aes_iv, protocol_version, cipher):
        Peer.started.append((self.where, token, len(aes_key), protocol_version, cipher))

    def wait(self):
        return self.closed

    async def stop(self):
        if not self.closed.done():
            self.closed.set_result(None)


def a_pass(until):
    return {"relay": "relay.hubs.example", "port": 443, "token": "gAAAA-token",
            "aes_key": "00" * 32, "aes_iv": "00" * 16, "protocol_version": 2,
            "cipher": "aes-gcm-siv", "until": until}  # fmt: skip


def test_without_cloud_plus_it_asks_again_later_then_connects(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="gatekeeper.tunnel")
    monkeypatch.setattr(tunnel, "RECHECK", 0.05)
    answers = [Stop(NO_PLUS), Stop(NO_PLUS), a_pass(time.time() + 3600)]

    class Service:
        def tunnel(self):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    Peer.started = []

    async def main():
        t = tunnel.Tunnel(lambda: Service(), lambda sock, address: None, peer=Peer)
        stop = asyncio.Event()
        task = asyncio.create_task(t.run(stop))
        for _ in range(100):
            if t.up:
                break
            await asyncio.sleep(0.02)
        up = t.up
        stop.set()
        await task
        return up

    assert asyncio.run(main())
    assert Peer.started == [(("relay.hubs.example", 443), b"gAAAA-token", 32, 2, "aes-gcm-siv")]
    off = [r.getMessage() for r in caplog.records if r.getMessage().startswith("relay: off")]
    assert off == [f"relay: off ({NO_PLUS}); asking again in an hour"]  # once, not each time
    assert "relay: tunnel up through relay.hubs.example" in caplog.text


@pytest.mark.skipif(sys.platform == "win32", reason="SniTun's relay runs on Linux only")
def test_a_phone_reaches_the_page_through_the_relay(tmp_path, monkeypatch, caplog):
    """End to end: SniTun's own relay and client, the name service's passes, and the web
    page's server with its TLS. The phone connects over IPv6, so its address can't be
    mistaken for the tunnel's own (127.0.0.1)."""
    from snitun.server.run import SniTunServerSingle

    from hubnames.service import NameService, Relay
    from hubnames.store import Store

    caplog.set_level(logging.INFO, logger="gatekeeper.tunnel")
    zone = "hubs.example"
    key = Fernet.generate_key().decode()
    store = Store(tmp_path / "hubnames.db")
    relay_settings = Relay(f"relay.{zone}", (key,), "turn-secret")
    names = NameService(store, SimpleNamespace(zone=zone), relay=relay_settings)
    hub = store.register(store.new_claims(1)[0], b"k" * 32)
    name = f"{hub.id}.{zone}"
    store.set_plus(hub.id, time.time() + 86400)

    probe = socket.socket(socket.AF_INET6)
    probe.bind(("::1", 0))
    port = probe.getsockname()[1]
    probe.close()

    class Service:
        def tunnel(self):
            return {**names.tunnel(hub.id), "relay": "::1", "port": port}

    guard = Guard()
    server = web.serve(
        web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14),
        PASSWORD,
        port=0,
        tls=tls.server_context(*tls.certificate(tmp_path / "web-tls", [name])),
        hosts=web.Hosts([name]),
        guard=guard,
    )
    # A new tunnel every half second: renewals must go unnoticed.
    monkeypatch.setattr(tunnel, "RENEW_BEFORE", 3600.0)
    monkeypatch.setattr(tunnel, "MIN_RENEW", 0.5)
    phone_tls = ssl.create_default_context()
    phone_tls.check_hostname, phone_tls.verify_mode = False, ssl.CERT_NONE

    async def ask(path, auth=None):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("::1", port, ssl=phone_tls, server_hostname=name), 10
        )
        head = f"GET {path} HTTP/1.0\r\nHost: {name}\r\n"
        if auth:
            head += "Authorization: Basic " + base64.b64encode(auth.encode()).decode() + "\r\n"
        writer.write((head + "\r\n").encode())
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        return answer

    async def main():
        relay = SniTunServerSingle([key], host="::1", port=port)
        await relay.start()
        stop = asyncio.Event()
        t = tunnel.Tunnel(lambda: Service(), server.process_request)
        running = asyncio.create_task(t.run(stop))
        try:
            for _ in range(100):
                if t.up:
                    break
                await asyncio.sleep(0.05)
            assert t.up
            page = await ask("/login")
            assert page.startswith(b"HTTP/1.0 200") and b"type=password" in page
            # The phone's own address reaches the login limit, not the tunnel's.
            for _ in range(Guard.PER_ADDRESS):
                await ask("/", auth="a:guess")
            assert guard.wait("::1") > 0 and guard.wait("127.0.0.1") == 0
            # Through several renewals, a phone keeps getting through.
            for _ in range(6):
                await asyncio.sleep(0.3)
                assert (await ask("/login")).startswith(b"HTTP/1.0 200")
            assert len(relay._peers._peers) == 1
        finally:
            stop.set()
            await running
            await relay.stop()
            server.shutdown()
            server.server_close()

    asyncio.run(main())
    assert caplog.text.count("relay: tunnel up through") == 1  # renewals aren't logged
