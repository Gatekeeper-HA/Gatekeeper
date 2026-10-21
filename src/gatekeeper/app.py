"""Entry point: wire Frigate MQTT events to visits."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from urllib.parse import quote

import paho.mqtt.client as mqtt

from gatekeeper import __version__
from gatekeeper.audio import Synthesizer, Transcriber, presynth_all
from gatekeeper.audio_tap import AudioTap
from gatekeeper.button import DoorbellButton
from gatekeeper.config import Settings
from gatekeeper.eventlog import EventLog
from gatekeeper.frigate import FrigateEvent, Zone, fetch_config, parse_event
from gatekeeper.health import Health, serve
from gatekeeper.interaction import Interaction, Visit
from gatekeeper.logs import setup_logging
from gatekeeper.notify import Notifier, NtfyBackend, frigate_snapshot
from gatekeeper.packages import (
    PackageModel,
    PackageWatcher,
    frigate_frame,
    person_snapshot,
)
from gatekeeper.publish import QUICK_REPLY_TOPIC, Publisher
from gatekeeper.quick_reply import NtfyReplies, QuickReplies
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
    commands: dict[str, Callable[[bytes], None]] | None = None,
) -> mqtt.Client:
    """``commands``: handlers for topics other than Frigate's events, by topic."""
    commands = commands or {}
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password.get_secret_value())

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to MQTT rc=%s", reason_code)
        client.subscribe(settings.mqtt_topic)
        for topic in commands:
            client.subscribe(topic)
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
            if msg.topic in commands:
                commands[msg.topic](msg.payload)
                return
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
        reply_url = ""
        if settings.quick_reply_list:
            reply_url = "/".join(
                (settings.ntfy_public_url.rstrip("/"), quote(settings.ntfy_reply_topic))
            )
        backends.append(
            NtfyBackend(
                settings.ntfy_url,
                settings.ntfy_topic,
                settings.ntfy_token.get_secret_value(),
                reply_url=reply_url,
            )
        )
    if not backends:
        log.warning("no notification backend configured (set NTFY_URL); nobody will be notified")
    return backends


def start_thread(name: str, target, *args, **kwargs) -> None:
    threading.Thread(target=target, args=args, kwargs=kwargs, name=name, daemon=True).start()


def start_packages(settings: Settings, tracker, notifier, interaction, publisher) -> None:
    """Package alerts: check the doorstep after each visit (packages.py)."""
    zone = Zone(
        settings.package_zone, settings.camera_name, lambda: fetch_config(settings.frigate_api)
    )
    start_thread("package-zone", zone.load_until_known)
    model = PackageModel()

    def on_change(kind, package, after, came) -> None:
        visit = Visit(
            f"package-{package.since:.3f}",
            settings.camera_name,
            outcome=f"package_{kind}",
            trigger="package",
        )
        visit.details = {"box": list(package.box), "score": package.score, "since": package.since}
        # Delivered: the doorstep with the package. Picked up: who came for it.
        image = after.jpeg
        if kind == "picked_up":
            image = person_snapshot(settings.frigate_api, settings.camera_name, came) or image
        path = settings.snapshot_dir / f"{visit.event_id}-{kind}.jpg"
        path.write_bytes(image)
        visit.image = path
        notifier(visit)
        interaction.log_only(visit)
        publisher.package(bool(watcher.packages))

    watcher = PackageWatcher(
        present=tracker.person_present,
        frame=lambda: frigate_frame(settings.frigate_api, settings.camera_name),
        zone=lambda: zone.polygon,
        score=model.score,
        on_change=on_change,
        state_file=settings.package_state_file,
    )
    publisher.package(bool(watcher.packages))
    start_thread("packages", watcher.run_forever)


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
        {
            "greeting": settings.greeting,
            **settings.replies,
            **settings.phrases,
            **{
                f"quick_{key}": settings.quick_reply_phrases[key]
                for key in settings.quick_reply_list
            },
        },
    )

    transcriber = Transcriber(
        settings.whisper_model,
        settings.whisper_compute_type,
        settings.whisper_device,
        settings.whisper_hotwords,
    )
    try:
        transcriber.load()
    except Exception:
        log.exception("could not load the Whisper model; will retry on the first visit")

    # The resident's quick replies from the notification, said by `interaction`
    # (defined below; none can arrive before a visit is notified).
    quick = None
    commands = {}
    if settings.quick_reply_list:
        quick = QuickReplies(
            settings.quick_reply_list,
            settings.quick_reply_window_seconds,
            say=lambda *args: interaction.quick_reply(*args),
        )
        commands[QUICK_REPLY_TOPIC.format(camera=settings.camera_name)] = quick.from_mqtt

    # The callbacks reference `tracker` and `publisher`, defined below; no
    # message arrives before connect() at the end.
    client = build_mqtt_client(
        settings,
        on_event=lambda event: tracker.handle_event(event),
        health=health,
        on_connected=lambda: publisher.announce(),
        commands=commands,
    )
    publisher = Publisher(
        client,
        camera=settings.camera_name,
        discovery_prefix=settings.ha_discovery_prefix,
        version=__version__,
        quick_replies=quick.replies if quick else (),
        packages=bool(settings.package_zone),
    )
    publisher.set_will()

    notifier = Notifier(
        build_notify_backends(settings),
        snapshot=lambda visit: frigate_snapshot(settings.frigate_api, visit),
        log_to=EventLog(settings.notification_log_file),
        actions=quick.buttons if quick else lambda visit: [],
    )
    if quick and settings.ntfy_url:
        replies = NtfyReplies(
            settings.ntfy_url,
            settings.ntfy_reply_topic,
            settings.ntfy_token.get_secret_value(),
            quick.from_ntfy,
        )
        start_thread("quick-replies", replies.run_forever)
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
        # quick.notified first: a reply's window starts with the notification.
        on_notify=[quick.notified, notifier] if quick else [notifier],
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

    door = None
    if settings.door_zone:
        # Someone appearing here is the household coming out (SessionTracker).
        door = Zone(
            settings.door_zone, settings.camera_name, lambda: fetch_config(settings.frigate_api)
        )
        start_thread("door-zone", door.load_until_known)
    tracker = SessionTracker(
        camera=settings.camera_name,
        dwell_seconds=settings.dwell_seconds,
        ttl_seconds=settings.session_ttl_seconds,
        cooldown_seconds=settings.cooldown_seconds,
        trigger_zones=settings.trigger_zone_list,
        door=door.contains if door else None,
        leaving_watch_seconds=settings.leaving_watch_seconds,
        leaving_cooldown_seconds=settings.leaving_cooldown_seconds,
        return_window_seconds=settings.return_window_seconds,
        on_not_greeted=interaction.log_not_greeted,
        on_went_inside=interaction.went_inside,
        run_visit=run_visit,
    )
    start_thread(
        "sweep", tracker.sweep_forever, settings.sweep_interval_seconds, on_sweep=health.sweep_done
    )
    if settings.package_zone:
        start_packages(settings, tracker, notifier, interaction, publisher)
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
