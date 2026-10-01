"""Settings, read from environment variables.

Every field maps to the upper-cased environment variable of the same name
(``mqtt_host`` -> ``MQTT_HOST``). Defaults are the compose rig's; the Home
Assistant add-on overrides them from its options in the s6 ``run`` script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPLY_KEYS = ("no_answer", "delivery", "sales", "maintenance", "generic")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    # MQTT
    mqtt_host: str = "mqtt"
    mqtt_port: int = 1883
    mqtt_topic: str = "frigate/events"

    # Camera and go2rtc
    camera_name: str = "front_door"
    go2rtc_api: str = "http://go2rtc:1984"
    go2rtc_talk_stream: str = "front_door_talk"
    # Defaults to rtsp://go2rtc:8554/<camera_name> when unset.
    audio_rtsp_url: str = ""

    # Visit timing
    dwell_seconds: float = 1.0
    listen_seconds: int = 4
    session_ttl_seconds: float = 120.0
    sweep_interval_seconds: float = 1.0

    # Speech
    whisper_model: str = "tiny"
    whisper_compute_type: str = "int8"
    kokoro_voice: str = "af_heart"

    # What Gatekeeper says
    greeting: str = "Hello. This property is monitored. Please state the purpose of your visit."
    reply_no_answer: str = (
        "You are being recorded. Please state your purpose or leave the property."
    )
    reply_delivery: str = "Thank you. Please leave the package at the door."
    reply_sales: str = "No solicitation. Please leave the property."
    reply_maintenance: str = "Please wait while I notify the resident."
    reply_generic: str = "Thank you. Please wait while I notify the resident."

    # Storage
    audio_dir: Path = Path("/audio")
    log_dir: Path = Path("/logs")

    # Logging
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if not self.audio_rtsp_url:
            self.audio_rtsp_url = f"rtsp://go2rtc:8554/{self.camera_name}"
        return self

    @property
    def in_dir(self) -> Path:
        return self.audio_dir / "in"

    @property
    def out_dir(self) -> Path:
        return self.audio_dir / "out"

    @property
    def event_log_file(self) -> Path:
        return self.log_dir / "events.jsonl"

    @property
    def replies(self) -> dict[str, str]:
        """Reply text by reply key (see ``classify.classify_response``)."""
        return {key: getattr(self, f"reply_{key}") for key in REPLY_KEYS}
