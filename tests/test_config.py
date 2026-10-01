from pathlib import Path

from gatekeeper.config import REPLY_KEYS, Settings


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
