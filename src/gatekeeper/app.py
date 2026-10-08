"""Entry point: wire Frigate MQTT events to visits."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt

from gatekeeper import __version__
from gatekeeper.audio import Synthesizer, Transcriber, presynth_all
from gatekeeper.audio_tap import AudioTap
from gatekeeper.button import DoorbellButton
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.frigate import FrigateEvent, parse_event
from gatekeeper.health import Health, serve
from gatekeeper.interaction import Interaction, Visit
from gatekeeper.logs import setup_logging
from gatekeeper.notify import Notifier, NtfyBackend, frigate_snapshot
from gatekeeper.publish import Publisher
from gatekeeper.retention import Retention
from gatekeeper.sessions import SessionTracker

log = logging.getLogger(__name__)


def ensure_dirs(settings: Settings) -> None:
    for d in (settings.in_dir, settings.out_dir, settings.snapshot_dir, settings.log_dir):
        d.mkdir(parents=True, exist_ok=True)


def build_mqtt_client(
    settings: Settings,
    *,
    on_event: Callable[[FrigateEvent], None],
    health: Health | None = None,
    on_connected: Callable[[], None] | None = None,
) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password.get_secret_value())

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT rc=%s", reason_code)
        client.subscribe(settings.mqtt_topic)
        if health:
            health.mqtt_connected = True
        if on_connected:
            on_connected()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected from MQTT: %s", reason_code)
        if health:
            health.mqtt_connected = False

    def on_message(client, userdata, msg):
        try:
            event = parse_event(msg.payload)
            if event:
                on_event(event)
        except Exception:
            log.exception("error handling MQTT message on %s", msg.topic)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


def build_notify_backends(settings: Settings) -> list:
    backends = []
    if settings.ntfy_url:
        backends.append(
            NtfyBackend(
                settings.ntfy_url, settings.ntfy_topic, settings.ntfy_token.get_secret_value()
            )
        )
    if not backends:
        log.warning("no notification backend configured (set NTFY_URL); nobody will be notified")
    return backends


def start_thread(name: str, target, *args, **kwargs) -> None:
    threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=True).start()


def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)
    log.info(
        "Gatekeeper %s starting: camera=%s zones=%s mqtt=%s:%s go2rtc=%s",
        __version__,
        settings.camera_name,
        settings.trigger_zones or "any",
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
        synth,
        settings.out_dir,
        {"greeting": settings.greeting, **settings.replies, **settings.phrases},
    )

    transcriber = Transcriber(
        settings.whisper_model, settings.whisper_compute_type, settings.whisper_hotwords
    )
    try:
        transcriber.load()
    except Exception:
        log.exception("could not load the Whisper model; will retry on the first visit")

    # The callbacks reference `tracker` and `publisher`, defined below; no
    # message arrives before connect() at the end.
    client = build_mqtt_client(
        settings,
        on_event=lambda event: tracker.handle_event(event),
        health=health,
        on_connected=lambda: publisher.announce(),
    )
    publisher = Publisher(
        client,
        camera=settings.camera_name,
        discovery_prefix=settings.ha_discovery_prefix,
        version=__version__,
    )
    publisher.set_will()

    notifier = Notifier(
        build_notify_backends(settings),
        snapshot=lambda visit: frigate_snapshot(settings.frigate_api, visit),
        log_to=EventLog(settings.notification_log_file),
    )
    tap = None
    if settings.audio_tap:
        # Reads the camera's audio from now on, so visits listen without connecting.
        tap = AudioTap(settings.audio_rtsp_url)
        health.tap_ok = tap.healthy
        start_thread("audio-tap", tap.run_forever)
    interaction = Interaction(
        settings,
        synth=synth,
        transcriber=transcriber,
        presynth=presynth,
        event_log=EventLog(settings.event_log_file),
        tap=tap,
        # Before asking an officer to leave a second time: are they still there?
        present=lambda: tracker.person_present(),
        on_notify=[notifier],
        on_logged=[publisher.visit],
    )

    def run_visit(event_id: str, trigger: str) -> None:
        health.visit_started()
        publisher.conversation(True)
        try:
            interaction.run(event_id, trigger)
        finally:
            publisher.conversation(False)
            health.visit_finished()

    tracker = SessionTracker(
        camera=settings.camera_name,
        dwell_seconds=settings.dwell_seconds,
        ttl_seconds=settings.session_ttl_seconds,
        cooldown_seconds=settings.cooldown_seconds,
        trigger_zones=settings.trigger_zone_list,
        run_visit=run_visit,
    )
    start_thread(
        "sweep", tracker.sweep_forever, settings.sweep_interval_seconds, on_sweep=health.sweep_done
    )
    start_thread("retention", Retention(settings).run_forever)

    def on_press() -> None:
        action, visit_id = tracker.press()
        if action == "merged":
            # Not on the doorbell listener's event loop: the reply plays audio.
            start_thread(
                "press-reply",
                interaction.acknowledge_press,
                Visit(
                    f"press-{time.time():.3f}",
                    settings.camera_name,
                    outcome="pressed_during_visit",
                    trigger="button",
                ),
            )

    if settings.reolink_host:
        button = DoorbellButton(
            settings.reolink_host,
            settings.reolink_username,
            settings.reolink_password.get_secret_value(),
            on_press=on_press,
        )
        start_thread("button", button.run_forever)
    if settings.hang_exit_seconds:
        start_thread("watchdog", health.watchdog, settings.hang_exit_seconds)

    while True:
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, 60)
            break
        except Exception as e:
            log.warning("MQTT connect failed, retrying: %s", e)
            time.sleep(5)

    client.loop_forever()
