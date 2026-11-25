"""The web page away from home, through Gatekeeper's relay, with Cloud Plus (README, "Away from
home"; the relay: cloud/README.md, "The relay"). The hub keeps a tunnel open to the relay, and a
phone that connects to the hub's name reaches the page through it. TLS still ends here, with
the hub name's own certificate: the relay passes each connection on still encrypted.

- **Passes.** Each time the tunnel connects, the hub asks the name service for a pass (``POST
  /v1/hubs/<id>/tunnel``, signed with the hub name's key): a token only the relay can read, for
  this hub's name, and a new key. Without Cloud Plus, it asks again every ``RECHECK`` seconds.
- **Phones.** Each phone's connection arrives as one of the tunnel's channels, with the phone's
  address. It's handed to the web page's server over a socket pair, so the page sees the
  phone's own address (the login limit, the log) as if the phone had connected directly.
- **The hub's name** points at the relay while the tunnel is up, and back at the home
  address when it closes or Cloud Plus ends: the hub tells the name service (``PUT
  /v1/hubs/<id>/route``). At home too, phones then go through the relay; Live goes direct.
- **Renewal.** A pass lasts an hour. ``RENEW_BEFORE`` its end a new tunnel replaces the old one
  (the relay keeps the newer of two for a name), so phones hardly notice.
- **Not with a weak password.** Through the relay the page faces the internet, so the tunnel
  stays off while ``WEB_PASSWORD`` is shorter than ``MIN_PASSWORD`` characters (app.py).

SniTun's client does the tunnel (``snitun``, the ``remote`` extra).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import socket
import threading
import time
from collections.abc import Callable

from snitun.client.client_peer import ClientPeer
from snitun.client.connector import Connector
from snitun.exceptions import MultiplexerTransportClose
from snitun.multiplexer.channel import ChannelFlowControlBase

from gatekeeper.hubname import Stop

log = logging.getLogger(__name__)

MIN_PASSWORD = 12
RECHECK = 3600.0  # seconds: asking again for a pass the service refused (no Cloud Plus)
RENEW_BEFORE = 300.0  # seconds: a new tunnel this long before the pass runs out
MIN_RENEW = 60.0  # seconds: never sooner than this after connecting
RETRY = (15.0, 60.0, 300.0, 900.0)  # seconds: after the 1st, 2nd... failure in a row
READ_SIZE = 64 * 1024


def strong(password: str) -> bool:
    """A password fit to face the internet."""
    return len(password) >= MIN_PASSWORD


class _Bridge(ChannelFlowControlBase):
    """One phone's connection: between its channel and the web page's end of a socket pair.
    While the tunnel's queue to the relay is full, it stops reading from the page."""

    def __init__(self, channel) -> None:
        super().__init__()
        self._channel = channel

    async def run(self, multiplexer, sock: socket.socket) -> None:
        channel = self._channel
        channel.set_pause_resume_reader_callback(self._pause_resume_reader_callback)
        reader, writer = await asyncio.open_connection(sock=sock)

        async def to_page() -> None:
            while True:
                writer.write(await channel.read())  # MultiplexerTransportClose: phone gone
                await writer.drain()

        async def to_phone() -> None:
            while data := await reader.read(READ_SIZE):
                while (paused := self._pause_future) is not None and not paused.done():
                    await paused
                await channel.write(data)

        page, phone = asyncio.create_task(to_page()), asyncio.create_task(to_phone())
        try:
            await asyncio.wait((page, phone), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (page, phone):
                task.cancel()
            results = await asyncio.gather(page, phone, return_exceptions=True)
            writer.close()
            # The phone hung up (its channel is closed already) or the page did: close the
            # phone's side too.
            if not isinstance(results[0], MultiplexerTransportClose):
                with contextlib.suppress(Exception):
                    multiplexer.delete_channel(channel)


class _ToWebPage(Connector):
    """Each phone's connection, to the web page's server (``hand_over``: its
    ``process_request(socket, (address, port))``)."""

    def __init__(self, hand_over: Callable[[socket.socket, tuple[str, int]], None]) -> None:
        super().__init__()
        self._hand_over = hand_over

    async def _handle_connection(self, multiplexer, channel) -> None:
        ours, theirs = socket.socketpair()
        try:
            self._hand_over(theirs, (str(channel.ip_address), 0))
        except Exception:  # noqa: BLE001 (the page's server; never the tunnel's problem)
            log.exception("relay: the web page didn't take a connection")
            for s in (ours, theirs):
                s.close()
            with contextlib.suppress(Exception):
                multiplexer.delete_channel(channel)
            return
        await _Bridge(channel).run(multiplexer, ours)


class Tunnel:
    """Keeps the tunnel to the relay open while the hub has Cloud Plus. ``service()``: the
    name service with the hub's key (hubname.HubName.service), None until it's registered.
    ``hand_over``: the web page's server's ``process_request``. ``peer``: SniTun's
    ``ClientPeer`` (tests replace it). ``up``: whether the tunnel is open now."""

    def __init__(
        self,
        service: Callable[[], object | None],
        hand_over: Callable[[socket.socket, tuple[str, int]], None],
        clock: Callable[[], float] = time.time,
        peer=ClientPeer,
    ) -> None:
        self._service = service
        self._connector = _ToWebPage(hand_over)
        self._clock = clock
        self._peer = peer
        self._said = ""  # the last state logged: each is logged once
        self._client = None  # the name service, as last asked for a pass
        self._routed = False  # the name points at the relay
        self.up = False
        self.thread: threading.Thread | None = None

    def _say(self, level: int, message: str, *args) -> None:
        text = message % args
        if text != self._said:
            self._said = text
            log.log(level, text)

    async def _pass(self) -> tuple[dict | None, float]:
        """A pass, or None and how long to wait before asking again."""
        client = self._client = self._service()
        if client is None:
            return None, 60.0  # the hub's name isn't registered yet
        try:
            return await asyncio.to_thread(client.tunnel), 0.0
        except Stop as e:  # no Cloud Plus, or no relay
            self._say(logging.INFO, "relay: off (%s); asking again in an hour", e)
            return None, RECHECK

    async def _route(self, via: str) -> None:
        """Point the hub's name at the relay or back home. A failure is logged, and tried
        again with the next pass."""
        if self._client is None or self._routed == (via == "relay"):
            return
        try:
            await asyncio.to_thread(self._client.route, via)
        except Exception as e:  # noqa: BLE001 (the tunnel keeps going either way)
            log.warning("relay: couldn't point the hub's name %s (%s)", via, e)
            return
        self._routed = via == "relay"
        log.info("relay: the hub's name points %s", "at the relay" if self._routed else "home")

    async def _connect(self, p: dict):
        peer = self._peer(p["relay"], p["port"])
        await peer.start(
            self._connector,
            p["token"].encode(),
            bytes.fromhex(p["aes_key"]),
            bytes.fromhex(p["aes_iv"]),
            protocol_version=p["protocol_version"],
            cipher=p["cipher"],
        )
        return peer

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        stopping = asyncio.create_task(stop.wait())
        current = None
        failures = 0

        async def pause(seconds: float) -> None:
            await asyncio.wait((stopping,), timeout=seconds)

        try:
            while not stop.is_set():
                try:
                    p, wait = await self._pass()
                    peer = await self._connect(p) if p is not None else None
                except Exception as e:  # noqa: BLE001 (any failure: try again later)
                    p, peer = None, None
                    wait = RETRY[min(failures, len(RETRY) - 1)]
                    failures += 1
                    self._say(logging.WARNING, "relay: couldn't connect (%s); trying again", e)
                if peer is None:
                    if current is not None:  # Cloud Plus ended, or the relay went away
                        await self._close(current)
                        current, self.up = None, False
                    await self._route("home")
                    await pause(wait)
                    continue
                # The relay keeps the newer of two tunnels for a name: the old one is done.
                if current is not None:
                    await self._close(current)
                current, failures = peer, 0
                if self.up:
                    log.debug("relay: tunnel renewed, its pass until %s", _time(p["until"]))
                else:
                    self.up = True
                    self._say(logging.INFO, "relay: tunnel up through %s", p["relay"])
                await self._route("relay")
                renew = max(MIN_RENEW, p["until"] - RENEW_BEFORE - self._clock())
                closed = asyncio.ensure_future(peer.wait())
                await asyncio.wait((closed, stopping), timeout=renew,
                                   return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                if not closed.done():
                    closed.cancel()  # time for a new pass, or stopping
                elif not stop.is_set():
                    await self._close(current)
                    current, self.up = None, False
                    failures += 1
                    self._say(logging.WARNING, "relay: the tunnel closed; connecting again")
                    await self._route("home")
                    await pause(RETRY[0])
        finally:
            stopping.cancel()
            self.up = False
            if current is not None:
                await self._close(current)

    @staticmethod
    async def _close(peer) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(peer.stop(), 10)

    def start(self) -> Tunnel:
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.run()), name="relay", daemon=True
        )
        self.thread.start()
        return self


def _time(when: float) -> str:
    return datetime.datetime.fromtimestamp(when).strftime("%H:%M")
