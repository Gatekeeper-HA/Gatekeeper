"""HTTPS for the web page (``WEB_TLS``), with a certificate of its own.

Browsers give the microphone (talking back from the Live page) only to an HTTPS page.
Gatekeeper makes a self-signed certificate once and keeps it next to the visit log, so a
browser asks to trust it once, not at every restart. It names the addresses the page is
reached at (``WEB_TLS_NAMES``, e.g. the server's IP); it's made again only when those
change. Installing it as trusted on a phone stops the warning altogether.

With a hub name (hubname.py), the page is also reached at that name, with a Let's Encrypt
certificate the phone already trusts (``NamedCertificate``).
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import ssl
from pathlib import Path

log = logging.getLogger(__name__)

YEARS = 10


def _names(names: list[str]) -> list:
    from cryptography import x509

    out = []
    for name in dict.fromkeys([*names, "localhost", "gatekeeper"]):
        try:
            out.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            out.append(x509.DNSName(name))
    return out


def _has_names(cert_file: Path, wanted: list) -> bool:
    from cryptography import x509

    try:
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        have = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:
        return False
    return set(have) == set(wanted)


def certificate(folder: Path, names: list[str]) -> tuple[Path, Path]:
    """The certificate and key files in ``folder``, made if missing or naming other
    addresses than ``names``."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_file, key_file = folder / "web-cert.pem", folder / "web-key.pem"
    wanted = _names(names)
    if key_file.exists() and _has_names(cert_file, wanted):
        return cert_file, key_file
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gatekeeper")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * YEARS))
        .add_extension(x509.SubjectAlternativeName(wanted), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("made the web page's certificate for %s", ", ".join(dict.fromkeys(names)) or "-")
    return cert_file, key_file


def server_context(cert_file: Path, key_file: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


class NamedCertificate:
    """A certificate for one name (the hub's name, hubname.py), served to browsers that ask
    for that name (SNI) instead of ``base``'s self-signed one. Anything else, like the
    server's IP address, still gets the self-signed one. ``use`` swaps a renewed
    certificate in for new connections, without a restart."""

    def __init__(self, base: ssl.SSLContext) -> None:
        self._current: tuple[str, ssl.SSLContext] | None = None
        base.sni_callback = self._pick

    def _pick(self, sock: ssl.SSLObject, server_name: str | None, _base: ssl.SSLContext) -> None:
        current = self._current
        if current is not None and server_name and server_name.lower() == current[0]:
            sock.context = current[1]

    def use(self, cert_file: Path, key_file: Path, name: str) -> None:
        self._current = (name.lower(), server_context(cert_file, key_file))
