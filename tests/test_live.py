import base64
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gatekeeper import web
from gatekeeper.live import LivePage

PASSWORD = "porch-light"
OFFER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"


class Go2rtc:
    """go2rtc's API as far as the Live page uses it: POST /api/webrtc?src=<stream>."""

    def __init__(self, streams=("front_door_live", "front_door_live_hd")):
        self.asked = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                src = self.path.split("src=", 1)[-1]
                outer.asked.append((src, body))
                if src not in streams:
                    self.send_response(404)
                    self.end_headers()
                    return
                answer = f"v=0\r\ns=answer for {src}\r\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/sdp")
                self.send_header("Content-Length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def go2rtc():
    g = Go2rtc()
    yield g
    g.server.shutdown()


def test_the_offer_goes_to_go2rtc_for_the_stream_asked(go2rtc):
    live = LivePage(go2rtc.url, "front_door_live", "front_door_live_hd")
    assert live.offer(OFFER) == "v=0\r\ns=answer for front_door_live\r\n"
    assert live.offer(OFFER, hd=True).endswith("answer for front_door_live_hd\r\n")
    assert go2rtc.asked == [("front_door_live", OFFER), ("front_door_live_hd", OFFER)]
    with pytest.raises(ValueError, match="isn't a WebRTC offer"):
        live.offer("<script>")
    with pytest.raises(ValueError, match="isn't available"):
        LivePage(go2rtc.url, "no_such_stream", "x").offer(OFFER)


def test_the_live_page_on_the_web(tmp_path, go2rtc):
    faces = web.FacesPage(tmp_path / "faces", enabled=True, candidate_days=14)
    live = LivePage(go2rtc.url, "front_door_live", "front_door_live_hd")
    server = web.serve(faces, PASSWORD, port=0, live=live)
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    auth = "Basic " + base64.b64encode(f"x:{PASSWORD}".encode()).decode()

    def request(path, data=None, headers=None):
        req = urllib.request.Request(
            base + path, data=data, headers={"Authorization": auth, **(headers or {})}
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    try:
        status, body = request("live/")
        assert status == 200 and "<h1>Live</h1>" in body and "<video id=video" in body
        assert f'data-token="{faces.token}"' in body and "<strong>Live</strong>" in body
        token = {"X-Token": faces.token, "Content-Type": "application/sdp"}
        assert request("live/offer?hd=1", OFFER.encode(), token) == (
            200,
            "v=0\r\ns=answer for front_door_live_hd\r\n",
        )
        status, body = request("live/offer", OFFER.encode(), {"X-Token": "stale"})
        assert status == 403 and "reload" in body
        status, body = request("live/offer", b"hello", token)
        assert status == 400 and "isn't a WebRTC offer" in body
        req = urllib.request.Request(base + "live/offer", data=OFFER.encode(), headers=token)
        with pytest.raises(urllib.error.HTTPError) as no_password:
            urllib.request.urlopen(req)
        assert no_password.value.code == 401
    finally:
        server.shutdown()


def test_the_live_streams_default_to_the_cameras():
    from gatekeeper.config import Settings

    s = Settings(camera_name="back_door")
    assert (s.live_stream, s.live_hd_stream) == ("back_door_live", "back_door_live_hd")
