from pathlib import Path

import pytest
from pydantic import ValidationError

from gatekeeper.config import QUICK_REPLY_KEYS, REPLY_KEYS, Settings


def test_defaults_match_compose_rig(monkeypatch):
    for var in ("MQTT_HOST", "CAMERA_NAME", "AUDIO_RTSP_URL", "AUDIO_DIR"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.mqtt_host == "mqtt"
    assert s.mqtt_port == 1883
    assert s.go2rtc_api == "http://go2rtc:1984"
    assert s.audio_rtsp_url == "rtsp://go2rtc:8554/front_door"
    assert s.event_log_file == Path("/logs/events.jsonl")
    assert s.in_dir == Path("/audio/in")


def test_env_overrides_like_the_ha_addon(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1884")
    monkeypatch.setenv("CAMERA_NAME", "porch")
    monkeypatch.setenv("DWELL_SECONDS", "2")
    monkeypatch.setenv("AUDIO_DIR", "/data/audio")
    monkeypatch.setenv("REPLY_SALES", "Not interested, thanks.")
    s = Settings()
    assert s.mqtt_host == "core-mosquitto"
    assert s.mqtt_port == 1884
    assert s.dwell_seconds == 2.0
    assert s.audio_rtsp_url == "rtsp://go2rtc:8554/porch"
    assert s.out_dir == Path("/data/audio/out")
    assert s.replies["sales"] == "Not interested, thanks."


def test_empty_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LISTEN_SECONDS", "")
    monkeypatch.setenv("AUDIO_RTSP_URL", "")
    assert Settings().listen_seconds == 4
    assert Settings().audio_rtsp_url == "rtsp://go2rtc:8554/front_door"


def test_explicit_audio_url_wins(monkeypatch):
    monkeypatch.setenv("AUDIO_RTSP_URL", "rtsp://localhost:8554/front_door")
    assert Settings().audio_rtsp_url == "rtsp://localhost:8554/front_door"


def test_replies_cover_all_keys():
    assert set(Settings().replies) == set(REPLY_KEYS)


def test_trigger_zones(monkeypatch):
    monkeypatch.setenv("TRIGGER_ZONES", " porch, walkway ,")
    assert Settings().trigger_zone_list == ["porch", "walkway"]
    monkeypatch.delenv("TRIGGER_ZONES")
    assert Settings().trigger_zone_list == []


def test_quick_replies_default_to_the_ntfy_server_and_topic(monkeypatch):
    for var in ("NTFY_PUBLIC_URL", "NTFY_REPLY_TOPIC", "QUICK_REPLIES"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(ntfy_url="http://ntfy", ntfy_topic="doorbell")
    assert (s.ntfy_public_url, s.ntfy_reply_topic) == ("http://ntfy", "doorbell-replies")
    assert s.quick_reply_list == list(QUICK_REPLY_KEYS)
    assert set(s.quick_reply_phrases) == set(QUICK_REPLY_KEYS)
    assert Settings(quick_replies="").quick_reply_list == []


@pytest.mark.parametrize("value", ["coming,shout", "coming,later,no_thanks,coming"])
def test_quick_replies_must_be_known_and_at_most_three(value):
    with pytest.raises(ValidationError):
        Settings(quick_replies=value)


def test_package_alerts_are_off_without_a_zone(monkeypatch):
    monkeypatch.delenv("PACKAGE_ZONE", raising=False)
    assert Settings().package_zone == ""
    assert Settings(log_dir="/logs").package_state_file == Path("/logs/packages.json")
