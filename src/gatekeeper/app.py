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
from gatekeeper.health import Health, serve
from gatekeeper.interaction import Interaction
from gatekeeper.logs import setup_logging
from gatekeeper.retention import Retention
from gatekeeper.sessions import SessionTracker

log = logging.getLogger(__name__)


def ensure_dirs(settings: Settings) -> None:
    for d in (settings.in_dir, settings.out_dir, settings.log_dir):
        d.mkdir(parents=True, exist_ok=True)


def build_mqtt_client(
    settings: Settings, tracker: SessionTracker, health: Health | None = None
) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT rc=%s", reason_code)
        client.subscribe(settings.mqtt_topic)
        if health:
            health.mqtt_connected = True

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected from MQTT: %s", reason_code)
        if health:
            health.mqtt_connected = False

    def on_message(client, userdata, msg):
        try:
            event = parse_event(msg.payload)
            if event:
                tracker.handle_event(event)
        except Exception:
            log.exception("error handling MQTT message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def start_thread(name: str, target, *args, **kwargs) -> None:
    threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=True).start()


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
    health = Health(
        sweep_max_age=max(10 * settings.sweep_interval_seconds, 10.0),
        visit_max_age=settings.visit_timeout_seconds + 30,
    )
    if settings.health_port:
        serve(health, settings.health_port)

    synth = Synthesizer(settings.kokoro_voice)
    presynth = presynth_all(
        synth, settings.out_dir, {"greeting": settings.greeting, **settings.replies}
    )

    transcriber = Transcriber(settings.whisper_model, settings.whisper_compute_type)
    try:
        transcriber.load()
    except Exception:
        log.exception("could not load the Whisper model; will retry on the first visit")

    interaction = Interaction(
        settings,
        synth=synth,
        transcriber=transcriber,
        presynth=presynth,
        event_log=EventLog(settings.event_log_file),
    )

    def run_visit(event_id: str) -> None:
        health.visit_started()
        try:
            interaction.run(event_id)
        finally:
            health.visit_finished()

    tracker = SessionTracker(
        camera=settings.camera_name,
        dwell_seconds=settings.dwell_seconds,
        ttl_seconds=settings.session_ttl_seconds,
        cooldown_seconds=settings.cooldown_seconds,
        run_visit=run_visit,
    )
    start_thread(
        "sweep", tracker.sweep_forever, settings.sweep_interval_seconds, on_sweep=health.sweep_done
    )
    start_thread("retention", Retention(settings).run_forever)
    if settings.hang_exit_seconds:
        start_thread("watchdog", health.watchdog, settings.hang_exit_seconds)

    client = build_mqtt_client(settings, tracker, health)
    while True:
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, 60)
            break
        except Exception as e:
            log.warning("MQTT connect failed, retrying: %s", e)
            time.sleep(5)

    client.loop_forever()
