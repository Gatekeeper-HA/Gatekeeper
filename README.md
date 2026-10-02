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
 Gatekeeper
  ├── Kokoro TTS synthesizes greeting
  ├── WebRTC → go2rtc → camera speaker (plays greeting)
  ├── ffmpeg captures visitor response via RTSP mic
  ├── Whisper STT transcribes response
  ├── Classifies intent (delivery / solicitor / service / other / no response)
  └── Plays contextual reply through camera speaker
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
DATA_DIR=./data              # Where recordings, audio, logs and caches are stored
TZ=America/Chicago
```

Then build and start everything:

```bash
docker compose up -d --build
```

Configs (`frigate/config/config.yml`, `go2rtc/go2rtc.yaml`, `mosquitto/config/`) are read
from the repo. Everything the services write lives under `DATA_DIR`.

## Configuration

Gatekeeper is configured via environment variables, set in `docker-compose.yml` (or the
add-on options in Home Assistant). Unset or empty variables use the default.

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `mqtt` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `frigate/events` | Frigate event topic |
| `CAMERA_NAME` | `front_door` | Must match the camera name in your Frigate config |
| `GO2RTC_API` | `http://go2rtc:1984` | go2rtc API endpoint |
| `GO2RTC_TALK_STREAM` | `front_door_talk` | go2rtc stream name for talkback |
| `AUDIO_RTSP_URL` | `rtsp://go2rtc:8554/<CAMERA_NAME>` | RTSP stream for capturing visitor audio |
| `DWELL_SECONDS` | `1` | Seconds a person must be visible before triggering |
| `LISTEN_SECONDS` | `4` | How long to record the visitor's response |
| `SESSION_TTL_SECONDS` | `120` | Forget a Frigate event this long after its last update |
| `SWEEP_INTERVAL_SECONDS` | `1` | How often sessions are checked for dwell and expiry |
| `COOLDOWN_SECONDS` | `90` | After a visit, new person events this soon are merged into it instead of greeting again |
| `VISIT_TIMEOUT_SECONDS` | `60` | Give up on a visit (greet, listen, reply) after this long |
| `WHISPER_MODEL` | `tiny` | Whisper model size (`tiny`, `base`, `small`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` for CPU, `float32` if issues arise |
| `KOKORO_VOICE` | `af_heart` | TTS voice (see voices below) |
| `GREETING` | *Hello. This property is monitored. Please state the purpose of your visit.* | What Gatekeeper says when a visitor is detected |
| `REPLY_DELIVERY` | *Thank you. Please leave the package at the door.* | Reply to a delivery |
| `REPLY_SALES` | *No solicitation. Please leave the property.* | Reply to a solicitor |
| `REPLY_MAINTENANCE` | *Please wait while I notify the resident.* | Reply to a service visit |
| `REPLY_GENERIC` | *Thank you. Please wait while I notify the resident.* | Reply to any other answer |
| `REPLY_NO_ANSWER` | *You are being recorded. Please state your purpose or leave the property.* | Reply to silence or a one-word answer |
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
transcript, and classifies what the visitor said by whole words and phrases (English only).
Categories are checked top to bottom; the first match wins.

| Classification (`events.jsonl`) | Matches, for example | Reply setting |
|----------------|----------|-------|
| `solicitor` | selling, sales, canvassing, campaign, petition, survey, donations, church… | `REPLY_SALES` |
| `likely_delivery` | delivery, package, parcel, FedEx, UPS, USPS, Amazon, DoorDash, Uber Eats, groceries… | `REPLY_DELIVERY` |
| `service_visit` | repair, maintenance, technician, plumber, electrician, meter, appointment, install… | `REPLY_MAINTENANCE` |
| `cooperative_other` | Any other answer of two or more words | `REPLY_GENERIC` |
| `no_response` | Silence or a single word | `REPLY_NO_ANSWER` |

Solicitors are checked first, so "I'm selling Amazon gift cards" is a solicitor, not a
delivery.

## Health

`GET http://<host>:8099/healthz` returns `200` with `{"status": "ok", ...}` when Gatekeeper
is connected to MQTT, its session loop is running and no visit is stuck; otherwise `503`
with the problems listed. The image's Docker `HEALTHCHECK` uses it, so `docker compose ps`
shows `healthy`/`unhealthy`. Docker doesn't restart unhealthy containers by itself, so on an
internal hang lasting `HANG_EXIT_SECONDS` Gatekeeper exits and `restart: unless-stopped`
brings it back. An MQTT outage only reports unhealthy, since the client reconnects by itself.

## Visit log

Every visit adds one JSON line to `events.jsonl`:

```json
{"ts": 1790897594.14, "event_id": "1790897570.2776-z44iqa", "camera": "front_door",
 "outcome": "completed", "classification": "likely_delivery",
 "transcript": "I have a package.", "response": "Thank you. Please leave the package at the door."}
```

`outcome` is `completed`, `reply_failed`, `talkback_busy` (another visit was in progress),
`talkback_failed`, `talkback_unavailable`, `timeout` or `error`.

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

## License

AGPL-3.0 — see [LICENSE](LICENSE)
