"""Run the relay: ``python -m relay``.

Settings, from the environment:
    RELAY_FERNET_KEYS  the keys shared with the name service, comma-separated (required): the
                       passes are made with them
    RELAY_PORT         the port (default 8443; compose publishes it as 443)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable

SWEEP_SECONDS = 60
COUNT_EVERY = 10  # sweeps: a count of open tunnels every 10 minutes

log = logging.getLogger("relay")


def sweep(peers) -> int:
    """Close the tunnels whose pass has run out; how many. ``peers`` is SniTun's
    ``PeerManager``: its ``_peers`` maps each hub's name to its tunnel (``Peer``), whose
    ``is_valid`` is false once the pass's time is up. The hub then connects again with a new
    pass, which the name service gives only while it has Cloud Plus."""
    closed = 0
    for peer in list(peers._peers.values()):
        if not peer.is_valid and peer.multiplexer is not None:
            peer.multiplexer.shutdown()
            closed += 1
    return closed


async def run(
    keys: list[str],
    port: int,
    stop: asyncio.Event | None = None,
    started: Callable[[object], None] | None = None,
) -> None:
    """Serve until ``stop``. ``started`` gets SniTun's server once it listens (a test's)."""
    from snitun.server.run import SniTunServerSingle

    server = SniTunServerSingle(keys, port=port)  # every address, IPv4 and IPv6
    await server.start()
    log.info("relay on :%d", port)
    if started is not None:
        started(server)
    stop = stop or asyncio.Event()
    sweeps = 0
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), SWEEP_SECONDS)
            except TimeoutError:
                pass
            if closed := sweep(server._peers):
                log.info("closed %d tunnel(s) whose pass had run out", closed)
            sweeps += 1
            if sweeps % COUNT_EVERY == 0:
                log.info("%d tunnel(s) open", len(server._peers._peers))
    finally:
        await server.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s"
    )
    # SniTun logs each tunnel and connection with names and addresses: only its warnings.
    logging.getLogger("snitun").setLevel(logging.WARNING)
    keys = [k.strip() for k in os.environ.get("RELAY_FERNET_KEYS", "").split(",") if k.strip()]
    if not keys:
        sys.exit("RELAY_FERNET_KEYS isn't set")
    asyncio.run(run(keys, int(os.environ.get("RELAY_PORT", "8443"))))


if __name__ == "__main__":
    main()
