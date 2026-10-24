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
| 8090 | ntfy notifications | Gatekeeper token / `phone` user; anyone may post a quick reply (signed, see [Quick replies](#quick-replies)) |
| 8098 | Gatekeeper's [household faces](#household-faces) page | `GATEKEEPER_WEB_PASSWORD` (plain HTTP; off without a password) |
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
| `QUICK_REPLIES` | `coming,later,no_thanks` | The [quick reply](#quick-replies) buttons on a notification, in order (at most 3); empty turns them off |
| `QUICK_REPLY_WINDOW_SECONDS` | `300` | A quick reply is only said for a visit notified this recently |
| `QUICK_REPLY_COMING` | *The resident is coming to the door. Please wait a moment.* | Said for *I'm coming* |
| `QUICK_REPLY_LATER` | *The resident can't come to the door right now. Please come back another time.* | Said for *Not now* |
| `QUICK_REPLY_NO_THANKS` | *The resident isn't interested, thank you. Have a good day.* | Said for *No thanks* |
| `NTFY_PUBLIC_URL` | `NTFY_URL` | The ntfy server as your phone reaches it, for the quick reply buttons (the compose stack sets `http://<SERVER_IP>:8090`) |
| `NTFY_REPLY_TOPIC` | `<NTFY_TOPIC>-replies` | The topic the buttons post to and Gatekeeper reads |
| `FRIGATE_API` | `http://frigate:5000` | Frigate API, for the visitor's snapshot in notifications |
| `REOLINK_HOST`, `REOLINK_USERNAME`, `REOLINK_PASSWORD` | *(none)* | Reolink doorbell for button presses (login over HTTPS, push events on port 9000). Empty host disables. The compose stack uses the camera's `.env` values |
| `CAMERA_NAME` | `front_door` | Must match the camera name in your Frigate config |
| `GO2RTC_API` | `http://go2rtc:1984` | go2rtc API endpoint |
| `GO2RTC_TALK_STREAM` | `front_door_talk` | go2rtc stream name for talkback |
| `AUDIO_RTSP_URL` | `rtsp://go2rtc:8554/<CAMERA_NAME>` | RTSP stream for capturing visitor audio |
| `DWELL_SECONDS` | `1` | Seconds a person must be visible before triggering |
| `TRIGGER_ZONES` | *(none)* | Comma-separated Frigate zones (e.g. `porch`). A person must enter one before Gatekeeper greets them, and the dwell counts from entering it. Empty: anywhere in view. Doorbell presses ignore it |
| `DOOR_ZONE` | *(none)* | A Frigate zone at the front door (e.g. `door`). Someone who appears there is the household coming out: if they walk out of it within `LEAVING_WATCH_SECONDS` they aren't greeted or notified, only logged (`outcome: left`). Someone who stays at the door is greeted, and a doorbell press always greets. Someone greeted who then goes in by the door without saying anything (their track ends there, with nobody else on the porch) is the household coming home: Gatekeeper stops talking and sends a quiet notification, *Someone went inside by the front door* (`outcome: entered`). The compose stack draws one next to the doorbell |
| `LEAVING_WATCH_SECONDS` | `3` | How long someone who appears in the door zone has to walk out of it. On the rig, 90% of the people leaving did within 2.9 s |
| `LEAVING_COOLDOWN_SECONDS` | `15` | After someone leaves by the door, person events this soon count as them too (Frigate sometimes splits one person's track) |
| `PACKAGE_ZONE` | *(none)* | A Frigate zone where packages are left (e.g. `porch`): [package alerts](#package-alerts). The compose stack sets `porch` |
| `RETURN_WINDOW_SECONDS` | `120` | Someone who walks up this soon after someone left by the door is most likely them coming back in (from the car or the mailbox): not greeted or notified, logged as `returned`. A doorbell press still greets. `0` turns it off. The compose stack sets `240`: on the rig, the household's quick returns took up to 3 minutes |
| `FACE_RECOGNITION` | `false` | Recognize the household by face: [household faces](#household-faces). Off by default (biometric data); the compose stack reads it from `.env` |
| `FACE_MATCH_THRESHOLD` | `0.5` | Cosine similarity (0–1) to an enrolled face that counts as a match. On the rig, different people in the household never scored above 0.45 |
| `FACE_MIN_MATCHES` | `2` | Matches to the same person that make someone them |
| `FACE_MIN_WIDTH` | `80` | Faces narrower than this, in pixels of the high-resolution frame, are too small to tell |
| `FACE_RTSP_URL` | `AUDIO_RTSP_URL` | The camera's high-resolution stream (video) |
| `FACE_DIR` | `/faces` | Enrolled faces (`gallery.json`) and faces kept for enrollment (`candidates/`) |
| `WEB_PORT` | `8098` | The [household faces](#household-faces) page |
| `WEB_PASSWORD` | *(none)* | Its password (HTTP Basic, any user name). Without one the page is off. The compose stack reads `GATEKEEPER_WEB_PASSWORD` from `.env` |
| `FACE_CANDIDATE_DAYS` | `14` | How long the faces of people who went in by the door are kept for you to enroll |
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
| `WHISPER_DEVICE` | `auto` | `auto` runs Whisper on an NVIDIA GPU when there is one (see [GPU](#gpu-nvidia)), else on the CPU; or `cpu`, `cuda` |
| `WHISPER_COMPUTE_TYPE` | `auto` | `auto` is `int8` on the CPU and `int8_float16` on a GPU; or any CTranslate2 type (`float32` if issues arise) |
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
| `PHRASE_POLICE_ASK_REASON` | *This is an automated assistant, and this conversation is recorded. What is the reason for your visit, and do you have a warrant?* | First question to law enforcement, unless their first sentence already said why they came or whether they have a warrant |
| `PHRASE_POLICE_DISCLOSURE` | *This is an automated assistant, and this conversation is recorded.* | Said instead, right before the next question, when the reason question is skipped |
| `PHRASE_POLICE_ASK_WARRANT` | *Do you have a warrant? Please answer yes or no.* | If they haven't said |
| `PHRASE_POLICE_ASK_IDENTITY` | *Please state your agency, your name, and your badge number.* | |
| `PHRASE_POLICE_SHOW_WARRANT` | *Please hold the warrant close to the camera and keep it still. Is it signed by a judge?* | Six photos are taken while they answer; the sharpest goes with the notification |
| `PHRASE_POLICE_HOLD_WARRANT` | *Please keep holding it there while I look it over. I'll let you know when I'm finished.* | If they answer before the photos are all taken |
| `PHRASE_POLICE_WARRANT_DONE` | *I'm finished. You can lower it now.* | Once they are |
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

## GPU (NVIDIA)

With an NVIDIA GPU, `docker-compose.gpu.yml` moves the slow parts onto it.
- **Frigate** detects people with YOLOv9 instead of its small default CPU model, so it makes
  fewer mistakes (a bush at dawn, for example).
- **Whisper** uses a much larger model and transcribes a visitor's answer in a fraction of
  the time `base.en` takes on a CPU, which is most of the wait for a reply. On a GTX 1660 Ti
  with real visits, per answer:
  - `large-v3-turbo`: 1.1 s, and the most accurate (the override's default);
  - `small.en`: 0.5 s;
  - `base.en` on a 4-core CPU: 4.0 s.

The default image stays CPU-only.

1. **Install the NVIDIA driver and the
   [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)**,
   then run `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker. Check it
   with `docker run --rm --gpus all ubuntu nvidia-smi`.
2. **Export the detection model once:** `scripts/gpu/export-yolov9.sh` (a few minutes; YOLOv9-s
   at 320×320). It writes it to `${DATA_DIR}/frigate/config/model_cache/`. YOLOv9 is GPL-3.0;
   you export it yourself, and Gatekeeper doesn't ship it.
3. **Turn it on** by adding this to `.env`:
   ```
   COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml
   ```
   then run `docker compose up -d --build`. The Gatekeeper image builds with the CUDA
   libraries (`VARIANT=cuda`, about 4 GB more), and Frigate runs its `-tensorrt` image with
   `frigate/config/config.gpu.yml`. That's `config.yml` plus the GPU detector, and a test
   keeps the two in sync, so edit zones and masks in both.
4. **Check it:**
   - Frigate's *System* page lists the `onnx` detector, at about 20 ms per inference on a
     GTX 1660 Ti (its default model takes 80 ms or more on a CPU).
   - Gatekeeper's log says `loading Whisper model: … on cuda (int8_float16)`.
   - If CUDA doesn't work, Whisper falls back to the CPU and logs why.
5. **Optional, GPU video decoding:** Frigate still decodes the camera's video on the CPU. To
   move that to the GPU too, add `hwaccel_args: preset-nvidia` under the camera's `ffmpeg`
   in `config.gpu.yml`. It needs the driver's NVDEC library (`libnvcuvid.so.1`), which
   Ubuntu's headless and server driver packages leave out (install
   `libnvidia-decode-<version>` to add it). Without it, Frigate gets no frames at all: check
   that the camera shows about 5 fps on the *System* page after restarting Frigate.

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
     asks the reason for the visit and whether they have a warrant. If their first sentence
     already said why they came or whether they have a warrant, it only says the first part
     (`PHRASE_POLICE_DISCLOSURE`). If the warrant is unclear, it asks yes or no. An
     emergency here switches to the emergency conversation.
  3. It asks for their agency, name and badge number.
  4. With a warrant, it asks them to hold it close to the camera and keep it still, and
     whether a judge signed it. It takes 6 full-resolution photos half a second apart from
     the camera's main stream (via go2rtc), keeps them in `audio/snapshots/`, and attaches
     the sharpest to the notification. If they answer before the photos are all taken, it
     asks them to keep holding it and says when it's finished.
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

## Quick replies

A doorbell notification has buttons to answer the visitor without going to the door:
**I'm coming**, **Not now** and **No thanks**. Pressing one makes Gatekeeper say that line
at the door (`QUICK_REPLY_COMING`, `QUICK_REPLY_LATER`, `QUICK_REPLY_NO_THANKS`). If
Gatekeeper is still talking to the visitor, it says it right after.

- **Within 5 minutes** (`QUICK_REPLY_WINDOW_SECONDS`) of the notification. A button pressed
  later does nothing, so an old notification can't talk to whoever is at the door now.
- **Once** per button and visit.
- **Not on the interim alert** while an officer is being questioned; the final
  notification has them.

**How it works with ntfy:** each button posts the visit's id, the reply and a signature to the
`doorbell-replies` topic, and Gatekeeper follows that topic. The phone posts without logging
in, so the compose stack lets anyone post there (and only Gatekeeper read it). The signature
is made with a key Gatekeeper creates at startup, so only buttons from its own notifications
work. After a restart, older buttons stop working. On your own ntfy server, allow anonymous
writes to the reply topic (`ntfy access everyone doorbell-replies write-only`) and give
Gatekeeper's user read access to it.

**Home Assistant** gets a *Say: …* button per reply on the Gatekeeper device (see [MQTT](#mqtt)),
which answers the latest visit. Use them in a dashboard, or as actions of your own
actionable notifications.

Each reply is logged in `events.jsonl` as a `quick_reply` record with the visit's id
(see [Visit log](#visit-log)), and the daily report counts them.

## Package alerts

With `PACKAGE_ZONE` set, you get **Package at the front door** when a package is left
there, with a photo of it, and **Package picked up** when it's gone, with Frigate's photo
of who came.

A package only arrives or leaves with someone at the door, so the doorstep isn't watched
all the time. It's compared before and after each visit:

1. While nobody is on the porch, Gatekeeper keeps a recent frame from Frigate.
2. Once whoever came has been gone 15 s, it takes another frame and compares the zone in
   the two, after evening out the camera's exposure.
   - If the lighting changed while they were there (a porch light, the camera's night
     mode, a doorbell's white LED), it compares with the last frame from before the visit
     in the same light instead. Gatekeeper keeps one per kind of light it has seen in the
     last two days: night mode, colour, and each brightness. It learns them by itself,
     whatever the camera and lighting.
   - If it has never seen this light, it compares again every minute for 10 minutes, then
     waits up to 12 hours for the light from before the visit to come back.
3. A small open image model scores each changed patch as a package or not, before and
   after. The model is CLIP ViT-B/32 (MIT license): about 150 MB, downloaded once and
   cached with the other models, and about 0.2 s per patch on the CPU. A patch that looks
   like a package now, and didn't before, is a package.
4. After a later visit, a package whose patch changed and no longer looks like a package
   has been picked up.

Packages at the door are kept in `packages.json` (next to the visit log) across restarts,
and the doorstep frames for each kind of light in `package-baselines/`.
Home Assistant gets a *Package* binary sensor (see [MQTT](#mqtt)).

**How well it works depends on your camera's view of the doorstep.** On the rig's 189 visits
in a week with no package, it raised no alert. Packages left out of the camera's view
can't be seen, and a small one at the edge of the frame may be too few pixels: on the rig,
a shoebox in the corner by the door was about 32 px in Frigate's 640x480 frame, right at
the limit. To check yours, leave a box in the zone and walk away, by day and at night.
The log shows each check's scores (`doorstep change at …: package score …`), and the daily
report has the day's numbers.

## Household faces

With `FACE_RECOGNITION` on, Gatekeeper recognizes the people you enroll. When one of them
walks up, they aren't greeted, and you get a quiet notification, *Alex at the front door*.
If they're recognized only after the greeting started, the visit stops there, as long as
they haven't said anything. A doorbell press always gets a full visit.

**How it works.** Gatekeeper keeps the last few seconds of the camera's high-resolution
stream (2560x1920 on the Reolink). While Frigate sees someone, it decodes from just before
they appeared, a few frames a second. It finds faces with YuNet and recognizes them with
SFace, two small open models from OpenCV Zoo (MIT and Apache-2.0, about 40 MB, downloaded
once). Both run on the CPU, and decoding plus recognition uses about one core, only while
someone is on camera. Faces are found on a 640x480 copy, but each is read from the full
frame. That's why it does better than recognizing from Frigate's 640x480 detect stream:
a face is big enough to recognize while they're still on the walkway.

**What's stored.**
- **Enrolled people:** 128 numbers per face in `FACE_DIR/gallery.json`, no pictures.
- **Faces kept for enrollment:** for `FACE_CANDIDATE_DAYS`, in `FACE_DIR/candidates/`, up
  to 8 per visit. They come from people who went in by the front door (`entered`), came back
  soon after leaving (`returned`), were recognized, or walked up while you asked to capture
  faces. Each is kept twice: aligned at 112x112 for recognition, and as a sharper picture
  from the full frame for the page.
- **Nobody else's face is stored.** Visitors' faces are compared in memory and forgotten.
- **Purge:** `purge` deletes everything (below).

**Enrolling.** Only enroll people who agreed to be recognized. Faces are biometric data,
regulated in some places (Illinois, Texas, Washington, the EU and the UK). After a few days
of the household coming and going, open the faces page: `http://<SERVER_IP>:8098/`, with
`GATEKEEPER_WEB_PASSWORD` in `.env` (any user name).
- **Faces to enroll:** the kept faces, grouped by person. Untick any face that isn't them,
  pick or type their name, tick that they agreed, and enroll. Faces that aren't the
  household can be deleted.
  - **Grouping:** one track's faces are always one person. Tracks are then grouped by how
    alike their faces are.
  - **Names:** once someone is enrolled, a group that clearly looks like them comes with
    their name filled in. Otherwise the closest names are shown, and you choose.
- **Capture at the door:** for someone the camera hasn't caught going inside. Tap it, walk
  up to the doorbell, and look at it for a few seconds. Your faces appear on the page.
- **Enrolled:** who is enrolled, each with *Forget*. *Delete every face* purges everything.

The page has no scripts and works on a phone. Every change needs a token from the page,
so another website can't make one through your browser. It's plain HTTP on your network,
so don't publish its port beyond it.

The same from the command line:

```bash
docker exec -it gatekeeper python -m gatekeeper.face_admin candidates
# Look at gatekeeper/faces/review/group-<n>.jpg (under DATA_DIR), then:
docker exec -it gatekeeper python -m gatekeeper.face_admin enroll Alex 1 4 --consent
docker exec -it gatekeeper python -m gatekeeper.face_admin list
docker exec -it gatekeeper python -m gatekeeper.face_admin forget Alex
docker exec -it gatekeeper python -m gatekeeper.face_admin purge
```

One person usually comes out as several groups, for daylight, night and the porch light.
Enroll all of them under the same name: faces from more kinds of light recognize them more
often. Run `candidates` again after a week or two to add more.

**How well it works on the rig.** A week of greeted visits, each matched against the
faces from the other days: of 70 visits, 28 showed a face big and sharp enough, and 16
were recognized, 12 of them before the greeting. Nobody was taken for someone else. In
daylight the rig's camera looks into a bright sky, and faces on the walkway are often too
dark to recognize. At night, under the porch light, they're clear. The daily report counts
the recognitions and how often a face was seen.

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
| `gatekeeper/<camera>/package` | `on` while a package is at the door, else `off` (with [package alerts](#package-alerts)) |

Gatekeeper listens on `gatekeeper/<camera>/quick_reply` for a [quick reply](#quick-replies):
`coming`, `later` or `no_thanks` answers the latest visit; `{"reply": "coming", "event_id":
"..."}` answers a given one.

With Home Assistant's MQTT integration connected to the broker, a **Gatekeeper** device
appears automatically with a *Last visitor* sensor (classification, with the transcript and
reply as attributes), a *Conversation* binary sensor, a *Say: …* button per quick reply,
and, with package alerts, a *Package* binary sensor.

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
`talkback_failed`, `talkback_unavailable`, `timeout`, `error`, `pressed_during_visit`, or
`left` (someone came out of the front door and walked away; see `DOOR_ZONE`),
`returned` (someone walked up soon after; see `RETURN_WINDOW_SECONDS`), `entered`
(greeted, then went in by the front door without answering; see `DOOR_ZONE`), or
`household` (recognized by face; `details` has `name`, and `greeted`: whether the
greeting had started; see [household faces](#household-faces)).
`package_delivered` and `package_picked_up` lines record a [package](#package-alerts)
left or taken. They have `trigger` `package`, `event_id` `package-<time>` (the same for
both lines of one package), and `details` with the package's `box` in Frigate's 640x480
frame, its `score` and `since`.
A `quick_reply` line records a [quick reply](#quick-replies) to the visit with the same
`event_id`. Its `details` hold `reply`; `result` (`said`, `too_late`, `busy`: a visit held
the speaker until the window ended, or `failed`); `after_seconds` (since the notification);
and `present` (whether Frigate still saw someone).
`trigger` is `person`, `button`, `quick_reply` or `package`. `turns` is how many times Gatekeeper listened: if the
visitor says nothing, the no-answer reply asks again and Gatekeeper listens once more.
`endpoint` is how listening ended: `answered` (they spoke, then fell silent), `no_input`,
`max`, `tap_lost`, or `window` (a fixed recording, without the audio tap). `reply_latency`
is the seconds from the end of their answer to the reply (or a conversation's first
question) starting to play; the camera adds
its own ~2.4 s before it's heard.

## Data

| Path (under `DATA_DIR`) | Contents |
|-------------------------|----------|
| `gatekeeper/audio/in/` | Captured visitor audio clips |
| `gatekeeper/audio/out/` | Synthesized TTS files |
| `gatekeeper/logs/events.jsonl` | Today's visit log (JSON Lines); earlier days are rotated to `events-YYYY-MM-DD.jsonl` |
| `gatekeeper/cache/` | Kokoro, Whisper and face model cache |
| `gatekeeper/faces/` | [Household faces](#household-faces): enrolled people and faces kept for enrollment (mode 600) |
| `frigate/config/` | Frigate database, model cache and secrets (`config.yml` comes from the repo) |
| `frigate/storage/` | Frigate recordings, clips and snapshots |
| `mosquitto/data/`, `mosquitto/log/` | MQTT broker persistence and log |

## Running a rig

`scripts/rig/` keeps a live install checked and up to date. The project's own rig runs it
from cron, as a user in the `docker` group:

```
# Deploy every merge to main, within a minute or two
* * * * * /docker/stack/scripts/rig/deploy.sh >> ~/.local/state/gatekeeper/deploy.log 2>&1
# The daily report, and an hourly check that recordings have audio (it restarts Frigate)
0 1 * * * /docker/stack/scripts/rig/daily-report.sh >> ~/.local/state/gatekeeper/rig.log 2>&1
7 * * * * /docker/stack/scripts/rig/audio-check.sh >> ~/.local/state/gatekeeper/rig.log 2>&1
```

- **The daily report** (`daily-report.sh`; the checks are in `src/gatekeeper/report.py`)
  looks at the last 24 hours. It checks that:
  - every porch arrival was greeted, and every visit was notified within 3 s;
  - there were no hangs, restarts or errors in Gatekeeper's log;
  - recordings had audio;
  - Frigate got camera frames and detected in time;
  - Whisper ran on the device it should and answered fast enough;
  - every deploy went through.

  It lists every visit with what was heard and the reply, and the log's warnings and
  errors grouped by kind. A deploy that recreates a container keeps its old log first
  (`~/.local/state/gatekeeper/logs/`, two days), so the report still covers the whole day; a
  container recreated by hand only has its log since then, and the report says so. It sends the summary via ntfy and keeps the full report (JSON and
  Markdown) in `~/.local/state/gatekeeper/reports/`.
- **Recording audio** (`audio-check.sh`): after a short camera dropout, Frigate's
  recordings can lose their audio until Frigate restarts. The hourly check restarts it
  then, once no visit is in progress, and sends a quiet notification when that brings the
  audio back. If it doesn't, the camera's own stream is likely broken, and you get an
  alert. It restarts Frigate at most every 6 h, and alerts at most every 6 h.
- **Publishing** (`publish-report.sh`): with `RIG_REPORTS_REPO` in `.env` (the SSH URL of a
  **private** repository), each report is pushed there with a deploy key
  (`~/.ssh/gatekeeper-reports`, or `RIG_REPORTS_KEY`). Reports hold visit times and what
  visitors said, so never send them to a public repository. The project's reviewer reads
  them there, files issues and opens pull requests.
- **Auto-deploy** (`deploy.sh`) follows `main` (or `DEPLOY_BRANCH`):
  - It builds while the old containers keep running.
  - It restarts once no visit is in progress, and only what changed.
  - It then checks that Gatekeeper turns healthy and Frigate gets camera frames. If not, it
    rolls back and alerts.
  - It only moves forward along the branch. While the checkout is on another branch (to
    try something) or has local changes, it waits, and says so once.
  - `touch ~/.local/state/gatekeeper/deploy-paused` pauses it.

## Development

The app is the `gatekeeper` Python package in `src/gatekeeper/`. The Home Assistant add-on
([Gatekeeper-HA](https://github.com/Gatekeeper-HA/Gatekeeper-HA)) installs the same package
from a tagged release of this repo.

**Perception.** What watches the door is behind `gatekeeper.perception`: person events,
zones, the camera's latest frame, and snapshots. Today Frigate provides it
(`frigate.FrigatePerception`, the only module that knows Frigate). Our own hub will have
its own perception behind the same interface, and Frigate stays supported for installs
that run it. New features use `perception`, not Frigate.

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
