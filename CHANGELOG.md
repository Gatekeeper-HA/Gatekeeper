# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Changed
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
