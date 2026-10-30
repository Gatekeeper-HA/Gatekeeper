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

# Gatekeeper's greeting, the first thing a visitor hears: friendly and a little funny, and
# every one says the property is monitored. One is said at random (not the same twice in
# a row).
GREETING_LINES = (
    "Hi there! Just so you know, this property is monitored. What brings you by today?",
    "Hey! I'm the doorbell, and I keep an eye on things around here. You're on camera, by "
    "the way. What are you here for?",
    "Hi there! You're on camera, and I'm all ears. What brings you by?",
    "Welcome! This front door is monitored, and I'm the one doing the monitoring. How can I "
    "help?",
    "Hi! I'm the house's digital doorman. The property is monitored, so tell me, what brings "
    "you here?",
    "Hello there! Fair warning: this doorbell talks back, and it records everything. What can "
    "I do for you?",
    "Hey, welcome! This property is monitored, so I already know you're here. Now tell me "
    "why.",
    "Hi! You've reached the front door. It's monitored, it's friendly, and it wants to know "
    "why you're here.",
    "Hello! I'm the doorbell assistant, and yes, this place is monitored. What brings you to "
    "the door?",
    "Hey there! Before you ask: yes, this doorbell is watching and recording. What can I help "
    "you with?",
    "Good to see you! Well, I see everyone. This property is monitored. What's up?",
)
# After a doorbell press, these can be said too (they answer the bell).
GREETING_PRESSED_LINES = (
    "You rang? This is the doorbell speaking, and this property is monitored. What can I do "
    "for you?",
    "Ding dong to you too! This property is monitored. What brings you by?",
)

# Said instead of a no-answer line to someone already walking away after the greeting (a
# courier who dropped something off, mostly): a friendly send-off, at random.
GOODBYE_LINES = (
    "Leaving so soon? Thanks for stopping by!",
    "Okay, bye! It was nice not talking to you.",
    "And they're off! Thanks for stopping by.",
    "Thanks! I'll let them know you came by.",
    "Drive safe out there! Thanks for stopping by.",
    "Alright, I'll take that as a goodbye. Thanks for coming by!",
    "See you later! Thanks for stopping by.",
)

