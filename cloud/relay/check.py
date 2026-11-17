"""End to end, with SniTun's own relay and client: passes from the name service, a tunnel, a
phone's TLS connection by the hub's name through it, what's refused, and the sweep closing a
tunnel whose pass ran out.

SniTun needs Python 3.13 and Linux, so this runs in the relay's image, not with the other
tests (CI: ``docker run ... python -m relay.check``, with ``cloud/`` on PYTHONPATH).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import sys
import tempfile
import time
from pathlib import Path

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from snitun.client.client_peer import ClientPeer
from snitun.client.connector import EndpointConnector

from hubnames.service import NameService, Relay
from hubnames.store import Store
from relay.__main__ import run, sweep

ZONE = "hubs.example"
KEY = Fernet.generate_key().decode()
failed: list[str] = []


def check(what: str, ok: bool) -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        failed.append(what)


class NoDNS:
    zone = ZONE


def certificate(name: str, folder: Path) -> tuple[Path, Path]:
    """A self-signed certificate for ``name``, standing in for the hub's Let's Encrypt one."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = folder / "cert.pem", folder / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


async def phone(port: int, name: str, cafile: Path) -> str:
    context = ssl.create_default_context(cafile=str(cafile))
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port, ssl=context, server_hostname=name), 10
    )
    writer.write(b"hello hub\n")
    await writer.drain()
    answer = await asyncio.wait_for(reader.readline(), 10)
    writer.close()
    return answer.decode().strip()


async def connect(port: int, page_port: int, p: dict, aes_key: bytes | None = None) -> ClientPeer:
    tunnel = ClientPeer("127.0.0.1", port)
    await tunnel.start(
        EndpointConnector("127.0.0.1", page_port),
        p["token"].encode(),
        aes_key or bytes.fromhex(p["aes_key"]),
        bytes.fromhex(p["aes_iv"]),
        protocol_version=p["protocol_version"],
        cipher=p["cipher"],
    )
    await asyncio.sleep(0.5)
    return tunnel


async def refused(port: int, page_port: int, p: dict, aes_key: bytes | None = None) -> bool:
    try:
        tunnel = await connect(port, page_port, p, aes_key)
    except Exception:  # noqa: BLE001 (SniTun's errors for a refused tunnel)
        return True
    return not tunnel.is_connected


async def main() -> int:
    folder = Path(tempfile.mkdtemp())
    store = Store(folder / "hubnames.db")
    names = NameService(store, NoDNS(), relay=Relay(f"relay.{ZONE}", (KEY,), "secret"))
    hub = store.register(store.new_claims(1)[0], b"k" * 32)
    name = f"{hub.id}.{ZONE}"
    store.set_plus(hub.id, time.time() + 86400)

    # The hub's HTTPS server, with a certificate for its name.
    cert, key = certificate(name, folder)
    tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    tls.load_cert_chain(cert, key)
    seen: list[str] = []

    async def hub_page(reader, writer):
        seen.append((await reader.readline()).decode().strip())
        writer.write(b"hello phone\n")
        await writer.drain()
        writer.close()

    page = await asyncio.start_server(hub_page, "127.0.0.1", 0, ssl=tls)
    page_port = page.sockets[0].getsockname()[1]

    # The relay, as __main__ runs it, on a free port.
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    stop = asyncio.Event()
    servers: list = []
    relay = asyncio.create_task(run([KEY], port, stop, servers.append))
    await asyncio.sleep(0.5)

    p = names.tunnel(hub.id)
    tunnel = await connect(port, page_port, p)
    check("a pass from the name service opens a tunnel", tunnel.is_connected)
    answer = await phone(port, name, cert)
    check("a phone reaches the hub by its name, TLS ending on the hub",
          answer == "hello phone" and seen[-1:] == ["hello hub"])  # fmt: skip
    try:
        await phone(port, f"zzzzzzzzzz.{ZONE}", cert)
        check("another name isn't passed to this hub", False)
    except (OSError, ssl.SSLError, TimeoutError):
        check("another name isn't passed to this hub", True)
    check("a pass without its AES key is refused",
          await refused(port, page_port, names.tunnel(hub.id), b"\0" * 32))  # fmt: skip
    forged = NameService(store, NoDNS(), relay=Relay("r", (Fernet.generate_key().decode(),), "s"))
    check("a pass made with another key is refused",
          await refused(port, page_port, forged.tunnel(hub.id)))  # fmt: skip
    expired = names.tunnel(hub.id)
    content = json.loads(Fernet(KEY).decrypt(expired["token"].encode()))
    content["valid"] = int(time.time()) - 1
    expired["token"] = Fernet(KEY).encrypt(json.dumps(content).encode()).decode()
    check("an expired pass is refused", await refused(port, page_port, expired))

    # The sweep: a tunnel whose pass runs out while it's open is closed.
    await tunnel.stop()
    short = names.tunnel(hub.id)
    content = json.loads(Fernet(KEY).decrypt(short["token"].encode()))
    content["valid"] = int(time.time()) + 2
    short["token"] = Fernet(KEY).encrypt(json.dumps(content).encode()).decode()
    tunnel = await connect(port, page_port, short)
    check("a short pass opens a tunnel", tunnel.is_connected)
    await asyncio.sleep(3)
    closed = sweep(servers[0]._peers)
    await asyncio.sleep(1)
    check("the sweep closes the tunnel once its pass has run out",
          closed == 1 and not tunnel.is_connected)  # fmt: skip

    stop.set()
    await relay
    page.close()
    print("all passed" if not failed else f"{len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
