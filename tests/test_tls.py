import base64
import ssl
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402

from gatekeeper import tls, web  # noqa: E402

PASSWORD = "porch-light"


def names_in(cert_file):
    cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
    return set(map(str, cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value))


def test_the_certificate_is_made_once_for_its_names(tmp_path):
    cert, key = tls.certificate(tmp_path / "tls", ["192.0.2.10"])
    assert "<IPAddress(value=192.0.2.10)>" in names_in(cert)
    assert "<DNSName(value='localhost')>" in names_in(cert)
    first = cert.read_bytes()
    assert tls.certificate(tmp_path / "tls", ["192.0.2.10"])[0].read_bytes() == first  # kept
    tls.certificate(tmp_path / "tls", ["192.0.2.20"])  # the server moved: made again
    assert cert.read_bytes() != first and "<IPAddress(value=192.0.2.20)>" in names_in(cert)


def test_the_pages_over_https(tmp_path):
    context = tls.server_context(*tls.certificate(tmp_path / "tls", ["127.0.0.1"]))
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    server = web.serve(faces, PASSWORD, port=0, tls=context)
    port = server.server_address[1]
    auth = "Basic " + base64.b64encode(f"x:{PASSWORD}".encode()).decode()
    trust = ssl.create_default_context(cafile=str(tmp_path / "tls" / "web-cert.pem"))
    try:
        req = urllib.request.Request(f"https://127.0.0.1:{port}/", headers={"Authorization": auth})
        with urllib.request.urlopen(req, context=trust) as r:
            assert r.status == 200 and b"Household faces" in r.read()

        # An old http:// bookmark is sent to https://.
        class Keep(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with pytest.raises(urllib.error.HTTPError) as moved:
            urllib.request.build_opener(Keep).open(f"http://127.0.0.1:{port}/faces/?x=1")
        assert moved.value.code == 301
        assert moved.value.headers["Location"] == f"https://127.0.0.1:{port}/faces/?x=1"
    finally:
        server.shutdown()
