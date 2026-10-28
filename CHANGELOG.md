# Changelog

All notable changes to this project will be documented in this file.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added
- **Knocks, in shadow** (`KNOCK_LISTENER=shadow`, off by default). Gatekeeper listens for
  knocks at the door: a cheap check for sharp sounds on the audio tap, then a sound
  classifier (AST, BSD-3-Clause, ONNX on the GPU) on those 2 seconds. Each knock is
  logged with whether anyone was on camera and whether a visit was running, and shows on
  the Visits page and in the daily report. On the rig's test, 4 of 4 sets of knocks were
  heard and nothing else scored as one. Nothing at the door depends on it yet: later, a
  knock with nobody on camera will be like a doorbell press.
- **Delivery guess, in shadow** (`DELIVERY_GUESS`, off by default). For each greeted visit,
  Gatekeeper guesses whether it was a delivery: SigLIP (Apache-2.0) on full-resolution
  crops of the person, how they moved (left the way they came, walked away when greeted),
  and a package left. The Visits page shows the guess next to the owner's label, and the
  daily report says how often they agree. Only scores are kept, never the crops.
- **Visits page** (`http://<SERVER_IP>:8098/`, with `GATEKEEPER_WEB_PASSWORD`). The last few
  days at the door, newest first: what Gatekeeper did in words, what it heard, and the
  camera's picture. The owner says what each visit really was (Amazon, UPS, FedEx, USPS,
  a food delivery, groceries, the household, a guest...). The daily report lists the
  labels added each day next to what Gatekeeper made of those visits. The faces page
  moves to `faces/`, linked from it.
  One Save button for the whole page, in a bar that stays at the bottom: change any
  visits' labels, then save them together.

- **Daily report: what was done on the household faces page.** A *Faces page* line (and
  `counts.recognized.faces_page`) lists the faces enrolled per name, kept faces deleted,
  names forgotten, purges and captures at the door, from Gatekeeper's log.
- **Own perception, in shadow** (`OWN_PERCEPTION=shadow`, off by default). This is step 2
  of replacing Frigate on our hub: Gatekeeper finds and tracks people itself, next to
  Frigate, and only logs them.
  - **How it works:** it reads the camera's low-resolution stream and keeps only the newest
    frame. A motion check gates the person detector, YOLOX-tiny (Apache-2.0), which was
    picked by testing on the rig's footage. Detections become tracks that walk through
    Gatekeeper's zones and give the same person events as Frigate's.
  - **Daily report:** each track is logged when it ends, and the report compares the tracks
    with Frigate's porch arrivals: seen in the zone, seen elsewhere, missed, and porch tracks
    Frigate didn't have. It also reports frames per second and the detector's time. Its
    `own_perception` check fails if it's on but logged nothing.
  - **New settings:** `OWN_PERCEPTION`, `OWN_PERCEPTION_URL`, `OWN_PERCEPTION_FPS`,
    `OWN_PERCEPTION_THRESHOLD`, `OWN_PERCEPTION_THREADS`, `OWN_PERCEPTION_DEVICE`,
    `MODEL_DIR`.
  - **On the GPU** (`docker-compose.gpu.yml`): the GPU image has onnxruntime with CUDA
    (`onnxruntime-gpu` 1.26, the last built for CUDA 12, which Whisper uses), so the
    detector runs at 5 fps in about 19 ms a frame on a GTX 1660 Ti. On the rig's CPU it took
    265 ms, and 1.5 cores on a windy day, when swaying leaves pass the motion check.
  - **Tracking:** a single box not seen again within 1 s is dropped.

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

- **The household going inside isn't talked to any more** (`DOOR_ZONE`). Most people
  Gatekeeper greets are the household coming home, who walk straight in. When someone it
  greeted goes in by the front door without saying anything, and nobody else is on the
  porch, Gatekeeper stops talking (no second "please state your purpose") and sends a
  quiet notification, *Someone went inside by the front door*, instead of *Someone at the
  front door*. A doorbell press or an answer always gets the full visit.
  - **On the rig:** in a week, 43 of the 60 unanswered visits Frigate tracked ended in the
    door zone, a median 9.7 s after the person appeared and before the notification went out.
  - **Visit log and report:** the outcome is `entered`; Home Assistant's last visitor isn't
    updated. The daily report counts them apart, as the household: *Went inside without
    answering*.

