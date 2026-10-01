"""Entry point: wire Frigate MQTT events to visits."""

from __future__ import annotations

import logging
import threading
import time

import paho.mqtt.client as mqtt

from gatekeeper import __version__
from gatekeeper.audio import Synthesizer, Transcriber, presynth_all
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.frigate import parse_event
from gatekeeper.interaction import Interaction
from gatekeeper.logs import setup_logging
from gatekeeper.sessions import SessionTracker

log = logging.getLogger(__name__)


def ensure_dirs(settings: Settings) -> None:
    for d in (settings.in_dir, settings.out_dir, settings.log_dir):
        d.mkdir(parents=True, exist_ok=True)


def build_mqtt_client(settings: Settings, tracker: SessionTracker) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("connected to MQTT rc=%s", reason_code)
        client.subscribe(settings.mqtt_topic)

    def on_message(client, userdata, msg):
        try:
            event = parse_event(msg.payload)
            if event:
                tracker.handle_event(event)
        except Exception:
            log.exception("error handling MQTT message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_message = on_message
    return client


def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)
    log.info(
        "Gatekeeper %s starting: camera=%s mqtt=%s:%s go2rtc=%s",
        __version__,
        settings.camera_name,
        settings.mqtt_host,
        settings.mqtt_port,
        settings.go2rtc_api,
    )

    ensure_dirs(settings)
    synth = Synthesizer(settings.kokoro_voice)
    presynth = presynth_all(
        synth, settings.out_dir, {"greeting": settings.greeting, **settings.replies}
    )

    interaction = Interaction(
        settings,
        synth=synth,
        transcriber=Transcriber(settings.whisper_model, settings.whisper_compute_type),
        presynth=presynth,
        event_log=EventLog(settings.event_log_file),
    )
    tracker = SessionTracker(
        camera=settings.camera_name,
        dwell_seconds=settings.dwell_seconds,
        ttl_seconds=settings.session_ttl_seconds,
        run_visit=interaction.run,
    )
    threading.Thread(
        target=tracker.sweep_forever,
        args=(settings.sweep_interval_seconds,),
        name="sweep",
        daemon=True,
    ).start()

    client = build_mqtt_client(settings, tracker)
    while True:
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, 60)
            break
        except Exception as e:
            log.warning("MQTT connect failed, retrying: %s", e)
            time.sleep(5)

    client.loop_forever()
