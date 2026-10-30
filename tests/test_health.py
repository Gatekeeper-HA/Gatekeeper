import json
import urllib.error
import urllib.request

import pytest

from gatekeeper.health import Health, probe, serve


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def health(clock):
    return Health(sweep_max_age=10, visit_max_age=90, clock=clock)


def test_starts_unhealthy_until_sweeping_and_connected(health):
    ok, details = health.check()
    assert not ok
    assert details["problems"] == ["session sweep stalled", "mqtt disconnected"]
    health.sweep_done()
    health.mqtt_connected = True
    assert health.check() == (
        True,
        {
            "status": "ok",
            "problems": [],
            "mqtt_connected": True,
            "sweep_age_seconds": 0.0,
            "visit_age_seconds": None,
            "audio_tap": "off",
        },
    )


def test_stalled_sweep_and_stuck_visit(health, clock):
    health.mqtt_connected = True
    health.sweep_done()
    health.visit_started()
    clock.now += 11
    assert health.check()[1]["problems"] == ["session sweep stalled"]
    health.sweep_done()
    clock.now += 80
    health.sweep_done()
    assert health.check()[1]["problems"] == ["visit stuck"]
    health.visit_finished()
    assert health.check()[0]


def test_mqtt_outage_is_not_an_internal_hang(health):
    health.sweep_done()
    health.mqtt_connected = False
    assert not health.check()[0]
    assert not health.internal_hang()


def test_own_perception_stalled_is_an_internal_hang_once_it_has_looked(health, clock):
    health.sweep_done()
    health.mqtt_connected = True
    assert "own_perception_age_seconds" not in health.check()[1]  # not starting the visits
    health.look_max_age = 60
    clock.now += 600  # the model still loading: no pass yet, not a hang
    health.sweep_done()
    assert health.check() == (
        True,
        {
            "status": "ok",
            "problems": [],
            "mqtt_connected": True,
            "sweep_age_seconds": 0.0,
            "visit_age_seconds": None,
            "own_perception_age_seconds": None,
            "audio_tap": "off",
        },
    )
    health.looked()
    clock.now += 61
    health.sweep_done()
    assert health.check()[1]["problems"] == ["own perception stalled"]
    assert health.check()[1]["own_perception_age_seconds"] == 61.0
    assert health.internal_hang()
    health.looked()
    assert health.check()[0]


def test_watchdog_exits_after_a_sustained_hang(health, clock, monkeypatch):
    import gatekeeper.health as mod

    exits = []

    def fake_sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    health.sweep_done()
    clock.now += 11  # sweep stalled from here on
    health.watchdog(hang_exit_seconds=300, interval=10, exit_fn=exits.append)
    assert exits == [1]
    assert clock.now - 111 >= 300


def test_http_endpoint_and_probe(health):
    server = serve(health, 0)
    port = server.server_address[1]
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert err.value.code == 503
        assert probe(port) == 1

        health.sweep_done()
        health.mqtt_connected = True
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            assert json.load(resp)["status"] == "ok"
        assert probe(port) == 0

        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_probe_that_hung_up_early_is_not_an_error(health, capsys, caplog):
    server = serve(health, 0)
    try:
        try:
            raise BrokenPipeError(32, "Broken pipe")
        except BrokenPipeError:
            server.handle_error(None, ("127.0.0.1", 49018))
        assert capsys.readouterr().err == ""
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

        try:
            raise ValueError("a real bug")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 49018))
        assert [r.message for r in caplog.records if r.levelname == "ERROR"] == [
            "health endpoint error"
        ]
    finally:
        server.shutdown()
        server.server_close()
