# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
