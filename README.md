# Gatekeeper

AI-powered doorbell assistant. When Frigate detects a person at your door, Gatekeeper greets them through the camera speaker, listens for their response, classifies their intent, and replies — all processed locally on your hardware.

## How it works

```
Frigate detects person
      │
      ▼
  MQTT event
      │
      ▼
 Gatekeeper  (always listening to the camera's mic via go2rtc: the audio tap)
  ├── WebRTC → go2rtc → camera speaker: plays the greeting (one connection per visit)
  ├── Voice activity detection: waits until the visitor has finished answering
  ├── Whisper STT transcribes the answer
  ├── Classifies intent (delivery / solicitor / service / other / no response)
  └── Plays a contextual reply through the camera speaker
```

## Stack

| Component | Role |
|-----------|------|
| [Frigate](https://frigate.video) | Person detection, publishes MQTT events |
| [go2rtc](https://github.com/AlexxIT/go2rtc) | RTSP proxy, WebRTC talkback to camera speaker |
| [Eclipse Mosquitto](https://mosquitto.org) | MQTT broker |
| [Kokoro TTS](https://github.com/hexgrad/kokoro) | Local text-to-speech |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Local speech-to-text (Whisper tiny, int8) |
| [aiortc](https://github.com/aiortc/aiortc) | WebRTC for audio talkback |

## Prerequisites

- Docker and Docker Compose
- A camera with RTSP streaming and audio talkback (backchannel) support
- Frigate configured with the camera

## Quick start

```bash
git clone https://github.com/Gatekeeper-HA/Gatekeeper.git
cd Gatekeeper
cp .env.example .env
```

Edit `.env` with your values:

```env
SERVER_IP=192.168.x.x       # LAN IP of the machine running Docker
CAMERA_IP=192.168.x.x       # LAN IP of your camera
CAMERA_USER=admin            # Camera RTSP username
CAMERA_PASS=yourpassword     # Camera RTSP password
MQTT_FRIGATE_PASSWORD=...    # One MQTT password per account: openssl rand -hex 16
MQTT_GATEKEEPER_PASSWORD=...
MQTT_HOMEASSISTANT_PASSWORD=...
DATA_DIR=./data              # Where recordings, audio, logs and caches are stored
TZ=America/Chicago
```

Add the notification credentials (the argument is the password you'll type into the ntfy
phone app):

```bash
scripts/ntfy-auth.sh 'a-password-for-the-phone-app' >> .env
```

Then build and start everything:

```bash
docker compose up -d --build
```

Configs (`frigate/config/config.yml`, `go2rtc/go2rtc.yaml`, `mosquitto/config/`) are read
from the repo. Everything the services write lives under `DATA_DIR`.

### Ports on your network

Only services that require a login are published, on `SERVER_IP`:

| Port | Service | Access |
|------|---------|--------|
| 8971 | Frigate UI and API | HTTPS, Frigate login |
| 1883 | MQTT | Per-account passwords (see [MQTT](#mqtt)) |
| 8090 | ntfy notifications | Gatekeeper token / `phone` user |
| 8123 | Home Assistant (optional `ha` profile) | Home Assistant login |

Frigate's unauthenticated API (5000) and go2rtc (API/UI, RTSP restream, WebRTC) are used
only inside Docker. Without a login, anyone on the network could watch the doorbell or
talk through its speaker.

## Configuration

Gatekeeper is configured via environment variables, set in `docker-compose.yml` (or the
add-on options in Home Assistant). Unset or empty variables use the default.

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `mqtt` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `frigate/events` | Frigate event topic |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | *(none)* | MQTT login. The compose stack sets `gatekeeper` and `MQTT_GATEKEEPER_PASSWORD` |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix. Empty disables discovery |
| `NTFY_URL` | *(none)* | ntfy server for notifications; empty disables ntfy. The compose stack runs one (`http://ntfy`) |
| `NTFY_TOPIC` | `doorbell` | ntfy topic |
| `NTFY_TOKEN` | *(none)* | ntfy access token (`NTFY_GATEKEEPER_TOKEN` in the compose stack) |
| `FRIGATE_API` | `http://frigate:5000` | Frigate API, for the visitor's snapshot in notifications |
| `REOLINK_HOST`, `REOLINK_USERNAME`, `REOLINK_PASSWORD` | *(none)* | Reolink doorbell for button presses (login over HTTPS, push events on port 9000). Empty host disables. The compose stack uses the camera's `.env` values |
| `CAMERA_NAME` | `front_door` | Must match the camera name in your Frigate config |
| `GO2RTC_API` | `http://go2rtc:1984` | go2rtc API endpoint |
| `GO2RTC_TALK_STREAM` | `front_door_talk` | go2rtc stream name for talkback |
| `AUDIO_RTSP_URL` | `rtsp://go2rtc:8554/<CAMERA_NAME>` | RTSP stream for capturing visitor audio |
| `DWELL_SECONDS` | `1` | Seconds a person must be visible before triggering |
| `TRIGGER_ZONES` | *(none)* | Comma-separated Frigate zones (e.g. `porch`). A person must enter one before Gatekeeper greets them, and the dwell counts from entering it. Empty: anywhere in view. Doorbell presses ignore it |
| `LISTEN_SECONDS` | `4` | How long to wait for the visitor to start answering after the greeting (or, without the audio tap, how long to record after it) |
| `AUDIO_TAP` | `true` | Read the camera's audio all the time and stop listening when the visitor stops talking. `false`: record a fixed window per prompt |
| `VAD_THRESHOLD` | `0.5` | Speech probability (0–1) above which audio counts as the visitor talking |
| `END_SILENCE_MS` | `800` | Silence after the visitor's last word that ends their answer. If they start again while it's being transcribed, Gatekeeper listens on |
| `MAX_ANSWER_SECONDS` | `15` | Cut off an answer longer than this |
| `TALKBACK_SESSION` | `visit` | `visit`: one talkback connection per visit. `clip`: a new one per phrase (slower; for cameras that misbehave with a long connection) |
| `SESSION_TTL_SECONDS` | `120` | Forget a Frigate event this long after its last update |
| `SWEEP_INTERVAL_SECONDS` | `1` | How often sessions are checked for dwell and expiry |
| `COOLDOWN_SECONDS` | `90` | After a visit, new person events this soon are merged into it instead of greeting again |
| `VISIT_TIMEOUT_SECONDS` | `180` | Give up on a visit (greet, listen, reply; a whole conversation) after this long |
| `LEAVE_CHECK_SECONDS` | `20` | After asking an officer without a judge-signed warrant to leave, wait this long; if Frigate still sees someone on the porch, ask again more firmly |
| `WHISPER_MODEL` | `tiny` | Whisper model (`tiny`, `base.en`, `small.en`, ...). On a 4-core CPU, `base.en` takes ~3.4 s per visit vs ~1.8 s for `tiny`, but hears noticeably better; the compose stack uses `base.en` |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` for CPU, `float32` if issues arise |
| `WHISPER_HOTWORDS` | *(none)* | Comma-separated words and names Whisper should expect, e.g. local candidates' names |
| `KOKORO_VOICE` | `af_heart` | TTS voice (see voices below) |
| `GREETING` | *Hello. This property is monitored. Please state the purpose of your visit.* | What Gatekeeper says when a visitor is detected |
| `REPLY_DELIVERY` | *Thank you. Please leave the package at the door.* | Reply to a delivery |
| `REPLY_SALES` | *No solicitation. Please leave the property.* | Reply to a solicitor |
| `REPLY_MAINTENANCE` | *Please wait while I notify the resident.* | Reply to a service visit |
| `REPLY_GENERIC` | *Thank you. Please wait while I notify the resident.* | Reply to any other answer |
| `REPLY_NO_ANSWER` | *You are being recorded. Please state your purpose or leave the property.* | Reply to silence or a one-word answer |
| `REPLY_PRESSED` | *The resident has already been notified.* | Said when the doorbell is pressed during a visit's cooldown |
| `PHRASE_EMERGENCY_WAIT` | *I have alerted the resident. Please wait.* | To someone reporting an emergency |
| `PHRASE_POLICE_ASK_REASON` | *This is an automated assistant, and this conversation is recorded. What is the reason for your visit, and do you have a warrant?* | First question to law enforcement |
| `PHRASE_POLICE_ASK_WARRANT` | *Do you have a warrant? Please answer yes or no.* | If the first answer didn't say |
| `PHRASE_POLICE_ASK_IDENTITY` | *Please state your agency, your name, and your badge number.* | |
| `PHRASE_POLICE_SHOW_WARRANT` | *Please hold the warrant up to the camera. Is it signed by a judge?* | Photos are taken while they answer |
| `PHRASE_POLICE_NOTIFYING` | *Thank you. I am notifying the resident now. Please wait.* | After a judge-signed warrant and their ID |
| `PHRASE_POLICE_LEAVE` | *The resident does not consent to entry or a search without a warrant signed by a judge. Please leave the property.* | Otherwise |
| `PHRASE_POLICE_LEAVE_AGAIN` | *You have been asked to leave. The resident does not consent to your presence, this conversation is recorded, and legal action will be taken if you remain.* | If they're still there `LEAVE_CHECK_SECONDS` later |
| `PHRASE_CIVIC_ASK_IDENTITY` | *Thanks for stopping by. Could you tell me your name, and who you're with?* | First question to a candidate or canvasser |
| `PHRASE_CIVIC_ASK_MESSAGE` | *What message would you like me to pass on to the resident?* | |
| `PHRASE_CIVIC_THANKS` | *Thank you. I'll make sure the resident gets your message. Feel free to leave any literature at the door.* | |
| `AUDIO_DIR` | `/audio` | Visitor clips (`in/`) and synthesized speech (`out/`) |
| `LOG_DIR` | `/logs` | Directory for `events.jsonl` |
| `AUDIO_RETENTION_DAYS` | `7` | Delete visitor recordings (and per-visit synthesized speech) older than this. `0` keeps them forever |
| `EVENT_LOG_RETENTION_DAYS` | `30` | Delete rotated daily visit logs older than this. `0` keeps them forever |
| `HEALTH_PORT` | `8099` | Port for `GET /healthz` (200 when healthy, 503 otherwise, with details as JSON). `0` disables it |
| `HANG_EXIT_SECONDS` | `300` | Exit (so Docker restarts Gatekeeper) when the session loop has stalled or a visit is stuck for this long. `0` disables it |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_FORMAT` | `text` | `text`, or `json` for one JSON object per line. Every line carries the visit's Frigate `event_id` |

### Available voices

| Voice | Description |
|-------|-------------|
| `af_heart` | Female, warm (default) |
| `af_bella` | Female, bright |
| `af_sarah` | Female, natural |
| `am_michael` | Male, neutral |
| `am_adam` | Male, deep |
| `bm_george` | British male |
| `bf_emma` | British female |

## go2rtc setup

Your `go2rtc.yaml` must define a talkback stream with `backchannel=1`:

```yaml
streams:
  front_door: rtsp://${CAMERA_USER}:${CAMERA_PASS}@${CAMERA_IP}:554/h264Preview_01_main#backchannel=0
  front_door_talk: rtsp://${CAMERA_USER}:${CAMERA_PASS}@${CAMERA_IP}:554/h264Preview_01_main#backchannel=1
```

The stream names must match `CAMERA_NAME` and `GO2RTC_TALK_STREAM` in docker-compose.

## Visitor classification

Gatekeeper records from the start of the greeting, cuts the greeting's own echo out of the
transcript (if the visitor says nothing, `REPLY_NO_ANSWER` asks again and Gatekeeper listens
once more), and classifies what the visitor said by whole words and phrases (English only).
Categories are checked top to bottom; the first match wins.

| Classification (`events.jsonl`) | Matches, for example | What happens |
|----------------|----------|-------|
| `emergency` | emergency, welfare check, 911, ambulance, fire department, on fire, smoke, gas leak, injured… | A conversation: see [Conversations](#conversations) |
| `law_enforcement` | police, officer, sheriff, deputy, detective, trooper, warrant, FBI, homeland security, immigration… | A conversation |
| `civic` | candidate, campaign, running for, election, vote, register to vote, senator, city council, petition, canvassing, DFL, GOP… | A conversation |
| `solicitor` | selling, sales, survey, donations, charity, solar, church… | `REPLY_SALES` |
| `likely_delivery` | delivery, package, parcel, FedEx, UPS, USPS, Amazon, DoorDash, Uber Eats, groceries… | `REPLY_DELIVERY` |
| `service_visit` | repair, maintenance, technician, plumber, electrician, meter, appointment, install… | `REPLY_MAINTENANCE` |
| `cooperative_other` | Any other answer of two or more words | `REPLY_GENERIC` |
| `no_response` | Silence or a single word | `REPLY_NO_ANSWER` |

Solicitors are checked before deliveries, so "I'm selling Amazon gift cards" is a solicitor,
not a delivery. An emergency is checked first of all, so "Police, we're doing a welfare
check" is an emergency.

## Conversations

Some visitors get a short conversation instead of one reply. Every line is a setting
(`PHRASE_*`), and every answer is in the visit log (`dialogue`, plus what was learned in
`details`) and in the notification.

- **Candidates, campaigns and canvassers** (`civic`): Gatekeeper asks who they are and who
  they're with, then what message they'd like passed on (they can talk for up to 30 s). It
  thanks them, promises to pass it on, and suggests leaving literature at the door. You get
  one notification with their name and message.
- **Emergencies** (`emergency`): you get an urgent notification (ntfy priority 5) at once,
  and the visitor hears "I have alerted the resident. Please wait." Responders are never sent
  away.
- **Law enforcement** (`law_enforcement`):
  1. You get an urgent notification at once, before Gatekeeper says anything more.
  2. Gatekeeper says it's an automated assistant and that the conversation is recorded, then
     asks the reason for the visit and whether they have a warrant. If that's unclear, it
     asks yes or no. An emergency here switches to the emergency conversation.
  3. It asks for their agency, name and badge number.
  4. With a warrant, it asks them to hold it up to the camera and whether a judge signed it.
     It keeps 3 full-resolution photos from the camera's main stream (via go2rtc) in
     `audio/snapshots/` and attaches one to the notification.
  5. **A warrant signed by a judge:** "Thank you. I am notifying the resident now."
  6. **Otherwise:** "The resident does not consent to entry or a search without a warrant
     signed by a judge. Please leave the property." If Frigate still sees someone on the
     porch `LEAVE_CHECK_SECONDS` later, it says `PHRASE_POLICE_LEAVE_AGAIN`.
  7. A final notification says how it ended, with their ID, reason and warrant.

  These lines state the resident's position; they aren't legal advice. Have a lawyer review
  them (and any change you make) for where you live.

`WHISPER_HOTWORDS` helps Whisper with names it would otherwise mishear, such as local
candidates'.

## Doorbell button

With `REOLINK_HOST` set, Gatekeeper listens for presses of a Reolink doorbell's button
(the same push events Home Assistant's Reolink integration uses; no Home Assistant needed):

- A press while someone is waiting out `DWELL_SECONDS` greets them right away.
- A press before Frigate has detected anyone starts a visit anyway, with the camera's latest
  frame as the snapshot.
- A press during a visit or its cooldown sends a *Doorbell pressed* notification and, if the
  speaker isn't mid-visit, says `REPLY_PRESSED`.

Visit records carry `trigger`: `person` (Frigate detection) or `button`.

## Notifications

As soon as the visitor's answer is classified (before the reply plays), Gatekeeper sends a
notification with what they said, how it replied, and Frigate's snapshot of them. Visits
where Gatekeeper couldn't talk to the visitor (talkback failed, busy, timed out) are
notified too.

The compose stack runs a private [ntfy](https://ntfy.sh) server on port 8090. Access is
denied by default: only Gatekeeper's token can publish to the `doorbell` topic, and only
the `phone` user can read it. In the ntfy Android app:

1. **+** → *Use another server* → `http://<SERVER_IP>:8090`, topic `doorbell`.
2. Log in as `phone` with the password you gave `scripts/ntfy-auth.sh`.

The phone must be able to reach the server (home Wi-Fi, or a VPN when away).

## MQTT

The broker requires a login. The `mqtt-auth` service writes Mosquitto's password file from
the three `MQTT_*_PASSWORD` values in `.env` each time the stack starts, and
`mosquitto/config/acl` limits each account to its topics:

| Account | May use |
|---------|---------|
| `frigate` | `frigate/#` |
| `gatekeeper` | reads `frigate/#`; writes `gatekeeper/#` and `homeassistant/#` |
| `homeassistant` | everything (for the Home Assistant MQTT integration) |

Gatekeeper publishes (all retained):

| Topic | Payload |
|-------|---------|
| `gatekeeper/status` | `online`, or `offline` (last will) |
| `gatekeeper/<camera>/state` | `conversation` during a visit, else `idle` |
| `gatekeeper/<camera>/visit` | The latest visit record (JSON, as in `events.jsonl`) |

With Home Assistant's MQTT integration connected to the broker, a **Gatekeeper** device
appears automatically with a *Last visitor* sensor (classification, with the transcript and
reply as attributes) and a *Conversation* binary sensor.

## Health

`GET http://<host>:8099/healthz` returns `200` with `{"status": "ok", ...}` when Gatekeeper
is connected to MQTT, its session loop is running and no visit is stuck; otherwise `503`
with the problems listed. The image's Docker `HEALTHCHECK` uses it, so `docker compose ps`
shows `healthy`/`unhealthy`. Docker doesn't restart unhealthy containers by itself, so on an
internal hang lasting `HANG_EXIT_SECONDS` Gatekeeper exits and `restart: unless-stopped`
brings it back. An MQTT outage only reports unhealthy, since the client reconnects by itself.
`audio_tap` is `ok`, `down` or `off`; while it's down, visits fall back to recording a fixed
window, so it doesn't make Gatekeeper unhealthy.

## Visit log

Every visit adds one JSON line to `events.jsonl`:

```json
{"ts": 1790897594.14, "event_id": "1790897570.2776-z44iqa", "camera": "front_door",
 "outcome": "completed", "classification": "likely_delivery",
 "transcript": "I have a package.", "response": "Thank you. Please leave the package at the door."}
```

`outcome` is `completed`, `reply_failed`, `talkback_busy` (another visit was in progress),
`talkback_failed`, `talkback_unavailable`, `timeout`, `error`, or `pressed_during_visit`.
`trigger` is `person` or `button`. `turns` is how many times Gatekeeper listened: if the
visitor says nothing, the no-answer reply asks again and Gatekeeper listens once more.
`endpoint` is how listening ended: `answered` (they spoke, then fell silent), `no_input`,
`max`, `tap_lost`, or `window` (a fixed recording, without the audio tap). `reply_latency`
is the seconds from the end of their answer to the reply starting to play; the camera adds
its own ~2.4 s before it's heard.

## Data

| Path (under `DATA_DIR`) | Contents |
|-------------------------|----------|
| `gatekeeper/audio/in/` | Captured visitor audio clips |
| `gatekeeper/audio/out/` | Synthesized TTS files |
| `gatekeeper/logs/events.jsonl` | Today's visit log (JSON Lines); earlier days are rotated to `events-YYYY-MM-DD.jsonl` |
| `gatekeeper/cache/` | Kokoro and Whisper model cache |
| `frigate/config/` | Frigate database, model cache and secrets (`config.yml` comes from the repo) |
| `frigate/storage/` | Frigate recordings, clips and snapshots |
| `mosquitto/data/`, `mosquitto/log/` | MQTT broker persistence and log |

## Development

The app is the `gatekeeper` Python package in `src/gatekeeper/`. The Home Assistant add-on
([Gatekeeper-HA](https://github.com/Gatekeeper-HA/Gatekeeper-HA)) installs the same package
from a tagged release of this repo.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"     # core + test tools; no torch or aiortc needed
ruff check src tests
pytest --cov
```

The speech (`faster-whisper`, `kokoro`) and talkback (`aiortc`) dependencies are optional
extras, imported lazily, so the core logic is tested without them. The image installs
everything from `requirements.lock`; regenerate it after changing dependencies in
`pyproject.toml` (the command is at the top of the file).

```bash
# Rebuild after code or dependency changes
docker compose up -d --build gatekeeper

# View live logs
docker compose logs -f gatekeeper
```

## Contributing

Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Your first pull request
asks you to agree to the [Contributor License Agreement](CLA.md).

## License

AGPL-3.0 — see [LICENSE](LICENSE)
