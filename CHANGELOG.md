# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- **Conversations for officers, emergencies and political visitors** (Phase 1). These are
  new classifications, checked before the others; each gets a short conversation instead of
  one reply. Every line is a `PHRASE_*` setting.
  - **Emergency** (welfare check, 911, fire, medical…): an urgent notification at once, and
    "I have alerted the resident. Please wait." Never sent away.
  - **Law enforcement:**
    - an urgent notification at once;
    - asks the reason and whether they have a warrant, then their agency, name and badge;
    - with a warrant, asks them to hold it up to the camera (3 full-resolution photos from
      go2rtc, one attached) and whether a judge signed it;
    - a judge-signed warrant gets "notifying the resident"; otherwise, the resident doesn't
      consent, please leave, with a firmer second request if Frigate still sees them after
      `LEAVE_CHECK_SECONDS`.
  - **Civic** (candidates, campaigns, canvassers): asks who they are and what message to pass
    on, thanks them, and notifies you with both. Campaign, petition and voting words used to
    count as a solicitor ("No solicitation. Please leave the property.").
  - The visit log gains `flow`, `details` (what was learned) and `dialogue` (every question
    and answer).
  - `WHISPER_HOTWORDS` (names Whisper should expect).
  - Pre-synthesized phrases are reused across restarts unless their text or voice changes.
  - `VISIT_TIMEOUT_SECONDS` now defaults to 180, for the longer conversations.
- **The conversation engine** (Phase 1): replies come sooner after the visitor stops talking.
  - **Audio tap:** Gatekeeper reads the camera's audio all the time (one ffmpeg, a 30 s ring
    buffer), so listening needs no stream to be opened.
  - **Listening until the visitor is done:** it stops when they've been silent for 0.8 s, not
    after a fixed 4 s. If they start talking again while their answer is being transcribed,
    it listens on and transcribes all of it. Speech is detected with Silero VAD, the model
    faster-whisper already ships.
  - **One talkback connection per visit**, instead of one per phrase.
  - **New settings:** `AUDIO_TAP`, `VAD_THRESHOLD`, `END_SILENCE_MS`, `MAX_ANSWER_SECONDS`,
    `TALKBACK_SESSION`. Without a working tap, visits record a fixed window as before.
  - **The visit log** records how listening ended (`endpoint`) and the time from the end of
    the answer to the reply (`reply_latency`).
  - `scripts/replay/endpointing.py` replays recorded visits through the endpointing, for
    tuning.
- **Contributing guide and CLA:** `CONTRIBUTING.md`, and a Contributor License Agreement
  (`CLA.md`, with Lobo Dorado LLC) that covers every repository in the Gatekeeper-HA
  organization. A GitHub workflow asks first-time contributors to agree on their pull
  request and records the agreement on the `cla-signatures` branch.

### Fixed
Found by installing the Home Assistant add-on on a slow virtual machine:
- **Greetings and replies are no longer cut off on a slow or busy CPU.** The talkback
  session closed a fixed time after the clip started. When the CPU couldn't send the audio in
  real time, it closed mid-sentence. It now stays open until the clip's last frame has been
  sent (up to 15 s late), then holds as before. A late clip logs a warning that the CPU is
  too slow or too busy.
- **A recording that runs over its time limit is kept, not thrown away.** ffmpeg is now asked
  to stop, which makes it finish the WAV file, instead of being killed. What it recorded is
  transcribed as usual.
- **No more downloading at every start.** Kokoro's English pronunciation needs spaCy's
  `en_core_web_sm` model, which was downloaded (12.8 MB) each time the container started. It
  is now part of the image, pinned in `requirements.lock`.

## [0.2.0-rc.1] - 2026-10-04

Phase 0, "make v0.1 real": Gatekeeper now actually hears visitors, tells you about every
visit, survives camera and network failures, and runs from one tested package shared with
the Home Assistant add-on. This release candidate is being soaked on a live porch for a
week before 0.2.0.

### Breaking
- **`.env`** needs three MQTT passwords and the ntfy credentials (see the README's Quick
  start). The broker no longer accepts anonymous clients.
- **`events.jsonl` records** have new classification names (`likely_delivery`,
  `solicitor`, `service_visit`, `cooperative_other`, `no_response`, replacing
  `unknown_cooperative`/`unknown_uncooperative`) and new fields: `outcome`, `trigger` and
  `turns`. `classification` and `response` are `null` when the visitor was never greeted.
- **Ports:** Frigate's port 5000 and go2rtc's ports are no longer published on the LAN (see
  Security).
- **Run command:** the app runs as `python -m gatekeeper`; `app/main.py` is gone.

### Added
- **Notifications** via ntfy, sent right after the visitor's answer is classified, with what
  they said, the reply and Frigate's snapshot. Visits Gatekeeper couldn't talk to are
  notified too, and so are doorbell presses during a visit. The compose stack runs a
  private ntfy server; `scripts/ntfy-auth.sh` generates its credentials.
- **Doorbell button** (Reolink, via reolink-aio push events over HTTPS):
  - A press greets at once, even before Frigate has seen anyone.
  - A press during a visit or its cooldown notifies and says `REPLY_PRESSED`.