# Gatekeeper's personality when someone at the door doesn't answer the greeting: friendly
# and funny, and every line still gets them to say why they came, or go. One is said at
# random (not the same twice in a row). The owner's examples, written for speaking.
NO_ANSWER_LINES = (
    "Hello! You've reached the Department of People Who Show Up Without Explaining "
    "Themselves. How can I not help you today?",
    "Hi! Are we having a conversation, or are you just here to admire my front door?",
    "Press one to explain why you're here. Press two to leave. Silence will automatically "
    "select option two.",
    "You came all the way here just to stand there and ignore a camera? That's an impressive "
    "waste of everybody's time.",
    "I know you can hear me. The awkward silence is getting its own zip code.",
    "Listen, I can see you, the camera can see you, and apparently neither of us knows why "
    "you're here. Let's fix that.",
    "You have two options: explain yourself, or become part of the doorbell footage "
    "documentary.",
    "Welcome! Today's grand prize is getting to tell me why you stopped by! But hurry, "
    "contestant, because the alternative is leaving!",
    "You have five seconds to tell me what you want. Five... four... three... this is not "
    "looking good for you!",
    "Is it a delivery? An emergency? A social visit? Find out after this very uncomfortable "
    "silence!",
    "And behind door number one... a person who refuses to explain why they're standing "
    "outside!",
    "Hey, if you have something to say, now's the time. Otherwise, I'll let you get back to "
    "whatever this performance is.",
    "I'm happy to talk, but I'm not playing charades through a doorbell camera.",
    "You can tell me why you're here, or you can enjoy the walk back. Totally your call.",
    "I'm giving you one last chance to make this visit make sense.",
    "No pressure, but standing there silently is a pretty weird way to start a conversation.",
    "Just so you know, this camera has better communication skills than you do.",
    "Should I call someone, or are you waiting for the director to yell cut?",
    "I'm going to need you to either speak or unlock the bonus level, because this "
    "standing-around thing is getting repetitive.",
    "This is a doorbell camera, not a staring contest. And honestly, I'm winning.",
)
# After a doorbell press, these can be said too (they mention the bell).
NO_ANSWER_PRESSED_LINES = (
    "You rang the bell, my friend. This is your opportunity to speak before your free trial "
    "expires.",
    "You rang my bell, not my emotional support hotline. What do you need?",
)
# Lines of the multi-turn conversations (dialogue.py), PHRASE_<KEY> in the environment.
PHRASE_KEYS = (
    "emergency_wait",
    "police_disclosure",
    "police_ask_reason", "police_ask_warrant", "police_ask_identity", "police_show_warrant",
    "police_hold_warrant", "police_warrant_done",
    "police_notifying", "police_leave", "police_leave_again",
    "civic_ask_identity", "civic_ask_message", "civic_thanks",
)  # fmt: skip
# What the resident can answer from the notification (quick_reply.py), QUICK_REPLY_<KEY>.
QUICK_REPLY_KEYS = ("coming", "later", "no_thanks")


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
    # The Live page's streams in go2rtc (live.py): the video copied, the audio as Opus.
    # Default <camera_name>_live (low resolution) and <camera_name>_live_hd.
    live_stream: str = ""
    live_hd_stream: str = ""
    # The web page over HTTPS (tls.py), with a self-signed certificate made once: browsers
    # give the microphone (talking back from the Live page) only to an HTTPS page.
    # web_tls_names: the addresses it's reached at, comma-separated (the server's IP).
    web_tls: bool = False
    web_tls_names: str = ""
    # Defaults to rtsp://go2rtc:8554/<camera_name> when unset.
    audio_rtsp_url: str = ""

    # Visit timing
    dwell_seconds: float = 1.0
    # Comma-separated Frigate zones (e.g. "porch"); a person must enter one
    # before a visit starts. Empty: anywhere in view.
    trigger_zones: str = ""
    # A Frigate zone at the front door (e.g. "door"). Someone who appears there is
    # the household coming out: if they walk out of it within
    # leaving_watch_seconds they aren't greeted, nor are person events in the
    # leaving_cooldown_seconds after (Frigate can split one person's track).
    # Empty: greet everyone.
    door_zone: str = ""
    leaving_watch_seconds: float = 3.0
    # Someone who appears at the door with nobody else on camera then, or this long
    # before, came out of the house: not greeted however long they stay at the door.
    came_out_quiet_seconds: float = 30.0
    # A delivery often takes several trips. Once one was seen (a package left at the door,
    # or a visitor who said they're delivering), people walking up this soon after it, or
    # after the last trip, are the courier again: not greeted. Ends when the package is
    # picked up. 0 disables.
    delivery_trip_seconds: float = 180.0
    leaving_cooldown_seconds: float = 15.0
    # Someone who walks up this soon after someone left by the door is most likely
    # them coming back in (from the car or the mailbox): not greeted. 0 disables.
    return_window_seconds: float = 120.0
    # A Frigate zone where packages are left (e.g. "porch"): after each visit,
    # Gatekeeper checks it for a package left or taken (packages.py). Empty: off.
    package_zone: str = ""
    # How long to wait for the visitor to start answering after a prompt.
    listen_seconds: int = 4

    # Recognizing the household by face (face_watch.py). Off by default: faces are
    # biometric data. The owner enrolls people, with their consent
    # (python -m gatekeeper.face_admin); someone recognized isn't greeted, or their
    # visit stops if it has started. A doorbell press still greets.
    face_recognition: bool = False
    # The camera's high-resolution stream. Defaults to audio_rtsp_url (go2rtc's main
    # stream, which has the video too).
    face_rtsp_url: str = ""
    # A face matches an enrolled person at this cosine similarity (0-1) or more; this
    # many matches make someone that person. On the rig, different people in the
    # household never scored above 0.45.
    face_match_threshold: float = 0.5
    face_min_matches: int = 2
    # Narrower faces (pixels, in the high-resolution frame) are too small to tell.
    face_min_width: float = 80.0
    face_dir: Path = Path("/faces")
    # The faces of people who went in by the door, kept this long for the owner to
    # enroll (0 = keep until enrolled or purged).
    face_candidate_days: float = 14.0
    face_model_dir: Path = Path.home() / ".cache" / "gatekeeper" / "faces"

    # What sees the door and starts the visits: "frigate" (its MQTT events) or "own" (our
    # own perception, own_service.py: Frigate isn't needed; with frigate_api set, Frigate
    # only stands in for zones not drawn here and for older visits' pictures).
    perception: Literal["frigate", "own"] = "frigate"
    # Gatekeeper's own perception (own_perception.py): it reads the camera's
    # low-resolution stream and finds and tracks people itself (YOLOX), to replace
    # Frigate on our hub. "shadow": it runs next to Frigate and only logs its tracks,
    # for the daily report to compare; Frigate still drives the visits.
    own_perception: Literal["off", "shadow"] = "off"
    # The camera's low-resolution stream. Defaults to audio_rtsp_url + "_sub" (go2rtc's
    # <camera_name>_sub).
    own_perception_url: str = ""
    # Frames looked at per second, at most: the detector runs on those where something
    # moved, or while someone is tracked.
    own_perception_fps: float = 5.0
    # The detector's score (0-1) for a person.
    own_perception_threshold: float = 0.3
    # Gatekeeper's own recordings (recording.py): a clip (MP4) while own perception sees
    # someone, from the high-resolution stream (face_rtsp_url) as it came and the
    # microphone, and a picture of each person. Needs own_perception.
    recording: bool = False
    clip_dir: Path = Path("/clips")
    # Days clips are kept (0 = forever). They hold visitors' faces and voices.
    clip_retention_days: float = 7.0
    own_perception_threads: int = 2
    # "auto": an NVIDIA GPU if onnxruntime has CUDA (the GPU image) and it works, else
    # the CPU.
    own_perception_device: Literal["auto", "cpu", "cuda"] = "auto"
    # Gatekeeper's guess at each greeted visitor, a delivery or not (visitor_type.py), in
    # shadow: kept for the Visits page and the daily report, nothing at the door yet.
    # Downloads SigLIP (~200 MB) once.
    delivery_guess: bool = False
    # Hearing a knock at the door (knock.py): "shadow" logs each knock, for the Visits
    # page and the daily report; nothing at the door yet. Needs the audio tap, and an
    # NVIDIA GPU to be quick (the classifier takes seconds on a CPU). Downloads AST
    # (~350 MB) once.
    knock_listener: Literal["off", "shadow"] = "off"
    # The classifier's "Knock" score (0-1) that counts as one.
    knock_threshold: float = 0.5
    model_dir: Path = Path.home() / ".cache" / "gatekeeper" / "models"

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
    # "auto": an NVIDIA GPU (CUDA) if there is one, else the CPU.
    whisper_device: Literal["auto", "cpu", "cuda"] = "auto"
    # "auto": int8 on the CPU, int8_float16 on a GPU; or any CTranslate2 compute type.
    whisper_compute_type: str = "auto"
    # Words and names Whisper should expect, comma-separated (e.g. local
    # candidates' names, which it otherwise mishears).
    whisper_hotwords: str = ""
    kokoro_voice: str = "af_heart"

    # What Gatekeeper says. The greeting: one of GREETING_LINES at random
    # (GREETING_PRESSED_LINES too after a doorbell press), each a JSON list to replace them.
    # GREETING, if set, is the only one. Every greeting should say the property is monitored.
    greeting: str = ""
    greeting_lines: list[str] = list(GREETING_LINES)
    greeting_pressed_lines: list[str] = list(GREETING_PRESSED_LINES)
    # Said when the visitor doesn't answer the greeting: one of NO_ANSWER_LINES at random
    # (NO_ANSWER_PRESSED_LINES too after a doorbell press); each a JSON list to replace
    # them. REPLY_NO_ANSWER, if set, is the only line.
    reply_no_answer: str = ""
    no_answer_lines: list[str] = list(NO_ANSWER_LINES)
    no_answer_pressed_lines: list[str] = list(NO_ANSWER_PRESSED_LINES)
    # Said instead to someone already walking away when the greeting gets no answer: one
    # of GOODBYE_LINES at random (a JSON list replaces them); REPLY_GOODBYE, if set, is
    # the only one.
    reply_goodbye: str = ""
    goodbye_lines: list[str] = list(GOODBYE_LINES)
    reply_delivery: str = "Thank you. Please leave the package at the door."
    reply_sales: str = "No solicitation. Please leave the property."
    reply_maintenance: str = "Please wait while I notify the resident."
    reply_generic: str = "Thank you. Please wait while I notify the resident."
    # Said when the doorbell is pressed during a visit's cooldown.
    reply_pressed: str = "The resident has already been notified."

    # Conversations (dialogue.py). They state the resident's position; have a
    # lawyer review any change to the law-enforcement lines.
    phrase_emergency_wait: str = "I have alerted the resident. Please wait."
    # Said before the next question when the officer's first sentence already gave
    # the reason, so police_ask_reason (which starts with it) isn't asked.
    phrase_police_disclosure: str = (
        "This is an automated assistant, and this conversation is recorded."
    )
    phrase_police_ask_reason: str = (
        "This is an automated assistant, and this conversation is recorded. "
        "What is the reason for your visit, and do you have a warrant?"
    )
    phrase_police_ask_warrant: str = "Do you have a warrant? Please answer yes or no."
    phrase_police_ask_identity: str = (
        "Please state your agency, your name, and your badge number."
    )
    phrase_police_show_warrant: str = (
        "Please hold the warrant close to the camera and keep it still. "
        "Is it signed by a judge?"
    )
    # The photos take a few seconds: said if they answer before they're all taken,
    # then the second line once they are.
    phrase_police_hold_warrant: str = (
        "Please keep holding it there while I look it over. I'll let you know when I'm finished."
    )
    phrase_police_warrant_done: str = "I'm finished. You can lower it now."
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

    # Quick replies: buttons on the notification that make Gatekeeper say one of
    # these lines at the door (quick_reply.py). QUICK_REPLIES picks the buttons, in
    # order (ntfy shows up to 3); empty turns them off. A reply is only said for a
    # visit notified in the last quick_reply_window_seconds.
    quick_replies: str = "coming,later,no_thanks"
    quick_reply_window_seconds: float = 300.0
    quick_reply_coming: str = "The resident is coming to the door. Please wait a moment."
    quick_reply_later: str = (
        "The resident can't come to the door right now. Please come back another time."
    )
    quick_reply_no_thanks: str = "The resident isn't interested, thank you. Have a good day."
    # The ntfy server as the phone reaches it (default NTFY_URL), and the topic the
    # buttons post to (default <NTFY_TOPIC>-replies). Gatekeeper reads that topic
    # with NTFY_TOKEN. Anyone may post to it: each button carries a signature.
    ntfy_public_url: str = ""
    ntfy_reply_topic: str = ""

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
    # The web page (web.py): the household's faces, to enroll them. Off unless
    # web_password is set (HTTP Basic, any user name).
    web_port: int = 8098
    web_password: SecretStr = SecretStr("")
    hang_exit_seconds: float = 300.0

    # Logging
    log_level: str = "INFO"
    log_format: Literal["text", "json"] = "text"

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if not self.audio_rtsp_url:
            self.audio_rtsp_url = f"rtsp://go2rtc:8554/{self.camera_name}"
        if not self.face_rtsp_url:
            self.face_rtsp_url = self.audio_rtsp_url
        if not self.own_perception_url:
            self.own_perception_url = f"{self.audio_rtsp_url}_sub"
        if not self.live_stream:
            self.live_stream = f"{self.camera_name}_live"
        if not self.live_hd_stream:
            self.live_hd_stream = f"{self.camera_name}_live_hd"
        if not self.ntfy_public_url:
            self.ntfy_public_url = self.ntfy_url
        if not self.ntfy_reply_topic:
            self.ntfy_reply_topic = f"{self.ntfy_topic}-replies"
        unknown = set(self.quick_reply_list) - set(QUICK_REPLY_KEYS)
        if unknown:
            raise ValueError(
                f"QUICK_REPLIES: unknown {', '.join(sorted(unknown))}; "
                f"choose from {', '.join(QUICK_REPLY_KEYS)}"
            )
        if len(self.quick_reply_list) > 3:
            raise ValueError("QUICK_REPLIES: at most 3 (ntfy shows no more buttons)")
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
        """Reply text by reply key (see ``classify.classify_response``); the no-answer
        line is one of ``no_answer_texts``."""
        return {
            key: getattr(self, f"reply_{key}") for key in REPLY_KEYS if key != "no_answer"
        }

    def no_answer_texts(self, pressed: bool | None = None) -> dict[str, str]:
        """The no-answer lines by phrase key: "no_answer" alone with REPLY_NO_ANSWER, else
        no_answer_1... and, after a press (or with ``pressed`` None: all of them, to
        pre-synthesize), no_answer_pressed_1..."""
        return _pool(
            "no_answer",
            self.reply_no_answer,
            self.no_answer_lines,
            self.no_answer_pressed_lines,
            pressed,
            "Please tell me why you're here.",
        )

    def goodbye_texts(self, pressed: bool | None = None) -> dict[str, str]:
        """The send-offs by phrase key: "goodbye" alone with REPLY_GOODBYE, else
        goodbye_1..."""
        return _pool(
            "goodbye",
            self.reply_goodbye,
            self.goodbye_lines,
            [],
            pressed,
            "Thanks for stopping by!",
        )

    def greeting_texts(self, pressed: bool | None = None) -> dict[str, str]:
        """The greetings by phrase key, as ``no_answer_texts``: "greeting" alone with
        GREETING, else greeting_1... and greeting_pressed_1..."""
        return _pool(
            "greeting",
            self.greeting,
            self.greeting_lines,
            self.greeting_pressed_lines,
            pressed,
            "Hello. This property is monitored. Please state the purpose of your visit.",
        )

    @property
    def phrases(self) -> dict[str, str]:
        """Conversation lines by key (see ``dialogue.py``)."""
        return {key: getattr(self, f"phrase_{key}") for key in PHRASE_KEYS}

    @property
    def package_state_file(self) -> Path:
        """The packages at the door, kept across restarts."""
        return self.log_dir / "packages.json"

    @property
    def package_baseline_dir(self) -> Path:
        """The doorstep in each kind of light lately (packages.Baselines)."""
        return self.log_dir / "package-baselines"

    @property
    def quick_reply_list(self) -> list[str]:
        return [k.strip() for k in self.quick_replies.split(",") if k.strip()]

    @property
    def quick_reply_phrases(self) -> dict[str, str]:
        """Quick reply text by key, for every key (offered or not)."""
        return {key: getattr(self, f"quick_reply_{key}") for key in QUICK_REPLY_KEYS}

    @property
    def gallery_file(self) -> Path:
        """The people the owner enrolled (faces.Gallery)."""
        return self.face_dir / "gallery.json"

    @property
    def candidate_dir(self) -> Path:
        """Faces kept for enrollment (face_watch.FaceWatcher.keep)."""
        return self.face_dir / "candidates"

    @property
    def knocks_file(self) -> Path:
        """The knocks heard (knock.py)."""
        return self.log_dir / "knocks.jsonl"

    @property
    def visit_guesses_file(self) -> Path:
        """Gatekeeper's guesses at visitors (visitor_type.py)."""
        return self.log_dir / "visit-guesses.json"

    @property
    def zones_file(self) -> Path:
        """Gatekeeper's own zones, drawn on the Zones page (zones.py)."""
        return self.log_dir / "zones.json"

    @property
    def visit_labels_file(self) -> Path:
        """The owner's labels for visits, from the Visits page (visits.py)."""
        return self.log_dir / "visit-labels.json"

    @property
    def snapshot_dir(self) -> Path:
        """Photos kept during conversations (e.g. a warrant held up to the camera)."""
        return self.audio_dir / "snapshots"


def _pool(
    kind: str, single: str, lines: list[str], pressed_lines: list[str], pressed, fallback: str
) -> dict[str, str]:
    """A personality's lines by phrase key: ``kind`` alone for a single line set in .env,
    else ``kind_1``... and (after a press, or ``pressed`` None for all) ``kind_pressed_1``..."""
    if single:
        return {kind: single}
    out = {f"{kind}_{n}": t for n, t in enumerate(lines, 1)}
    if pressed is not False:
        out |= {f"{kind}_pressed_{n}": t for n, t in enumerate(pressed_lines, 1)}
    return out or {kind: fallback}