- **Household faces** (`FACE_RECOGNITION`, off by default). Gatekeeper recognizes the
  people the owner enrolls. Someone recognized before their greeting isn't greeted, and
  their visit stops if it had started, with a quiet notification, *Alex at the front door*.
  A doorbell press always gets a full visit.
  - **Our own pipeline,** not Frigate's: YuNet and SFace (OpenCV Zoo, MIT and Apache-2.0),
    on the CPU, on faces read from the camera's high-resolution stream. A video tap keeps
    its last few seconds and decodes only while someone is on camera.
  - **Enrolling:** `python -m gatekeeper.face_admin`. Gatekeeper keeps the faces of people
    who went in by the door, groups them, and the owner names the groups, with that
    person's consent (`--consent`). `forget` and `purge` delete faces for good. Nobody
    else's face is stored.
  - **On the rig:** of a week's 70 greeted visits, replayed with a gallery from the other
    days, 16 were recognized, 12 of them before the greeting, and nobody was taken for
    someone else.
  - **Visit log and report:** the outcome is `household`, with `details.name` and
    `details.greeted`. The daily report counts recognitions by name, and how often a face
    was seen at all.

- **The household faces page** (`WEB_PORT` 8098, behind `WEB_PASSWORD`). Enrolling no
  longer needs a shell.
  - **What it shows:** the faces kept from people who went in by the front door, grouped
    by person. Untick any that aren't them, pick or type their name, confirm they agreed,
    and enroll. Faces that aren't the household can be deleted, and it lists who is
    enrolled, with *Forget* and *Delete every face*.
  - **How it's built:** plain HTML with no scripts, so it works on a phone. Relative links
    work behind Home Assistant's ingress. Each change needs a token from the page, and
    nothing is cached.
  - **Compose:** publishes 8098 on `SERVER_IP` and reads `GATEKEEPER_WEB_PASSWORD` from
    `.env`.
  - **Grouping:** a Frigate track's faces stay together, and tracks are grouped by their
    average face. On the rig's first faces, from 3 people, that's 5 groups instead of 12,
    with none mixed. It's also vectorized: hundreds of faces group in well under a second.
  - **Names:** a group that clearly looks like someone enrolled comes with their name
    filled in. Otherwise the closest names are shown.
  - **Sharper pictures:** each kept face also keeps a picture from the full frame, up to
    320 px, instead of only the 112 px aligned face.
  - **More faces for enrollment:** faces seen after the decision count, so people still
    walking up are included. People back soon after leaving (`returned`) are kept too.
    *Capture at the door* keeps whoever walks up in the next 2 minutes: the way to enroll
    someone the camera hasn't caught going inside.

