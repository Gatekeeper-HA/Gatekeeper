# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- **Quick replies** (Phase 1): the doorbell notification has buttons, *I'm coming*,
  *Not now* and *No thanks*, that make Gatekeeper say that line at the door. Before, you
  couldn't answer from your phone at all. It works within 5 minutes of the notification,
  once per button and visit, and right after the visit if Gatekeeper is still talking.
  - **With ntfy,** the buttons post to a reply topic (`doorbell-replies`) that Gatekeeper
    follows. The compose stack lets anyone post there and only Gatekeeper read it. Each
    button is signed with a key made at startup, so only Gatekeeper's own notifications'
    buttons work.
  - **Home Assistant** gets a *Say: …* button per reply (MQTT `gatekeeper/<camera>/quick_reply`).
  - **New settings:** `QUICK_REPLIES`, `QUICK_REPLY_WINDOW_SECONDS`, `QUICK_REPLY_COMING`,
    `QUICK_REPLY_LATER`, `QUICK_REPLY_NO_THANKS`, `NTFY_PUBLIC_URL`, `NTFY_REPLY_TOPIC`.
  - **Visit log and report:** each reply is logged as a `quick_reply` record. The daily
    report counts them by reply and result, with how long after the notification they came
    and how many found nobody there. Its `quick_replies` check fails if one couldn't be said.

- **Package alerts** (Phase 1, `PACKAGE_ZONE`): **Package at the front door** when a
  package is left, with a photo of it, and **Package picked up** when it's gone, with a
  photo of who came. No subscription or extra hardware.
  - **How it works:** a package only arrives or leaves with someone at the door, so the
    zone is compared before and after each visit. A small open image model (CLIP ViT-B/32,
    on the CPU) judges whether a changed patch is a package. When the lighting changed
    during a visit, the comparison is tried again for up to 10 minutes.
  - **On the rig:** 189 visits in a week with no package raised no alert. The compose stack
    checks the `porch` zone.
  - **Also:** Home Assistant gets a *Package* binary sensor. Records are
    `package_delivered` and `package_picked_up`. The daily report counts the packages and
    how the day's checks went, for tuning.

### Fixed
- **The daily report's `audio_tap` check fails when the tap was down.** It only caught a tap
  lost in the middle of a turn. When the tap was already down, each turn fell back to a
  fixed recording window and the check passed, even on a day with the tap down for every
  visit. The report now counts those turns (`replies.tap_down_turns`) and fails on them.
- **No traceback when Docker's health probe gives up early.** While the models load at
  startup, a probe can time out before `/healthz` answers. Writing the answer then raised
  `BrokenPipeError`, which the daily report counted as an error in Gatekeeper's log (the
  first deploy of quick replies). Other health endpoint errors are now logged normally.
- **The daily report flags visits where the doorbell speaker didn't connect.** A new
  `talkback` check fails on `talkback_failed` and `talkback_unavailable` visits: nobody at
  the door heard anything, yet every check passed. Unclassified visits that weren't button
  presses are counted as `unclassified` rather than `pressed`.
- **The audio tap recovers after a stall.** Once the camera's audio stopped for more than
  10 s, the watchdog judged every new ffmpeg reader by the audio from before the stall and
  killed it within a second, before it could connect. The tap stayed down, and visits
  recorded fixed windows, until Gatekeeper restarted. A new reader now gets 10 s from its
  start (#41).

## [0.3.0] - 2026-10-14

Phase 1's first release: the conversation engine, conversations for officers, emergencies
and political visitors, no greeting for the household on its way out, and NVIDIA GPU
support. Each ran on the rig's porch before the release, with real visitors and the
owner's test visits.

### Added
- **The household coming out of the front door isn't greeted** (`DOOR_ZONE`, Phase 1).
  Someone who appears in Frigate's door zone and walks out of it within
  `LEAVING_WATCH_SECONDS` (3) is logged as `left`: no greeting, no notification. On the
  rig, 71 of 202 porch detections in 9 days were people coming out, and 56 of them were
  greeted. Visitors appear on the walkway, someone who stays at the door is greeted, and a
  doorbell press always greets. Person events in the next `LEAVING_COOLDOWN_SECONDS` (15)
  count as the same person. The compose stack adds a `door` zone, and the daily report
  counts the people who left this way.
  - **Nor are they greeted when they come back in soon after** (`RETURN_WINDOW_SECONDS`,
    120): someone walking up within 2 minutes of someone leaving is logged as
    `returned`. On the rig's first night with the door zone, 4 of the 6 people who left
    were back at the door 27–80 s later, and each was greeted with no answer.
- **Conversations for officers, emergencies and political visitors** (Phase 1). These are
  new classifications, checked before the others; each gets a short conversation instead of
  one reply. Every line is a `PHRASE_*` setting.
  - **Emergency** (welfare check, 911, fire, medical…): an urgent notification at once, and
    "I have alerted the resident. Please wait." Never sent away.
  - **Law enforcement:**
    - an urgent notification at once;
    - asks the reason and whether they have a warrant, then their agency, name and badge.
      If their first sentence already said why they came or whether they have a warrant,
      that isn't asked again; only the question's disclosure ("This is an automated
      assistant, and this conversation is recorded.") is said, as `PHRASE_POLICE_DISCLOSURE`;
    - with a warrant, asks them to hold it close to the camera and keep it still, and
      whether a judge signed it. Six full-resolution photos are taken from go2rtc half a
      second apart; the sharpest (over the middle of the frame) goes with the notification,
      and all are kept. If they answer before the photos are all taken, it asks them to
      keep holding it and says when it's finished (`PHRASE_POLICE_HOLD_WARRANT`,
      `PHRASE_POLICE_WARRANT_DONE`);
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
  - The daily report counts these conversations by kind.
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
    the answer to the reply (`reply_latency`; in a conversation, to its first phrase). The
    daily report shows both, and checks the replies' speed (p90 under 3.5 s on a GPU, 7 s on
    a CPU) and that the audio tap held.
  - `scripts/replay/endpointing.py` replays recorded visits through the endpointing, for
    tuning.
