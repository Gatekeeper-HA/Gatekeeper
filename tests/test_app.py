from types import SimpleNamespace

from pydantic import SecretStr

from conftest import load_fixture
from gatekeeper.app import build_mqtt_client, ensure_dirs
from gatekeeper.config import Settings
from gatekeeper.health import Health
from gatekeeper.sessions import SessionTracker


def message(payload: bytes):
    return SimpleNamespace(topic="frigate/events", payload=payload)


def test_mqtt_messages_reach_the_tracker(settings, clock):
    started = []
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120,
        run_visit=started.append, spawn=lambda fn: fn(), clock=clock,
    )  # fmt: skip
    client = build_mqtt_client(settings, on_event=tracker.handle_event)
    client.on_message(client, None, message(load_fixture("frigate/person_new.json")))
    assert started == ["1727712000.123456-abc123"]


def test_bad_messages_do_not_raise(settings):
    events = []
    client = build_mqtt_client(settings, on_event=events.append)
    for payload in (b"garbage", b"[]", b'{"type": "new"}'):
        client.on_message(client, None, message(payload))
    assert events == []


def test_handler_errors_do_not_raise(settings):
    def boom(event):
        raise RuntimeError("tracker bug")

    client = build_mqtt_client(settings, on_event=boom)
    client.on_message(client, None, message(load_fixture("frigate/person_new.json")))


def test_on_connect_subscribes_and_announces(settings):
    connected = []
    client = build_mqtt_client(
        settings, on_event=lambda e: None, on_connected=lambda: connected.append(True)
    )
    subscribed = []
    fake = SimpleNamespace(subscribe=subscribed.append)
    client.on_connect(fake, None, {}, 0, None)
    assert subscribed == ["frigate/events"]
    assert connected == [True]


def test_refused_connection_does_not_announce(settings):
    connected = []
    client = build_mqtt_client(
        settings, on_event=lambda e: None, on_connected=lambda: connected.append(True)
    )
    client.on_connect(SimpleNamespace(subscribe=lambda t: None), None, {}, 5, None)
    assert connected == []


def test_credentials_are_used_when_set(settings):
    settings.mqtt_username = "gatekeeper"
    settings.mqtt_password = SecretStr("s3cret")
    client = build_mqtt_client(settings, on_event=lambda e: None)
    assert client._username == b"gatekeeper"
    assert client._password == b"s3cret"


def test_no_credentials_by_default(settings):
    client = build_mqtt_client(settings, on_event=lambda e: None)
    assert client._username is None


def test_mqtt_connection_state_feeds_health(settings):
    health = Health(sweep_max_age=10, visit_max_age=90)
    client = build_mqtt_client(settings, on_event=lambda e: None, health=health)
    fake = SimpleNamespace(subscribe=lambda topic: None)
    client.on_connect(fake, None, {}, 5, None)  # refused (not authorized)
    assert not health.mqtt_connected
    client.on_connect(fake, None, {}, 0, None)
    assert health.mqtt_connected
    client.on_disconnect(fake, None, {}, 7, None)
    assert not health.mqtt_connected


def test_ensure_dirs(tmp_path):
    s = Settings(audio_dir=tmp_path / "a", log_dir=tmp_path / "l")
    ensure_dirs(s)
    assert s.in_dir.is_dir() and s.out_dir.is_dir() and s.log_dir.is_dir()
