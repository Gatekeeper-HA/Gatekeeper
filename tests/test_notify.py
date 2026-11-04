import threading
from email.header import decode_header
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from gatekeeper.interaction import Visit
from gatekeeper.notify import Notification, Notifier, NtfyBackend, compose, emergency_links


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
        ("trick_or_treat", "Trick-or-treaters at the front door", 3),
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


def test_notifier_logs_every_attempt(tmp_path):
    import json

    from gatekeeper.eventlog import EventLog

    path = tmp_path / "notifications.jsonl"
    broken, ok = FakeBackend("broken", fail=True), FakeBackend("ok")
    notifier = Notifier([broken, ok], snapshot=lambda v: b"IMG", log_to=EventLog(path))
    notifier.send(visit(classification="likely_delivery", transcript="box", response="OK"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["event_id"], r["backend"], r["ok"], r["snapshot"]) for r in rows] == [
        ("e1", "broken", False, True), ("e1", "ok", True, True),
    ]  # fmt: skip
    assert rows[0]["error"] == "down" and rows[1]["error"] is None
    assert all(r["seconds"] >= 0 for r in rows)


def test_notifier_sends_a_photo_taken_during_the_visit_instead_of_the_snapshot(tmp_path):
    # e.g. a warrant held up to the camera, from the full-resolution stream
    photo = tmp_path / "e1-2.jpg"
    photo.write_bytes(b"WARRANT")
    ok = FakeBackend("ok")
    Notifier([ok], snapshot=lambda v: b"FRIGATE").send(
        visit(classification="law_enforcement", flow="law_enforcement", image=photo)
    )
    assert ok.sent[0][1] == b"WARRANT"


def test_compose_civic_visit_relays_who_and_their_message():
    n = compose(visit(
        classification="civic", flow="civic", transcript="I'm running for city council",
        details={"identity": "Sam Lee, for council", "message": "Please vote on November 3rd"},
        response="Thank you.",
    ))  # fmt: skip
    assert n.title == "Political visitor at the front door"
    assert n.priority == 3
    assert 'Who: "Sam Lee, for council"' in n.message
    assert 'Message: "Please vote on November 3rd"' in n.message


def test_compose_law_enforcement_alert_says_more_is_coming():
    n = compose(visit(
        classification="law_enforcement", flow="law_enforcement", transcript="Police",
        stage="alert",
    ))  # fmt: skip
    assert n.title == "Law enforcement at the front door"
    assert n.priority == 5
    assert "More when it's done." in n.message


def test_ntfy_quick_reply_buttons_post_to_the_reply_topic():
    backend = NtfyBackend(
        "http://ntfy", "doorbell", "tk", reply_url="http://192.168.1.5:8090/doorbell-replies"
    )
    n = Notification(
        "Visitor", "hi", actions=[("I'm coming", "e1 coming 0a1b"), ("Not now", "e1 later 2c3d")]
    )
    assert backend.request(n, None).get_header("Actions") == (
        "http, \"I'm coming\", http://192.168.1.5:8090/doorbell-replies, method=POST, "
        "body=e1 coming 0a1b, clear=true; "
        'http, "Not now", http://192.168.1.5:8090/doorbell-replies, method=POST, '
        "body=e1 later 2c3d, clear=true"
    )
    # Without a reply topic (quick replies off), no buttons.
    plain = NtfyBackend("http://ntfy", "doorbell", "tk")
    assert plain.request(n, None).get_header("Actions") is None


def test_notifier_adds_the_visit_s_buttons():
    sent = []

    class Backend:
        name = "fake"

        def send(self, notification, image):
            sent.append(notification)

    notifier = Notifier([Backend()], actions=lambda v: [("I'm coming", f"{v.event_id} coming x")])
    notifier.send(visit(classification="cooperative_other", response="OK."))
    assert sent[0].actions == [("I'm coming", "e1 coming x")]


def test_compose_package_alerts(monkeypatch):
    monkeypatch.setattr("gatekeeper.notify._clock_time", lambda ts: "14:05")
    left = compose(
        visit(event_id="package-1.0", outcome="package_delivered", details={"since": 1.0})
    )
    assert (left.title, left.message, left.tags) == (
        "Package at the front door",
        "Left at 14:05.",
        ["package"],
    )
    taken = compose(
        visit(event_id="package-1.0", outcome="package_picked_up", details={"since": 1.0})
    )
    assert taken.title == "Package picked up at the front door"
    assert taken.message.startswith("It had been there since 14:05.")
    assert taken.priority == 4


def test_compose_someone_who_went_inside_is_quiet():
    n = compose(visit(outcome="entered", classification="no_response"))
    assert n.title == "Someone went inside by the front door"
    assert n.priority == 2  # low: no sound for the household coming home
    assert n.tags == ["house"]



def test_compose_someone_recognized_is_quiet_and_named():
    n = compose(visit(outcome="household", details={"name": "Alex", "greeted": False}))
    assert n.title == "Alex at the front door"
    assert n.message == "Recognized by face, so not greeted."
    assert n.priority == 2
    n = compose(visit(outcome="household", details={"name": "Alex", "greeted": True}))
    assert n.message == "Recognized by face, after the greeting started."


