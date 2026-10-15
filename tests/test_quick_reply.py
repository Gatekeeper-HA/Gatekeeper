import io
import json

import pytest

from conftest import FakeClock
from gatekeeper.interaction import Visit
from gatekeeper.quick_reply import NtfyReplies, QuickReplies

EID = "1792033159.758298-y9p87o"


def quick(clock, said, **kw):
    return QuickReplies(
        ("coming", "later", "no_thanks"),
        300.0,
        say=lambda *args: said.append(args),
        clock=clock,
        spawn=lambda fn: fn(),
        **kw,
    )


def notified(q, event_id=EID, **kw):
    q.notified(Visit(event_id, "front_door", **kw))


def test_a_button_says_its_reply_for_its_visit():
    clock, said = FakeClock(1000.0), []
    q = quick(clock, said)
    notified(q)
    buttons = q.buttons(Visit(EID, "front_door"))
    assert [label for label, _ in buttons] == ["I'm coming", "Not now", "No thanks"]

    clock.now += 40
    q.from_ntfy(buttons[0][1])
    # 40 s after the notification, 260 s left to say it.
    assert said == [(EID, "coming", 40.0, 260.0)]


def test_only_a_notification_button_works():
    clock, said = FakeClock(), []
    q = quick(clock, said)
    notified(q)
    q.from_ntfy(f"{EID} coming 0123456789abcdef")  # someone guessing the signature
    q.from_ntfy("coming")
    other = quick(clock, said).buttons(Visit(EID, "front_door"))[0][1]
    q.from_ntfy(other)  # signed before a restart (another key)
    assert said == []


def test_late_and_unknown_visits_are_handed_on_as_too_late():
    clock, said = FakeClock(1000.0), []
    q = quick(clock, said)
    notified(q)
    clock.now += 301
    assert q.request(EID, "later")
    assert q.request("press-999.0", "coming")  # never notified (e.g. before a restart)
    assert said == [(EID, "later", 301.0, -1.0), ("press-999.0", "coming", None, 0.0)]


def test_each_reply_is_said_once_per_visit():
    clock, said = FakeClock(), []
    q = quick(clock, said)
    notified(q)
    assert q.request(EID, "coming")
    assert not q.request(EID, "coming")  # a double tap
    assert q.request(EID, "no_thanks")
    assert not q.request(EID, "shout")
    assert [s[1] for s in said] == ["coming", "no_thanks"]


def test_no_buttons_on_the_interim_alert_mid_conversation():
    q = quick(FakeClock(), [])
    assert q.buttons(Visit(EID, "front_door", stage="alert")) == []


def test_offered_replies_are_the_settings():
    q = QuickReplies(("coming",), 300.0, say=lambda *a: None)
    assert [label for label, _ in q.buttons(Visit(EID, "front_door"))] == ["I'm coming"]
    assert not q.request(EID, "later")


def test_old_visits_are_forgotten():
    clock, said = FakeClock(0.0), []
    q = quick(clock, said)
    notified(q, "old")
    q.request("old", "coming")
    clock.now = 700.0
    notified(q, "new")
    q.request("old", "coming")  # forgotten: can't be told from never notified
    assert said[-1] == ("old", "coming", None, 0.0)


@pytest.mark.parametrize(
    ("payload", "event_id"),
    [
        (b"coming", "second"),  # Home Assistant's button: the latest visit
        (b'{"reply": "coming", "event_id": "first"}', "first"),
        (b'{"reply": "coming"}', "second"),
    ],
)
def test_mqtt_replies(payload, event_id):
    clock, said = FakeClock(1000.0), []
    q = quick(clock, said)
    notified(q, "first")
    clock.now += 10
    notified(q, "second")
    q.from_mqtt(payload)
    assert [s[:2] for s in said] == [(event_id, "coming")]


def test_mqtt_reply_with_no_visit_or_bad_json_does_nothing():
    said = []
    q = quick(FakeClock(), said)
    q.from_mqtt(b"coming")
    notified(q)
    q.from_mqtt(b"{not json")
    assert said == []


class Stream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def lines(*messages):
    return Stream(b"".join(json.dumps(m).encode() + b"\n" for m in messages))


def test_ntfy_replies_reads_messages_and_resumes_after_the_last_one():
    opened, got = [], []

    def open_url(request, timeout):
        opened.append((request.full_url, request.get_header("Authorization")))
        return lines(
            {"event": "open"},
            {"event": "keepalive"},
            {"event": "message", "id": "m1", "message": f"{EID} coming abc"},
        )

    replies = NtfyReplies("http://ntfy/", "doorbell-replies", "tk_x", got.append, open_url=open_url)
    replies.follow_once()
    replies.follow_once()
    assert got == [f"{EID} coming abc"] * 2
    assert opened == [
        ("http://ntfy/doorbell-replies/json", "Bearer tk_x"),
        ("http://ntfy/doorbell-replies/json?since=m1", "Bearer tk_x"),
    ]


def test_ntfy_replies_retries_after_losing_the_connection():
    attempts, sleeps = [], []

    def open_url(request, timeout):
        attempts.append(request.full_url)
        raise OSError("connection refused")

    class Stop(Exception):
        pass

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise Stop

    replies = NtfyReplies("http://ntfy", "r", "", lambda m: None, open_url=open_url, sleep=sleep)
    with pytest.raises(Stop):
        replies.run_forever()
    assert len(attempts) == 3
    assert sleeps == [5.0, 10.0, 20.0]