### Changed
- **A send-off for someone already walking away.** When the greeting gets no answer and
  the visitor is walking back down the walk (on a doorbell camera, back up the frame from
  the closest they came), Gatekeeper says one of 7 friendly send-offs (*"Leaving so soon?
  Thanks for stopping by!"*) instead of asking again, and doesn't wait for an answer.
  Couriers who drop something off and go are the usual case: replayed on the rig's
  labelled visits, 3 of 5 silent deliveries would have got one. `GOODBYE_LINES` /
  `REPLY_GOODBYE` set them; the Visits page and the daily report show the send-offs.
- **A greeting with personality.** Instead of *"Hello. This property is monitored. Please
  state the purpose of your visit."*, Gatekeeper greets with one of 11 friendly, lightly
  funny lines, at random and never the same twice in a row, plus 2 that answer the bell
  after a press. Every one says the property is monitored, one way or another (a test
  checks it). `GREETING_LINES` / `GREETING_PRESSED_LINES` replace them, and `GREETING` sets
  a single greeting. The daily report shows how often greetings got an answer, and
  which ones. Before, about 1 greeted visitor in 10 answered on the rig.
- **A personality when the visitor doesn't answer.** Instead of one flat warning (*"You are
  being recorded. Please state your purpose or leave the property."*), Gatekeeper says one
  of 20 friendly, funny lines, at random and never the same twice in a row. Two more that
  mention the bell are only for after a doorbell press. Each still gets them to say why
  they came, or go. `NO_ANSWER_LINES` / `NO_ANSWER_PRESSED_LINES` replace them, and
  `REPLY_NO_ANSWER` sets a single line. The daily report shows how often each line got an
  answer.
- **Frigate is behind a perception interface** (`gatekeeper.perception`, inside the code,
  no change in behaviour). Person events, zones, the camera's latest frame and snapshots
  come from a `Perception`, which Frigate provides today (`frigate.FrigatePerception`).
  It's the first step to running on our own hub without Frigate (design doc 03).
- **The compose stack doesn't greet someone back within 4 minutes of leaving**
  (`RETURN_WINDOW_SECONDS` 240, was 120). In 8 days on the rig, all 41 people who walked
  up within 30 minutes of someone leaving by the door were the household. The 2-minute
  window caught 19 of them, and 5 more came back between 2 and 3 minutes and were greeted.
  The doorbell button still greets anyone.
- **The daily report shows what the rig's audio check did:** when it restarted Frigate,
  whether that brought the recordings' audio back, its alerts, and when a deploy held it
  off. Before, only the silent hours showed, so an automatic restart looked like a manual
  one. An alert now fails the `recording_audio` check.
- **A deploy no longer hides the day's log from the daily report.** Recreating a container
  takes its log with it, so a deploy shortly before the report left it with minutes of
  Gatekeeper's log: no face watches, package checks, Whisper times or errors from before.
  `deploy.sh` now keeps each container's last 24 h of log before it restarts anything
  (`~/.local/state/gatekeeper/logs/`, two days), and the report reads it back. It says
  *Logs kept across the deploy* instead of *Logs only since…*, which now only shows when a
  container was recreated some other way (by hand).

### Fixed
- **Visits page: doorbell presses.** A press during a visit had a label of its own and no
  picture, though it's part of that visit: it's now a note with no label. A visit that
  started with a press, before anyone was seen, shows the frame sent with its
  notification, now kept for the page (like other snapshots, `AUDIO_RETENTION_DAYS`).
- **A canvasser who already said who they are isn't asked again** (#78). When a civic
  visitor's first sentence gives their name and who they're with (*"My name is …, a
  volunteer with …"*), it's taken as who they are and Gatekeeper goes straight to their
  message, as the officer flow does with the reason (#23).
- **Daily report: a camera restart isn't a red Frigate check.** `frigate_log_errors`
  counted lines, so one camera restart (a doorbell's scheduled maintenance: ffmpeg
  crashing and the watchdog retrying ~5 times in a minute, ~56 lines) went over the
  50-line limit meant for about two dropouts. Frigate's errors are now grouped into
  bursts: one over within 3 minutes is a camera dropout, and the check fails on more than
  two dropouts or more than 50 lines outside them (an outage). The *frigate log* line
  says how many dropouts.
- **A courier making several trips isn't greeted and warned each time.** On the rig, a
  grocery delivery came in four trips. Gatekeeper greeted the courier twice, and twice
  told them to state their purpose or leave the property, the second time just after it
  had sent *Package at the front door* for the bags. Once a delivery is seen (a
  package left, or a visitor who says they're delivering), people walking up within
  `DELIVERY_TRIP_SECONDS` (180) of it or of the last trip are logged as `delivery_trip`
  and not greeted. The pickup ends it, the door rules come first, and a doorbell press
  still greets. The daily report counts the trips apart.
- **Daily report: own perception compared from when it was really running.** A deploy
  later in the day restarted it, and the report compared only the arrivals after that
  restart (on the rig, *0 of 0* instead of *21 of 21*). It now starts from its first
  track or hour of stats in the log.
- **Daily report: face matches that didn't make a recognition.** *Recognized by face*
  counted `household` records only, so a face matched over the threshold once (one short
  of `FACE_MIN_MATCHES`), or someone recognized after their greeting (no record), was
  invisible: *0 recognized* read like recognition not working. `counts.recognized` now has
  `matched` (watches whose faces matched each name) and `no_record` (recognitions with no
  `household` record), both on the line.
- **Daily report: the doorbell's weekly restart isn't a log error.** reolink-aio's *lost
  event subscription* line, and the button listener's own reconnects (now a warning), made
  every Sunday report red. Whether the button is heard again is the `doorbell_button`
  check and the *Doorbell* line.
- **Daily report: own perception's detector numbers per device.** On the day the detector
  moved to the GPU, the line read *median 251 ms on cuda*: the median of every hour, most
  on the CPU, labelled with the last hour's device. It now gives each device's fps and
  median, in order (on the rig: *on cpu 3.0 fps, median 254 ms (20 h); on cuda 5.0 fps,
  median 18 ms (3 h)*), so a GPU falling back to the CPU shows too. Its *since* stays
  within the report's window.
- **The doorbell button after the doorbell restarts.** Reolink doorbells restart weekly
  (scheduled maintenance) and after firmware updates, and their push
  events stop. reolink-aio resubscribes by itself, but logs it only at debug level, so
  nothing showed whether presses were heard again. Now, once push events have come in,
  two minutes without them make Gatekeeper reconnect, and say so.
  - **Daily report:** a *Doorbell* line with the firmware (marked when it changes, i.e.
    an update), each restart and how long until the button was heard again. The
    `doorbell_button` check fails if it isn't heard when the report runs.
- **Daily report: the time to the next arrival after someone left by the door** (the data
  for `RETURN_WINDOW_SECONDS`) skips someone stepping out of the door and back in
  (`stepped_out`), which isn't an arrival and was listed as greeted. Someone recognized by
  face before their greeting is listed as not greeted.
- **The household stepping out of the front door isn't greeted any more.** Someone who
  came out and stayed at the door more than 3 s was greeted, and warned when they didn't
  answer. On the rig that happened 3 times in 8 days (46 greeted visits), once after
  midnight: someone locking up, letting the dog out, or looking around.
  - **Now** someone who appears in the door zone with nobody else on camera then, or in
    the 30 s before (`CAME_OUT_QUIET_SECONDS`), isn't greeted however long they stay.
    They're logged `left` when they walk away, or `stepped_out` (new) when they go back in.
    A doorbell press still greets.
  - **When someone else was just on camera,** it could be a visitor's track picking up
    again at the door, so it's judged as before.
  - **Replayed** on the 3 visits from the rig's real paths: none would be greeted.
  - **Also:** someone who appears at the door and goes straight back in is logged
    `stepped_out`, instead of counting in the daily report as a porch arrival not greeted.
- **Package alerts when the light changes during a visit.** The doorstep is compared before
  and after each visit, which needs the same light. When someone switched a light on (or
  the camera's night mode or a doorbell's white LED changed), the check retried for 10
  minutes and then gave up. The frame after the visit, package and all, then became the
  "empty" doorstep, so the package was never reported.
  - **Now** it compares with the last frame from before the visit in the same light. One
    is kept per kind of light (night mode or colour, by brightness), learned by itself for
    each camera and kept for two days.
  - **A light it has never seen:** the visit waits up to 12 hours for the light from before
    to come back.
  - **Found by** the owner's test box, left just as the porch light came on.
  - **Also:** Frigate's frame keeps its shape (854x480 for a 16:9 camera) instead of being
    squeezed to 640x480.
- **Rig: recordings that lose their audio are fixed without you.** After a short camera
  dropout, Frigate's recordings could stay silent until Frigate restarted. Once, that
  was 19 hours of alerts that nobody acted on. `scripts/rig/audio-check.sh` now restarts
  Frigate itself (at most every 6 h, after any visit in progress, never during a deploy),
  sends a quiet notification when the audio is back, and alerts only when the restart
  didn't help.
- **Daily report: packages "not checked" counts visits, not retries** (#35). A visit whose
  lighting changed was counted once per retry (every minute for up to 10 minutes), and
  the visits it gave up on weren't counted at all. Retries are now counted apart, as
  "retried for the lighting".
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
- **Daily report: Frigate's "Non-monotonic DTS" lines are known noise** (#43). When the
  camera stream comes back after a dropout, ffmpeg corrects its timestamps in a burst of
  these lines (97 of a sub-minute dropout's 153 errors on the rig). They're now counted as
  ignored; the dropout still shows in the watchdog's lines.
- **Daily report: each porch arrival not greeted says why** (#45). A person in a trigger
  zone whose Frigate event ended (or expired) before a visit started was dropped without a
  log line, so the report could only list the time. Gatekeeper now logs `person event <id>
  ended before a visit: …` (under the dwell, still in the door's leaving watch, or not
  started), and the report lists each arrival not greeted with how long Frigate saw the
  person and what Gatekeeper logged about it (`arrivals.why`).
- **Daily report: an arrival while Gatekeeper wasn't listening says so** (#49). After a
  deploy, someone who walked up while Gatekeeper was still starting (before it connected to
  MQTT) was listed as "nothing logged", the same as an event Gatekeeper dropped silently.
  The report now says `not listening yet (Gatekeeper starting)`, `not listening (MQTT
  disconnected)` or `before Gatekeeper's log starts (container recreated)` instead.

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
