"""Settings, read from environment variables.

Every field maps to the upper-cased environment variable of the same name
(``mqtt_host`` -> ``MQTT_HOST``). Defaults are the compose rig's; the Home
Assistant add-on overrides them from its options in the s6 ``run`` script.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPLY_KEYS = ("no_answer", "delivery", "sales", "maintenance", "generic", "pressed")
# Lines of the multi-turn conversations (dialogue.py), PHRASE_<KEY> in the environment.
PHRASE_KEYS = (
    "emergency_wait",
    "police_ask_reason", "police_ask_warrant", "police_ask_identity", "police_show_warrant",
    "police_notifying", "police_leave", "police_leave_again",
    "civic_ask_identity", "civic_ask_message", "civic_thanks",
)  # fmt: skip


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    # MQTT
    mqtt_host: str = "mqtt"
    mqtt_port: int = 1883
    mqtt_topic: str = "frigate/events"
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")
    # Home Assistant MQTT discovery prefix; empty disables discovery.
    ha_discovery_prefix: str = "homeassistant"

    # Camera and go2rtc
    camera_name: str = "front_door"
    go2rtc_api: str = "http://go2rtc:1984"
    go2rtc_talk_stream: str = "front_door_talk"
    # Defaults to rtsp://go2rtc:8554/<camera_name> when unset.
    audio_rtsp_url: str = ""

    # Visit timing
    dwell_seconds: float = 1.0
    # Comma-separated Frigate zones (e.g. "porch"); a person must enter one
    # before a visit starts. Empty: anywhere in view.
    trigger_zones: str = ""
    # How long to wait for the visitor to start answering after a prompt.
    listen_seconds: int = 4

    # Listening. With the audio tap, Gatekeeper reads the camera's audio all the
    # time and stops listening when the visitor stops talking (voice activity
    # detection); without it, it records a fixed window per prompt.
    audio_tap: bool = True
    # Speech probability (0-1) above which audio counts as the visitor talking.
    vad_threshold: float = 0.5
    # Silence after the visitor's last word that ends their answer.
    end_silence_ms: int = 800
    # Cut off an answer that runs longer than this.
    max_answer_seconds: float = 15.0
    # "visit": one talkback connection for the whole visit. "clip": a new one
    # per phrase (slower; for cameras that misbehave with a long connection).
    talkback_session: Literal["visit", "clip"] = "visit"
    session_ttl_seconds: float = 120.0
    sweep_interval_seconds: float = 1.0
    # New person events this soon after a visit are merged into it, not re-greeted.
    cooldown_seconds: float = 90.0
    # A visit (greet, listen, reply) takes ~25 s, ~45 s when the visitor only
    # answers the second time, and up to ~2 minutes for a conversation with an
    # officer or a canvasser; give up after this.
    visit_timeout_seconds: float = 180.0
    # After asking an officer without a judge-signed warrant to leave, wait this
    # long; if Frigate still sees someone on the porch, ask again more firmly.
    leave_check_seconds: float = 20.0

    # Speech
    whisper_model: str = "tiny"
    whisper_compute_type: str = "int8"
    # Words and names Whisper should expect, comma-separated (e.g. local
    # candidates' names, which it otherwise mishears).
    whisper_hotwords: str = ""
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
    # Said when the doorbell is pressed during a visit's cooldown.
    reply_pressed: str = "The resident has already been notified."

    # Conversations (dialogue.py). They state the resident's position; have a
    # lawyer review any change to the law-enforcement lines.
    phrase_emergency_wait: str = "I have alerted the resident. Please wait."
    phrase_police_ask_reason: str = (
        "This is an automated assistant, and this conversation is recorded. "
        "What is the reason for your visit, and do you have a warrant?"
    )
    phrase_police_ask_warrant: str = "Do you have a warrant? Please answer yes or no."
    phrase_police_ask_identity: str = (
        "Please state your agency, your name, and your badge number."
    )
    phrase_police_show_warrant: str = (
        "Please hold the warrant up to the camera. Is it signed by a judge?"
    )
    phrase_police_notifying: str = (
        "Thank you. I am notifying the resident now. Please wait."
    )
    phrase_police_leave: str = (
        "The resident does not consent to entry or a search without a warrant "
        "signed by a judge. Please leave the property."
    )
    phrase_police_leave_again: str = (
        "You have been asked to leave. The resident does not consent to your presence, "
        "this conversation is recorded, and legal action will be taken if you remain."
    )
    phrase_civic_ask_identity: str = (
        "Thanks for stopping by. Could you tell me your name, and who you're with?"
    )
    phrase_civic_ask_message: str = (
        "What message would you like me to pass on to the resident?"
    )
    phrase_civic_thanks: str = (
        "Thank you. I'll make sure the resident gets your message. "
        "Feel free to leave any literature at the door."
    )

    # Notifications. FRIGATE_API supplies the visitor's snapshot; an empty
    # NTFY_URL disables ntfy.
    frigate_api: str = "http://frigate:5000"
    ntfy_url: str = ""
    ntfy_topic: str = "doorbell"
    ntfy_token: SecretStr = SecretStr("")

    # Doorbell button presses from a Reolink doorbell (HTTPS login + push
    # events); an empty REOLINK_HOST disables them.
    reolink_host: str = ""
    reolink_username: str = ""
    reolink_password: SecretStr = SecretStr("")

    # Storage
    audio_dir: Path = Path("/audio")
    log_dir: Path = Path("/logs")
    # Retention, in days (0 = keep forever). Visitor audio and transcripts are
    # third parties' voice data.
    audio_retention_days: float = 7.0
    event_log_retention_days: float = 30.0

    # Health: GET :health_port/healthz (0 disables). On an internal hang (sweep
    # loop stalled or a visit stuck) lasting hang_exit_seconds, exit so the
    # container restarts (0 disables).
    health_port: int = 8099
    hang_exit_seconds: float = 300.0

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
    def trigger_zone_list(self) -> list[str]:
        return [z.strip() for z in self.trigger_zones.split(",") if z.strip()]

    @property
    def notification_log_file(self) -> Path:
        return self.log_dir / "notifications.jsonl"

    @property
    def replies(self) -> dict[str, str]:
        """Reply text by reply key (see ``classify.classify_response``)."""
        return {key: getattr(self, f"reply_{key}") for key in REPLY_KEYS}

    @property
    def phrases(self) -> dict[str, str]:
        """Conversation lines by key (see ``dialogue.py``)."""
        return {key: getattr(self, f"phrase_{key}") for key in PHRASE_KEYS}

    @property
    def snapshot_dir(self) -> Path:
        """Photos kept during conversations (e.g. a warrant held up to the camera)."""
        return self.audio_dir / "snapshots"
