import threading
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gatekeeper.interaction import Visit
from gatekeeper.notify import Notification, Notifier, NtfyBackend, compose, frigate_snapshot


def visit(**kw):
    base = {"event_id": "e1", "camera": "front_door", "outcome": "completed"}
    return Visit(**{**base, **kw})


@pytest.mark.parametrize(
    ("classification", "title", "priority"),
    [
        ("likely_delivery", "Delivery at the front door", 3),
        ("solicitor", "Solicitor at the front door", 2),
        ("service_visit", "Service visit at the front door", 4),
        ("cooperative_other", "Visitor at the front door", 4),
        ("no_response", "Someone at the front door", 3),
    ],
)
def test_compose_answered(classification, title, priority):
    n = compose(visit(classification=classification, transcript="hi there", response="OK."))
    assert n.title == title
    assert n.priority == priority
    assert n.message == 'Said: "hi there"\nReplied: OK.'


def test_compose_no_answer():
    n = compose(visit(classification="no_response", transcript="", response="Leave."))
    assert n.message == "No answer.\nReplied: Leave."


def test_compose_failed_visit():
    n = compose(visit(outcome="talkback_failed"))
    assert n.title == "Someone at the front door"
    assert "speaker didn't connect" in n.message
    assert n.tags == ["warning"]


class Recorder(BaseHTTPRequestHandler):
    requests: list = []

    def _record(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Recorder.requests.append((self.command, self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_PUT = _record

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"JPEGDATA")

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_ntfy_with_snapshot_puts_the_image(server):
    n = Notification("Delivery at the front door", 'Said: "Paquete, señor"\nReplied: OK.',
                     ["package"], 3)  # fmt: skip
    NtfyBackend(server, "doorbell", token="tk_abc").send(n, b"JPEG")
    method, path, headers, body = Recorder.requests[0]
    assert (method, path, body) == ("PUT", "/doorbell", b"JPEG")
    assert headers["Authorization"] == "Bearer tk_abc"
    assert headers["Filename"] == "visitor.jpg"
    assert headers["Tags"] == "package" and headers["Priority"] == "3"
    text, charset = decode_header(headers["Message"])[0]
    assert text.decode(charset) == 'Said: "Paquete, señor"\\nReplied: OK.'


def test_ntfy_without_snapshot_posts_text(server):
    NtfyBackend(server, "doorbell").send(Notification("T", "line1\nline2"), None)
    method, path, headers, body = Recorder.requests[0]
    assert (method, path, body) == ("POST", "/doorbell", b"line1\nline2")
    assert "Authorization" not in headers


def test_frigate_snapshot(server):
    assert frigate_snapshot(server, visit()) == b"JPEGDATA"
    assert frigate_snapshot("http://127.0.0.1:1", visit(), timeout=0.5) is None


def test_press_without_frigate_event_uses_latest_frame(server, monkeypatch):
    import gatekeeper.notify as notify

    urls = []
    real = notify._fetch

    def spy(url, timeout):
        urls.append(url.split("?")[0].removeprefix(server))
        return real(url, timeout)

    monkeypatch.setattr(notify, "_fetch", spy)
    assert frigate_snapshot(server, visit(event_id="press-1.0")) == b"JPEGDATA"
    assert urls == ["/api/front_door/latest.jpg"]


def test_compose_press_during_visit():
    n = compose(visit(outcome="pressed_during_visit", trigger="button"))
    assert n.title == "Doorbell pressed at the front door"
    assert n.tags == ["bell"] and n.priority == 4


def test_compose_button_visit_gets_a_bell():
    n = compose(visit(classification="likely_delivery", transcript="box", response="OK",
                      trigger="button"))  # fmt: skip
    assert n.tags == ["bell", "package"]


class FakeBackend:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.sent = []

    def send(self, n, image):
        if self.fail:
            raise OSError("down")
        self.sent.append((n.title, image))


def test_notifier_sends_to_every_backend_even_if_one_fails():
    broken, ok = FakeBackend("broken", fail=True), FakeBackend("ok")
    notifier = Notifier([broken, ok], snapshot=lambda eid: b"IMG")
    notifier.send(visit(classification="likely_delivery", transcript="box", response="Thanks"))
    assert ok.sent == [("Delivery at the front door", b"IMG")]


def test_notifier_runs_in_the_background_on_a_copy():
    ok = FakeBackend("ok")
    gate = threading.Event()

    def slow_snapshot(event_id):
        gate.wait(5)
        return None

    notifier = Notifier([ok], snapshot=slow_snapshot)
    v = visit(classification="solicitor", transcript="selling", response="No.")
    notifier(v)  # returns immediately
    v.classification = "changed later"
    assert ok.sent == []
    gate.set()
    notifier._executor.shutdown(wait=True)
    assert ok.sent == [("Solicitor at the front door", None)]


def test_notifier_without_backends_does_nothing():
    Notifier([])(visit())


def test_compose_when_the_reply_could_not_be_played():
    # 2026-10-03 go2rtc-stop test: the message read "Replied: None".
    n = compose(visit(classification="no_response", transcript="Hello?", response=None,
                      outcome="reply_failed"))  # fmt: skip
    assert n.message == 'Said: "Hello?"\nCouldn\'t reply: the doorbell speaker didn\'t connect.'