- **NVIDIA GPU support (opt-in, `docker-compose.gpu.yml`):**
  - **Frigate** detects with YOLOv9 on the GPU (the `-tensorrt` image, `config.gpu.yml`), at
    about 20 ms per inference on the rig's GTX 1660 Ti against 83 ms for its default CPU
    model. A test keeps `config.gpu.yml` the same as `config.yml` apart from the GPU parts.
    `scripts/gpu/export-yolov9.sh` exports the model. Video decoding stays on the CPU: GPU
    decoding needs NVDEC, which headless server drivers leave out (README).
  - **Gatekeeper's image** can be built with the CUDA libraries Whisper needs
    (`VARIANT=cuda`, `requirements-cuda.lock`), and the override runs a larger Whisper model
    on the GPU.
  - **`WHISPER_DEVICE`** (`auto`: the GPU when there is one). **`WHISPER_COMPUTE_TYPE`** now
    defaults to `auto` (`int8` on the CPU, `int8_float16` on a GPU). If the GPU fails,
    Whisper falls back to the CPU.
  - **Measured on the rig's GTX 1660 Ti with real visits:** `large-v3-turbo` (the override's
    model) transcribes an answer in 1.1 s, against 4.0 s for `base.en` on the CPU, and hears
    names and full sentences better. `int8_float16` is 2–4× faster than `float16` on that
    card.
- **Rig tooling (`scripts/rig/`, README "Running a rig"),** for a live install that takes
  every merge:
  - **The daily report is part of the package** (`python -m gatekeeper.report`), so it
    always matches the deployed code, and it's tested. Besides the visits, notifications,
    hangs and recording audio, it now checks Frigate's frames and detector speed, Whisper's
    device and speed, Gatekeeper's and Frigate's log errors (grouped by kind, known noise
    left out), and the day's deploys. It writes JSON and Markdown as well as the ntfy
    summary.
  - **`publish-report.sh`** pushes each report to a private repository with a deploy key,
    for review.
  - **`deploy.sh`** (cron, every minute) deploys each new commit on `main`, then checks that
    Gatekeeper is healthy and Frigate gets frames, and rolls back and alerts if not.
- **Whisper logs how long each transcription took**, split into reading the audio and its
  features, and the model, with the host's CPU load; the daily report shows the medians
  (#16: a live visit takes ~1.8 s against 1.07 s for the same clip on its own).
- **The daily report says when a container's log starts mid-day.** A deploy that recreates
  a container loses its earlier log, so the report names the logs that only start then
  (#14).

### Changed
- **A turn where nobody spoke isn't transcribed**, so "Please state your purpose" comes
  ~1.5 s sooner. Whisper's text for silence always failed the speech check anyway. An
  answer said over the prompt, before listening starts, is still transcribed. The daily
  report counts the turns skipped.
- **Whisper no longer re-decodes at higher temperatures** when its output looks like
  garbage. On noise that took up to 7 s and only produced different made-up text.
- **`scripts/soak/` is now `scripts/rig/`.** Its alerts go straight to ntfy, so they also
  work while Gatekeeper is down. Update the cron lines (README, "Running a rig").

### Fixed
- **An answer that repeats the question isn't cut as its echo any more.** "Do you have a
  warrant?" "I do not have a warrant." was dropped as the prompt's own echo (an
  officer test, with a lawnmower next door). With the audio tap, words after the
  echo's end are always the visitor's.
- **Answers Whisper made up from noise are ignored** ("Thanks for watching!", "Please click
  the link in the description below"). An answer only counts if Silero VAD hears at least
  0.25 s of speech where Whisper put its words; otherwise the visit is treated as no answer.
  The daily report counts the answers ignored.

## [0.2.1] - 2026-10-13

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

## [0.2.0] - 2026-10-12

Phase 0 is done. This is 0.2.0-rc.1 unchanged, after a week on a live porch
(2026-10-05 to 2026-10-12). Every exit criterion was met:
- all 194 porch arrivals were greeted and all 136 visits were notified (7 daily reports),
  the slowest in 0.42 s;
- 0 hangs, timeouts, watchdog exits or restarts in 8 days, and Frigate's recordings kept
  their audio throughout;
- false triggers: at most 1 a day (a bush mistaken for a person at dawn, on 3 of 7 days);
- the Home Assistant add-on works on a clean Home Assistant OS (2026-10-04);
- 83% test coverage.

Seen during the soak and fixed for the next release: Whisper sometimes turns noise into
words ("Thanks for watching!"), and campaign, petition and voting words counted as a
solicitor.

### Added
- **Contributing guide and CLA:** `CONTRIBUTING.md`, and a Contributor License Agreement
  (`CLA.md`, with Lobo Dorado LLC) that covers every repository in the Gatekeeper-HA
  organization. A GitHub workflow asks first-time contributors to agree on their pull
  request and records the agreement on the `cla-signatures` branch.

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
