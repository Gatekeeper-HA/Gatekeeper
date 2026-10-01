# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Fixed
- The visitor's answer was usually missed. Recording started about 3 s after the greeting
  ended (playback buffer + a fresh RTSP connection), after most visitors had finished
  answering; 96% of logged v0.1 visits had an empty transcript. Recording now starts
  with the greeting, and the greeting's echo is cut out by matching its words in the
  transcript (Whisper's VAD filter is off for this, as it drops the echo).
- The Whisper model loads at startup instead of on the first visit.
- Classification matches whole words and phrases: "ups" no longer matches "groups" or
  "cups", nor "tech" "technically". Solicitors are checked before deliveries.
- British voices (`bm_george`, `bf_emma`) use Kokoro's British English pipeline.
- A stalled camera stream no longer hangs the doorbell. ffmpeg gets an RTSP socket timeout
  (`-timeout`; `-rw_timeout` has no effect on RTSP) and a hard process timeout, and each
  visit is bounded by `VISIT_TIMEOUT_SECONDS`.
- Visitors are no longer greeted repeatedly. Only one visit runs per camera, and person
  events during it or within `COOLDOWN_SECONDS` (90) after it are merged into it.
- No visit goes unlogged: a busy talkback, a failed greeting or reply, a timeout or an
  error are all written to `events.jsonl` with an `outcome`.

- Visitor audio and visit logs are no longer kept forever. Recordings are deleted after
  `AUDIO_RETENTION_DAYS` (7); `events.jsonl` rotates daily to `events-YYYY-MM-DD.jsonl`,
  deleted after `EVENT_LOG_RETENTION_DAYS` (30). `0` keeps them.

### Changed
- **Breaking for `events.jsonl` readers:** classifications are now `likely_delivery`,
  `solicitor`, `service_visit`, `cooperative_other` and `no_response` (replacing
  `unknown_cooperative` and `unknown_uncooperative`).
- Spanish keywords were removed; transcription is English-only until multilingual support.
- `events.jsonl` records gain an `outcome` field: `completed`, `reply_failed`,
  `talkback_busy`, `talkback_failed`, `talkback_unavailable`, `timeout` or `error`.
  `classification` and `response` are `null` when the visitor was never greeted.
- The app is now the `gatekeeper` Python package (`src/gatekeeper/`, run with
  `python -m gatekeeper`), shared with the Home Assistant add-on. Behavior is unchanged.
- Settings are read by a typed config module; empty variables fall back to defaults.
- Logging uses the `logging` module, with the visit's Frigate `event_id` on every line
  and an optional JSON format (`LOG_FORMAT=json`).
- Dependencies are pinned in `requirements.lock`, and images are pinned to exact versions
  (Frigate 0.16.4, Mosquitto 2.0.22, go2rtc 1.9.14, Python 3.11.16).
- `docker-compose.yml` builds from the repo and reads configs from it; state lives under
  `DATA_DIR` (default `./data`), so `git clone && cp .env.example .env && docker compose up`
  works.
- Licence in the README corrected to AGPL-3.0.

### Added
- `SESSION_TTL_SECONDS`, `SWEEP_INTERVAL_SECONDS`, `REPLY_*`, `LOG_LEVEL` and `LOG_FORMAT`
  settings.
- Unit tests and a GitHub Actions workflow (ruff + pytest).

### Removed
- Unused `/gatekeeper_audio` and `/models` mounts, and the unused `alsa-utils` package.

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
