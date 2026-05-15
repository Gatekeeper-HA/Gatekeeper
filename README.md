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
  ├── Classifies intent (delivery / sales / maintenance / unknown)
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
git clone https://github.com/your-username/gatekeeper.git
cd gatekeeper
cp .env.example .env
```

Edit `.env` with your values:

```env
SERVER_IP=192.168.x.x       # LAN IP of the machine running Docker
CAMERA_IP=192.168.x.x       # LAN IP of your camera
CAMERA_USER=admin            # Camera RTSP username
CAMERA_PASS=yourpassword     # Camera RTSP password
```

Copy configs to your server's Docker volume paths and start:

```bash
docker compose up -d
```

## Configuration

Gatekeeper is configured via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMERA_NAME` | `front_door` | Must match the camera name in your Frigate config |
| `GREETING` | *(see below)* | What Gatekeeper says when a visitor is detected |
| `DWELL_SECONDS` | `1` | Seconds a person must be visible before triggering |
| `LISTEN_SECONDS` | `4` | How long to record the visitor's response |
| `WHISPER_MODEL` | `tiny` | Whisper model size (`tiny`, `base`, `small`) |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` for CPU, `float32` if issues arise |
| `KOKORO_VOICE` | `af_heart` | TTS voice (see voices below) |
| `GO2RTC_API` | `http://go2rtc:1984` | go2rtc API endpoint |
| `GO2RTC_TALK_STREAM` | `front_door_talk` | go2rtc stream name for talkback |
| `AUDIO_RTSP_URL` | `rtsp://go2rtc:8554/front_door` | RTSP stream for capturing visitor audio |

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

Gatekeeper classifies visitors based on keywords in their response:

| Classification | Keywords | Reply |
|----------------|----------|-------|
| Delivery | fedex, ups, amazon, package, doordash… | Leave package at door |
| Sales | selling, soliciting, campaign, petition… | No solicitation |
| Maintenance | repair, service, technician, contractor… | Notifying resident |
| Generic (cooperative) | Any multi-word response | Notifying resident |
| No answer | Silence or single word | Warning + re-state purpose |

## Data

| Path (on server) | Contents |
|------------------|----------|
| `/docker/gatekeeper/audio/in/` | Captured visitor audio clips |
| `/docker/gatekeeper/audio/out/` | Synthesized TTS files |
| `/docker/gatekeeper/logs/events.jsonl` | Interaction log (JSON Lines) |
| `/docker/gatekeeper/cache/` | Kokoro and Whisper model cache |

## Development

The app directory is volume-mounted (`/docker/gatekeeper/app:/app/app`), so changes to `gatekeeper/app/main.py` on the server take effect on the next interaction without rebuilding the container. Rebuild is only needed when changing `requirements.txt` or `Dockerfile`.

```bash
# Rebuild after dependency changes
docker compose up -d --build gatekeeper

# View live logs
docker compose logs -f gatekeeper
```

## License

MIT — see [LICENSE](LICENSE)
