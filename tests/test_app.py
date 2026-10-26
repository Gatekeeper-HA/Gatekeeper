from types import SimpleNamespace

from pydantic import SecretStr

from conftest import load_fixture
from gatekeeper.app import build_mqtt_client, ensure_dirs
from gatekeeper.config import Settings
from gatekeeper.frigate import FrigatePerception
from gatekeeper.health import Health
from gatekeeper.sessions import SessionTracker


def message(payload: bytes):
    return SimpleNamespace(topic="frigate/events", payload=payload)


def wired(settings, on_event=lambda e: None, commands=(), **kw):
    """The MQTT client as app.main wires it: the perception (Frigate, here) follows its
    events topic, next to any other topics."""
    topics = {}
    perception = FrigatePerception(
        settings.frigate_api, settings.camera_name, subscribe=topics.__setitem__
    )
    perception.start(on_event)
    topics.update(dict(commands))
    return build_mqtt_client(settings, commands=topics, **kw)


def test_mqtt_messages_reach_the_tracker(settings, clock):
    started = []
    tracker = SessionTracker(
        camera="front_door", dwell_seconds=0, ttl_seconds=120,
        run_visit=lambda eid, trigger: started.append(eid), spawn=lambda fn: fn(), clock=clock,
    )  # fmt: skip
    client = wired(settings, on_event=tracker.handle_event)
    client.on_message(client, None, message(load_fixture("frigate/person_new.json")))
    assert started == ["1727712000.123456-abc123"]


def test_bad_messages_do_not_raise(settings):
    events = []
    client = wired(settings, on_event=events.append)
    for payload in (b"garbage", b"[]", b'{"type": "new"}'):
        client.on_message(client, None, message(payload))
    assert events == []


def test_handler_errors_do_not_raise(settings):
    def boom(event):
        raise RuntimeError("tracker bug")

    client = wired(settings, on_event=boom)
    client.on_message(client, None, message(load_fixture("frigate/person_new.json")))


def test_on_connect_subscribes_and_announces(settings):
    connected = []
    client = wired(settings, on_event=lambda e: None, on_connected=lambda: connected.append(True))
    subscribed = []
    fake = SimpleNamespace(subscribe=subscribed.append)
    client.on_connect(fake, None, {}, 0, None)
    assert subscribed == ["frigate/events"]
    assert connected == [True]


def test_refused_connection_does_not_announce(settings):
    connected = []
    client = wired(settings, on_event=lambda e: None, on_connected=lambda: connected.append(True))
    client.on_connect(SimpleNamespace(subscribe=lambda t: None), None, {}, 5, None)
    assert connected == []


def test_credentials_are_used_when_set(settings):
    settings.mqtt_username = "gatekeeper"
    settings.mqtt_password = SecretStr("s3cret")
    client = wired(settings, on_event=lambda e: None)
    assert client._username == b"gatekeeper"
    assert client._password == b"s3cret"


def test_no_credentials_by_default(settings):
    client = wired(settings, on_event=lambda e: None)
    assert client._username is None


def test_mqtt_connection_state_feeds_health(settings):
    health = Health(sweep_max_age=10, visit_max_age=90)
    client = wired(settings, on_event=lambda e: None, health=health)
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


def test_notify_backends_from_settings(settings):
    from gatekeeper.app import build_notify_backends

    assert build_notify_backends(settings) == []
    settings.ntfy_url = "http://ntfy"
    settings.ntfy_token = SecretStr("tk_x")
    (backend,) = build_notify_backends(settings)
    assert backend.endpoint == "http://ntfy/doorbell"
    assert backend.token == "tk_x"


def test_command_topics_are_subscribed_and_handled_apart_from_events(settings):
    events, replies = [], []
    client = wired(
        settings,
        on_event=events.append,
        commands={"gatekeeper/front_door/quick_reply": replies.append},
    )
    subscribed = []
    client.on_connect(SimpleNamespace(subscribe=subscribed.append), None, {}, 0, None)
    assert subscribed == ["frigate/events", "gatekeeper/front_door/quick_reply"]
    msg = SimpleNamespace(topic="gatekeeper/front_door/quick_reply", payload=b"coming")
    client.on_message(client, None, msg)
    assert replies == [b"coming"] and events == []


def test_a_package_left_opens_the_delivery_and_its_pickup_ends_it(monkeypatch, tmp_path):
    from gatekeeper import app

    calls = []
    watchers = []

    class Watcher:
        def __init__(self, **kwargs):
            self.on_change = kwargs["on_change"]
            self.packages = []
            watchers.append(self)

        def run_forever(self):
            pass

    monkeypatch.setattr(app, "PackageWatcher", Watcher)
    monkeypatch.setattr(app, "PackageModel", lambda: SimpleNamespace(score=None))
    monkeypatch.setattr(app, "Baselines", lambda path: None)
    monkeypatch.setattr(app, "start_thread", lambda *a, **k: None)
    tracker = SimpleNamespace(
        person_present=lambda: False,
        delivered=lambda delivery_id: calls.append(("delivered", delivery_id)),
        picked_up=lambda: calls.append(("picked_up",)),
    )
    perception = SimpleNamespace(
        zone=lambda name: SimpleNamespace(polygon=None),
        latest_frame=lambda: None,
        person_snapshot=lambda since: b"jpeg",
    )
    settings = Settings(package_zone="porch", log_dir=tmp_path, audio_dir=tmp_path)
    ensure_dirs(settings)
    app.start_packages(
        settings,
        perception,
        tracker,
        notifier=lambda visit: None,
        interaction=SimpleNamespace(log_only=lambda visit: None),
        publisher=SimpleNamespace(package=lambda present: None),
    )
    package = SimpleNamespace(since=1800000000.25, box=(1, 2, 3, 4), score=0.81)
    after = SimpleNamespace(jpeg=b"jpeg")
    watchers[0].on_change("delivered", package, after, 0.0)
    watchers[0].on_change("picked_up", package, after, 1800000600.0)
    assert calls == [("delivered", "package-1800000000.250"), ("picked_up",)]
