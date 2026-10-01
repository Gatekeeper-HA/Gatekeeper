from types import SimpleNamespace

from conftest import load_fixture
from gatekeeper.app import build_mqtt_client, ensure_dirs
from gatekeeper.config import Settings
from gatekeeper.sessions import SessionTracker


def test_mqtt_messages_reach_the_tracker(settings, clock):
    started = []
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120,
        run_visit=started.append, spawn=lambda fn: fn(), clock=clock,
    )  # fmt: skip
    client = build_mqtt_client(settings, tracker)

    msg = SimpleNamespace(topic="frigate/events", payload=load_fixture("frigate/person_new.json"))
    client.on_message(client, None, msg)
    assert started == ["1727712000.123456-abc123"]


def test_bad_messages_do_not_raise(settings, clock):
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120,
        run_visit=lambda _: None, clock=clock,
    )  # fmt: skip
    client = build_mqtt_client(settings, tracker)
    for payload in (b"garbage", b"[]", b'{"type": "new"}'):
        client.on_message(client, None, SimpleNamespace(topic="frigate/events", payload=payload))
    assert tracker.sessions == {}


def test_on_connect_subscribes_to_topic(settings, clock):
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120, run_visit=lambda _: None
    )
    client = build_mqtt_client(settings, tracker)
    subscribed = []
    fake = SimpleNamespace(subscribe=subscribed.append)
    client.on_connect(fake, None, {}, 0, None)
    assert subscribed == ["frigate/events"]


def test_ensure_dirs(tmp_path):
    s = Settings(audio_dir=tmp_path / "a", log_dir=tmp_path / "l")
    ensure_dirs(s)
    assert s.in_dir.is_dir() and s.out_dir.is_dir() and s.log_dir.is_dir()
