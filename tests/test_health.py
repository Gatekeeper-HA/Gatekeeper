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