def test_a_knock_says_so():
    n = compose(visit(trigger="knock", classification="no_response"))
    assert n.tags[0] == "door" and n.message.startswith("Knocked. No answer.")
    during = compose(visit(trigger="knock", outcome="pressed_during_visit"))
    assert during.title == "A knock at the front door" and during.tags == ["door"]
    assert compose(visit(trigger="button", outcome="pressed_during_visit")).title == (
        "Doorbell pressed at the front door"
    )


def test_tapping_the_notification_opens_the_live_page():
    backend = NtfyBackend("http://ntfy", "doorbell", click_url="https://192.0.2.10:8098/live/")
    req = backend.request(Notification("Someone at the front door", "No answer."), None)
    assert req.get_header("Click") == "https://192.0.2.10:8098/live/"
    plain = NtfyBackend("http://ntfy", "doorbell").request(Notification("t", "m"), None)
    assert plain.get_header("Click") is None


def test_the_web_page_address_for_notifications():
    from gatekeeper.config import Settings

    tls = Settings(web_tls=True, web_tls_names="192.0.2.10, gatekeeper.lan")
    assert tls.web_public_url == "https://192.0.2.10:8098/"
    assert Settings(web_tls_names="192.0.2.10").web_public_url == "http://192.0.2.10:8098/"
    assert Settings().web_public_url == ""
    custom = Settings(web_public_url="https://door.example/")
    assert custom.web_public_url == "https://door.example/"


def threat_visit(level, acted=True):
    reasons = ["asked whether anyone's home", "asked to come in"][: max(level, 1)]
    return visit(
        classification="cooperative_other",
        transcript="Is anyone home? Can I come in?",
        response="I've let the resident know you're here. This visit is being recorded.",
        details={"threat": {"level": level, "signs": [], "reasons": reasons, "acted": acted}},
    )


def test_a_suspicious_visit_is_urgent_with_the_reasons():
    n = compose(threat_visit(2))
    assert (n.title, n.priority, n.tags) == ("Suspicious visitor at the front door", 5, ["warning"])
    assert n.message.splitlines()[1] == "Why: asked whether anyone's home; asked to come in."
    n = compose(threat_visit(3))
    assert n.title == "Urgent: threatening visitor at the front door"
    assert (n.priority, n.tags) == (5, ["rotating_light"])


def test_one_sign_is_a_note_and_shadow_changes_nothing():
    n = compose(threat_visit(1))
    assert (n.title, n.priority) == ("Visitor at the front door", 4)
    assert "Note: asked whether anyone's home." in n.message
    n = compose(threat_visit(2, acted=False))
    assert (n.title, n.priority) == ("Visitor at the front door", 4)
    assert "Note:" not in n.message and "Why:" not in n.message


def test_911_buttons_for_a_suspicious_visit_or_an_emergency():
    v = threat_visit(2)
    (text, body), call = emergency_links(v, "12 Elm St")
    assert text == "Text 911" and call == ("Call 911", "tel:911")
    assert body.startswith("sms:911?body=Suspicious%20person%20at%20my%20door%20at%2012%20Elm%20St")
    assert "asked%20whether%20anyone%27s%20home" in body
    assert "," not in body and ";" not in body  # safe in ntfy's Actions header
    # Without an address there's nothing to text: only the call.
    assert emergency_links(v, "") == [("Call 911", "tel:911")]
    # An emergency: what they said.
    fire = visit(classification="emergency", transcript="There's smoke coming out of your garage")
    assert "smoke%20coming" in emergency_links(fire, "12 Elm St")[0][1]
    # Nothing for an ordinary visit, one sign, or a level not acted on (shadow).
    assert emergency_links(visit(classification="likely_delivery"), "12 Elm St") == []
    assert emergency_links(threat_visit(1), "12 Elm St") == []
    assert emergency_links(threat_visit(3, acted=False), "12 Elm St") == []


def test_ntfy_puts_the_911_buttons_first_and_shows_three_at_most():
    backend = NtfyBackend("http://ntfy", "doorbell", "tk", reply_url="http://h:8090/r")
    n = Notification(
        "Suspicious visitor",
        "hi",
        links=[("Text 911", "sms:911?body=x"), ("Call 911", "tel:911")],
        actions=[("I'm coming", "e1 coming 1"), ("Not now", "e1 later 2")],
    )
    assert backend.request(n, None).get_header("Actions") == (
        'view, "Text 911", sms:911?body=x, clear=true; '
        'view, "Call 911", tel:911, clear=true; '
        "http, \"I'm coming\", http://h:8090/r, method=POST, body=e1 coming 1, clear=true"
    )
    # Link buttons don't need the reply topic.
    plain = NtfyBackend("http://ntfy", "doorbell", "tk")
    assert plain.request(n, None).get_header("Actions") == (
        'view, "Text 911", sms:911?body=x, clear=true; view, "Call 911", tel:911, clear=true'
    )


def test_a_package_taken_by_a_visitor_is_urgent_with_what_they_said():
    taken = {"level": 2, "signs": ["package_taken"], "reasons": [], "said": "UPS, return pickup"}
    v = visit(outcome="package_picked_up", details={"since": 0, "threat": {**taken, "acted": True}})
    n = compose(v)
    assert (n.title, n.priority) == ("Package taken by a visitor at the front door", 5)
    assert 'they said: "UPS, return pickup"' in n.message
    assert emergency_links(v, "12 Elm St") == []  # not an emergency in progress
    # In shadow: the usual notification.
    v.details["threat"]["acted"] = False
    assert compose(v).title == "Package picked up at the front door"