- **Home Assistant over MQTT:**
  - Gatekeeper publishes its status (with a last will), conversation state and each visit.
  - MQTT discovery creates a *Gatekeeper* device with *Last visitor* and *Conversation*
    entities.
- **Zones:** `TRIGGER_ZONES` only greets people who enter the given Frigate zones; the
  compose stack ships a `porch` zone and a street mask.
- **A second chance:** if the visitor says nothing, the no-answer reply asks again and
  Gatekeeper listens once more.
- **Health:** `GET /healthz` (port 8099), a Docker `HEALTHCHECK`, and a watchdog that exits
  on an internal hang lasting `HANG_EXIT_SECONDS` so Docker restarts Gatekeeper.
- **Retention:** visitor recordings are deleted after `AUDIO_RETENTION_DAYS` (7). Visit logs
  rotate daily and are deleted after `EVENT_LOG_RETENTION_DAYS` (30).
- **Settings:**
  - Every reply text (`REPLY_*`).
  - `SESSION_TTL_SECONDS`, `SWEEP_INTERVAL_SECONDS`, `COOLDOWN_SECONDS` and
    `VISIT_TIMEOUT_SECONDS`.
  - MQTT and ntfy credentials, `FRIGATE_API`, `REOLINK_*` and `HA_DISCOVERY_PREFIX`.
  - `LOG_LEVEL`, and `LOG_FORMAT=json`, with the visit's Frigate event id on every line.
- **Optional Home Assistant container** (compose profile `ha`) for testing.
- **Development:** unit tests (166) and CI (ruff, pytest, compose validation).
- **Soak tooling:** `scripts/soak/` holds a daily report and an hourly recording-audio
  check, sent via ntfy.

### Changed
- **One package:** the app is the `gatekeeper` Python package (`src/gatekeeper/`), installed
  by both the compose image and the Home Assistant add-on.
- **Pinned versions:** dependencies are pinned in `requirements.lock`. Images are pinned to
  Frigate 0.16.4, Mosquitto 2.0.22, go2rtc 1.9.14 and Python 3.11.16.
- **Compose:** `docker-compose.yml` builds from the repo, reads configs from it, and keeps
  state under `DATA_DIR`. `git clone && cp .env.example .env && docker compose up` works.
- **Whisper:** the compose stack uses `base.en` (more accurate than `tiny`, about 1.6 s
  slower per visit). Whisper loads at startup.
- **One visit at a time:** only one visit runs per camera. Person detections during it, or
  within `COOLDOWN_SECONDS` (90) after it, count as the same visitor.
- **English only:** the Spanish keywords were removed until multilingual support exists.

### Fixed
- **The visitor's answer was usually missed.** 96% of v0.1's logged visits had an empty
  transcript, because recording started ~3 s after the greeting ended.
  - Recording now starts with the greeting.
  - The greeting's echo is cut out by matching its words, including when its start or end
    is cut off or misheard.
- **The end of the greeting was cut off after a button press**, because the doorbell's chime
  delays playback. Talkback now stays open 5 s after each clip.
- **A stalled camera stream hung the doorbell until restart.** ffmpeg now gets an RTSP socket
  timeout (`-rw_timeout` doesn't work for RTSP) and a process timeout, and every visit is
  bounded.
- **Visits were lost without a trace.** A busy talkback, failed greeting, failed reply,
  timeout or error is now logged and notified.
- **Classification:** it matches whole words, so "ups" no longer matches "groups", and
  solicitors are checked before deliveries.
- **British voices** use Kokoro's British English pipeline.
- **Licence:** the README now says AGPL-3.0.

### Security
- **Logins:** MQTT requires them, one account per service, limited by an ACL.
- **Published ports:** only services with a login are published (Frigate 8971, MQTT 1883,
  ntfy 8090, optional Home Assistant 8123). Frigate's port 5000 and go2rtc's API, RTSP and
  WebRTC ports had no authentication; anyone on the LAN could watch the doorbell or talk
  through its speaker.
- **No STUN:** go2rtc no longer asks public STUN servers for the home's IP.
- **Secrets:** go2rtc only gets the camera variables, not the whole `.env`.

### Removed
- **Unused bits:** the `/gatekeeper_audio` and `/models` mounts, and the `alsa-utils`
  package.

## [0.1.0] - 2026-05-13

### Added
- Initial release
- Frigate MQTT event listener for person detection
- Kokoro TTS for natural-sounding voice responses
- faster-whisper (tiny model, int8) for local speech-to-text transcription
- WebRTC talkback to camera speaker via go2rtc
- Pre-synthesis of all fixed responses at startup for instant playback
- Keyword-based visitor classification (delivery, sales, maintenance, generic)
- Configurable greeting via `GREETING` environment variable
- Session management with dwell timer to prevent duplicate interactions
- Talkback lock to prevent concurrent go2rtc session conflicts
- Interaction log written to `events.jsonl`
- Support for multiple Kokoro voices (af_heart, af_bella, af_sarah, am_michael, am_adam, bm_george, bf_emma)
- Environment variable substitution for credentials in go2rtc.yaml
- `.env` file support for server IP and camera credentials
