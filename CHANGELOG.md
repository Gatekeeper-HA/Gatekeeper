# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
