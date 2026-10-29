import datetime
import io
import json
import time

import pytest

from conftest import load_fixture
from gatekeeper import report
from gatekeeper.report import Inputs, build, render_markdown, render_text, summarize_logs

GATEKEEPER_LOG = load_fixture("report/gatekeeper.log").decode()
FRIGATE_LOG = load_fixture("report/frigate.log").decode()

LOGS = f"### gatekeeper\n{GATEKEEPER_LOG}### frigate\n{FRIGATE_LOG}"

NOW = 1_800_000_000.0
STATS = {
    "detectors": {"onnx": {"inference_speed": 21.5}},
    "cameras": {
        "front_door": {
            "camera_fps": 5.1,
            "process_fps": 5.1,
            "detection_fps": 0.9,
            "skipped_fps": 0.0,
        }
    },
}
HOST = {
    "health": "healthy",
    "started": "2026-10-14T00:30",
    "commit": "5acf45b0",
    "deploys": [],
    "hang_exits": 0,
    "restarts": 0,
    "silent_audio_hours": [],
}


def visit(start, end, event_id=None, **fields):
    return {
        "ts": end,
        "event_id": event_id or f"{start:.6f}-abc",
        "camera": "front_door",
        "trigger": "person",
        "outcome": "completed",
        **fields,
    }


def inputs(**changes) -> Inputs:
    v = visit(
        NOW - 3600,
        NOW - 3570,
        classification="likely_delivery",
        transcript="I have a package for you.",
        response="Thank you.",
    )
    values = dict(
        now=NOW,
        visits=[v],
        window_visits=[v],
        notifications=[{"ts": NOW - 3570, "event_id": v["event_id"], "ok": True, "seconds": 0.8}],
        tracked_since=0.0,
        arrivals=[{"start_time": NOW - 3601}],
        stats=STATS,
        log_text="### gatekeeper\n" + "\n".join(GATEKEEPER_LOG.splitlines()[3:5]) + "\n",
        host=HOST,
        version="0.2.1",
    )
    values.update(changes)
    return Inputs(**values)


def failing(r):
    return {c["name"]: c["problem"] for c in r["checks"] if not c["ok"]}


def test_logs_are_grouped_by_kind_without_the_known_noise():
    logs = summarize_logs(LOGS)

    gk, frigate = logs["sources"]["gatekeeper"], logs["sources"]["frigate"]
    assert (gk["errors"], gk["warnings"]) == (2, 1)
    assert (frigate["errors"], frigate["warnings"]) == (2, 1)
    assert logs["ignored"] == 3  # HF Hub, torch's UserWarning, Frigate's hwaccel advice

    # The same error with different addresses is one kind.
    assert len([k for k in frigate["top"] if k["level"] == "ERROR"]) == 1
    assert frigate["top"][0]["count"] == 2
    assert frigate["top"][0]["first"] == "2026-10-13 19:35:01"


def test_a_traceback_belongs_to_the_error_line_before_it():
    kinds = summarize_logs(LOGS)["sources"]["gatekeeper"]["top"]

    error = next(k for k in kinds if k["level"] == "ERROR")
    assert error["message"] == "transcription error"
    assert error["exception"] == "RuntimeError: CUDA failed with error out of memory"
    assert "audio.py" in error["where"]

    # The health server's traceback has no log line of its own.
    traceback = next(k for k in kinds if k["level"] == "TRACEBACK")
    assert traceback["exception"] == "BrokenPipeError: [Errno 32] Broken pipe"
    assert "health.py" in traceback["where"]


def test_whisper_model_device_and_transcription_times():
    info = report.whisper_info(GATEKEEPER_LOG.splitlines())

    assert (info["model"], info["device"], info["compute_type"]) == (
        "large-v3-turbo",
        "cuda",
        "int8_float16",
    )
    assert info["transcriptions"] == 1
    assert info["median_seconds"] == 1.82


def test_a_whisper_gpu_fallback_fails_its_check():
    lines = [
        "2026-10-13 19:33:31,255 INFO    gatekeeper.audio [-] loading Whisper model: "
        "large-v3-turbo on cuda (int8_float16)",
        "2026-10-13 19:33:40,000 ERROR   gatekeeper.audio [-] Whisper couldn't run on the "
        "GPU; using the CPU",
    ]
    r = build(inputs(log_text="### whisper\n" + "\n".join(lines) + "\n"))

    assert r["whisper"]["device"] == "cpu"
    assert failing(r)["whisper_device"] == "Whisper fell back to the CPU"


def test_a_good_day_is_all_clear():
    r = build(inputs())

    assert failing(r) == {}
    assert render_text(r).startswith("Gatekeeper daily report: all clear\n")
    assert "Whisper: large-v3-turbo on cuda, 1 answers, median 1.8 s" in render_text(r)
    assert "Frigate: onnx 22 ms, camera 5.1 fps" in render_text(r)
    assert r["visits"][0]["transcript"] == "I have a package for you."
    assert r["notifications"] == {
        "tracked": 1,
        "notified": 1,
        "unnotified": [],
        "slowest_seconds": 0.8,
        "failed_attempts": 0,
    }


def test_problems_fail_their_checks_and_make_the_title():
    late = visit(NOW - 600, NOW - 590)
    r = build(
        inputs(
            visits=[late],
            window_visits=[late],
            notifications=[{"ts": NOW - 500, "event_id": "other", "ok": True, "seconds": 3.5}],
            arrivals=[{"start_time": NOW - 7200}],
            stats={
                "detectors": {"cpu": {"inference_speed": 140.0}},
                "cameras": {"front_door": {"camera_fps": 0.0, "skipped_fps": 0.0}},
            },
            log_text=LOGS,
            host={
                **HOST,
                "silent_audio_hours": ["Mon 22:00"],
                "restarts": 1,
                "deploys": [{"to": "abcdef12", "result": "rolled-back"}],
            },
        )
    )

    assert failing(r) == {
        "arrivals_greeted": "1 porch arrival(s) not greeted",
        "visits_notified": "1 visit(s) not notified",
        "notify_speed": "slowest notification 3.5 s",
        "no_hangs": "1 hang/restart(s)",
        "recording_audio": "no recording audio in 1 hour(s)",
        "gatekeeper_log_errors": "2 error(s) in Gatekeeper's log",
        "camera_frames": "camera at 0.0 fps",
        "detector_speed": "cpu detector at 140 ms",
        "deploys": "1 failed deploy(s)",
    }
    assert render_text(r).startswith(
        "Gatekeeper daily report: 1 porch arrival(s) not greeted; 1 visit(s) not notified;"
    )
    assert "gatekeeper log: 2 error(s), e.g. BrokenPipeError" in render_text(r)


def test_logs_that_start_after_a_deploy_say_so():
    recreated = report.datetime.datetime.fromtimestamp(NOW - 600, report.datetime.UTC)
    host = {
        **HOST,
        "created": {
            "gatekeeper": recreated.strftime("%Y-%m-%dT%H:%M:%S.123456789Z"),
            "frigate": "2026-01-01T00:00:00Z",  # before the window: its log covers the day
        },
    }
    r = build(inputs(host=host))

    assert r["logs"]["since"] == {"gatekeeper": report.local(NOW - 600)}
    assert (
        f"Logs only since the container was recreated: gatekeeper from {report.local(NOW - 600)}"
    ) in render_text(r)
    assert "Logs only since" not in render_text(build(inputs()))
    assert build(inputs(host={**HOST, "created": {"frigate": ""}}))["logs"]["since"] == {}


def test_logs_kept_across_a_deploy_say_so_instead():
    recreated = report.datetime.datetime.fromtimestamp(NOW - 600, report.datetime.UTC)
    host = {
        **HOST,
        "created": {"gatekeeper": recreated.strftime("%Y-%m-%dT%H:%M:%S")},
        "kept_logs": ["gatekeeper"],
    }
    r = build(
        inputs(
            host=host,
            arrivals=[{"id": "1-early", "start_time": NOW - 7200, "end_time": NOW - 7190}],
        )
    )

    assert r["logs"]["since"] == {}
    assert r["logs"]["kept"] == {"gatekeeper": report.local(NOW - 600)}
    text = render_text(r)
    assert "Logs only since" not in text
    assert f"Logs kept across the deploy: gatekeeper (recreated {report.local(NOW - 600)})" in text
    # The log before the deploy is there, so a missed arrival then isn't blamed on it.
    assert r["arrivals"]["why"][0]["gatekeeper"] is None


def test_detection_that_drops_frames_fails():
    stats = {"detectors": {}, "cameras": {"front_door": {"camera_fps": 5.0, "skipped_fps": 2.0}}}
    assert failing(build(inputs(stats=stats)))["detection_keeps_up"] == (
        "detection skips 2.0 of 5.0 fps"
    )


def frigate_errors(times: list[float]) -> str:
    """Frigate error lines at these times (seconds after 03:00:00)."""
    lines = []
    for t in times:
        at = datetime.datetime(2026, 1, 4, 3) + datetime.timedelta(seconds=t)
        lines.append(
            f"2026-01-04 09:00:00.000000000  [{at:%Y-%m-%d %H:%M:%S}] frigate.video"
            "                  ERROR   : front_door: Unable to read frames from ffmpeg process.\n"
        )
    return "### frigate\n" + "".join(lines)


def test_a_camera_dropout_is_fine_but_frigate_failing_all_day_is_not():
    dropout = "### frigate\n" + FRIGATE_LOG
    assert "frigate_log_errors" not in failing(build(inputs(log_text=dropout)))

    # An outage: the record watchdog every 20 s, for longer than a dropout.
    outage = frigate_errors([20.0 * n for n in range(report.FRIGATE_ERRORS + 1)])
    assert failing(build(inputs(log_text=outage)))["frigate_log_errors"] == (
        f"{report.FRIGATE_ERRORS + 1} error(s) in Frigate's log"
    )


def test_a_camera_restart_is_one_dropout_however_many_lines():
    # A doorbell restarting for its scheduled maintenance: about 56 lines in 42 s.
    restart = frigate_errors([i * 42 / 55 for i in range(56)])
    r = build(inputs(log_text=restart))
    assert "frigate_log_errors" not in failing(r)
    assert r["logs"]["sources"]["frigate"]["dropouts"] == 1
    assert any(
        line.startswith("frigate log: 56 error(s) (1 camera dropout(s)), e.g.")
        for line in report.summary_lines(r)
    )


def test_lone_frigate_errors_arent_dropouts():
    # "MQTT disconnected" at three deploys, hours apart: lines, not camera dropouts.
    r = build(inputs(log_text=frigate_errors([0.0, 3600.0, 7200.0])))
    assert "frigate_log_errors" not in failing(r)
    assert not r["logs"]["sources"]["frigate"].get("dropouts")


def test_many_dropouts_fail_even_when_short():
    hour = 3600.0
    times = [h * hour + s for h in range(report.MAX_DROPOUTS + 1) for s in range(0, 20, 2)]
    r = build(inputs(log_text=frigate_errors(times)))
    assert failing(r)["frigate_log_errors"] == (
        f"{len(times)} error(s) in Frigate's log, {report.MAX_DROPOUTS + 1} camera dropouts"
    )


@pytest.mark.parametrize(("device", "ok"), [("cpu", True), ("cuda", False)])
def test_transcription_speed_is_held_to_the_device(device, ok):
    log = (
        f"### gatekeeper\n2026-10-13 19:33:31,255 INFO    gatekeeper.audio [-] loading "
        f"Whisper model: base.en on {device} (int8)\n"
        "2026-10-13 19:46:39,352 INFO    gatekeeper.audio [-] transcribed 12.0 s of audio "
        "in 5.00 s\n"
    )
    assert ("transcription_speed" not in failing(build(inputs(log_text=log)))) == ok


def test_no_frigate_api_is_a_failed_check():
    r = build(inputs(arrivals=None, stats=None))
    assert failing(r) == {"frigate_api": "Frigate's API didn't answer"}
    assert r["frigate"] is None


def test_markdown_lists_checks_visits_and_logs():
    v = visit(NOW - 3600, NOW - 3570, transcript="a | b")
    md = render_markdown(
        json.loads(json.dumps(build(inputs(visits=[v], window_visits=[v], log_text=LOGS))))
    )

    assert md.startswith("# Gatekeeper daily report: ")
    assert "| gatekeeper_log_errors | **2 error(s) in Gatekeeper's log** |" in md
    assert "a \\| b" in md
    assert "`RuntimeError: CUDA failed with error out of memory`" in md
    assert "Known harmless lines ignored: 3." in md


def test_render_reads_a_report_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(build(inputs()))))
    assert report.main(["--render", "text"]) == 0
    assert capsys.readouterr().out.startswith("Gatekeeper daily report: all clear\n")


def test_collect_reads_the_logs_and_asks_frigate(settings):
    now = time.time()
    with open(settings.event_log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(visit(now - 100, now - 60)) + "\n")
        f.write(json.dumps(visit(now - 90000, now - 89990)) + "\n")  # before the window
    with open(settings.notification_log_file, "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"ts": now - 60, "event_id": f"{now - 100:.6f}-abc", "ok": True, "seconds": 0.2}
            )
            + "\n"
        )
    asked = []

    def fetch(url):
        asked.append(url)
        return STATS if url.endswith("/api/stats") else [{"start_time": now - 99}]

    r = build(report.collect(settings, HOST, "", now, fetch=fetch))

    assert r["counts"]["visits"] == 1
    assert r["arrivals"] == {"total": 1, "by": "Frigate", "since": None, "missed": [], "why": []}
    assert failing(r) == {}
    assert "labels=person" in asked[0] and asked[1].endswith("/api/stats")


def test_reply_times_and_how_listening_ended():
    answered = [
        visit(NOW - 3600 + k, NOW - 3500 + k, endpoint="answered", reply_latency=s)
        for k, s in enumerate((1.6, 1.9, 2.2))
    ]
    lost = visit(NOW - 1000, NOW - 990, endpoint="tap_lost")
    visits = [*answered, lost]
    r = build(inputs(visits=visits, window_visits=visits))

    assert r["replies"] == {
        "count": 3,
        "endpoints": {"answered": 3, "tap_lost": 1},
        "conversations": {},
        "tap_down_turns": 0,
        "median_seconds": 1.9,
        "p90_seconds": 2.2,
        "max_seconds": 2.2,
    }
    assert "reply_speed" not in failing(r)
    assert failing(r)["audio_tap"] == "the audio tap was lost during 1 visit(s)"
    text = render_text(r)
    assert "Replies: 3, median 1.9 s after the answer (p90 2.2 s" in text
    assert "Listening ended: answered 3, tap_lost 1" in text
    assert "| answered | 1.6 |" in render_markdown(r)


def test_slow_replies_fail_on_the_gpu():
    slow = visit(NOW - 3600, NOW - 3500, endpoint="answered", reply_latency=4.0)
    r = build(inputs(visits=[slow], window_visits=[slow]))
    assert failing(r)["reply_speed"] == "replies p90 4.0 s after the answer"


def test_visits_without_the_engine_have_no_reply_checks():
    r = build(inputs())
    assert r["replies"] == {"count": 0, "endpoints": {}, "conversations": {}, "tap_down_turns": 0}
    assert not {"reply_speed", "audio_tap"} & {c["name"] for c in r["checks"]}


def test_turns_recorded_while_the_audio_tap_was_down_fail_the_audio_tap_check():
    visits = [visit(NOW - 3600 + 600 * k, NOW - 3500 + 600 * k, endpoint="window") for k in (0, 1)]
    down = (
        "2026-10-13 19:33:31,255 WARNING gatekeeper.interaction [x] "
        "audio tap down; recording a fixed window instead\n"
    )
    log = "### gatekeeper\n" + down * 3
    r = build(inputs(visits=visits, window_visits=visits, log_text=log))

    assert r["replies"]["tap_down_turns"] == 3
    assert failing(r)["audio_tap"] == "the audio tap was down for 3 listening turn(s)"
    assert "Listening ended: window 2 (audio tap down for 3 turn(s))" in render_text(r)


def test_fixed_windows_without_a_tap_configured_pass_the_audio_tap_check():
    visits = [visit(NOW - 3600, NOW - 3500, endpoint="window")]
    r = build(inputs(visits=visits, window_visits=visits))
    assert "audio_tap" not in failing(r)


def test_tailored_conversations_and_made_up_answers_are_counted():
    civic = visit(NOW - 3600, NOW - 3500, classification="civic", flow="civic")
    officer = visit(
        NOW - 2000, NOW - 1900, classification="law_enforcement", flow="law_enforcement"
    )
    log = (
        "### gatekeeper\n"
        "2026-10-13 19:33:31,255 INFO    gatekeeper.audio [-] loading Whisper model: "
        "large-v3-turbo on cuda (int8_float16)\n"
        "2026-10-13 19:46:39,352 INFO    gatekeeper.interaction [1.0-abc] ignoring 'Thanks for "
        "watching!': no speech where Whisper heard it (made up from noise)\n"
    )
    r = build(inputs(visits=[civic, officer], window_visits=[civic, officer], log_text=log))

    assert r["replies"]["conversations"] == {"civic": 1, "law_enforcement": 1}
    assert r["whisper"]["made_up_ignored"] == 1
    text = render_text(r)
    assert "Conversations: civic 1, law_enforcement 1" in text
    assert "Whisper: large-v3-turbo on cuda, no answers, 1 made-up answer(s) ignored" in text


def test_the_household_coming_back_is_counted_too():
    leaving = visit(NOW - 1800, NOW - 1797, outcome="left")
    back = visit(NOW - 1750, NOW - 1748, outcome="returned")
    r = build(inputs(visits=[leaving, back], window_visits=[leaving, back], notifications=[]))
    assert "visits_notified" not in failing(r)
    assert (r["counts"]["visits"], r["counts"]["left_by_door"], r["counts"]["returned"]) == (
        0,
        1,
        1,
    )
    assert "1 came out of the door and left, 1 came back soon after" in render_text(r)


def test_people_leaving_by_the_door_are_counted_not_expected_to_be_notified():
    greeted = visit(NOW - 3600, NOW - 3570, classification="likely_delivery")
    leaving = visit(NOW - 1800, NOW - 1797, outcome="left")
    r = build(
        inputs(
            visits=[greeted, leaving],
            window_visits=[greeted, leaving],
            arrivals=[{"start_time": NOW - 3601}, {"start_time": NOW - 1800}],
            notifications=[
                {"ts": NOW - 3570, "event_id": greeted["event_id"], "ok": True, "seconds": 0.8}
            ],
        )
    )
    assert failing(r) == {}  # both arrivals covered; only the greeted visit notified
    assert r["counts"]["visits"] == 1 and r["counts"]["left_by_door"] == 1
    assert r["counts"]["classifications"] == {"likely_delivery": 1}
    assert len(r["visits"]) == 2  # the reviewer still sees both
    assert (
        "not greeted 0; 1 came out of the door and left (not greeted, on purpose)" in render_text(r)
    )


def test_where_whisper_spends_its_time():
    log = (
        "### gatekeeper\n"
        "2026-10-14 10:00:00,000 INFO    gatekeeper.audio [-] loading Whisper model: "
        "large-v3-turbo on cuda (int8_float16)\n"
        "2026-10-14 10:01:00,000 INFO    gatekeeper.audio [a] transcribed 9.4 s of audio in "
        "1.84 s (features 0.31 s, model 1.53 s; load 2.4)\n"
        "2026-10-14 10:02:00,000 INFO    gatekeeper.audio [b] transcribed 5.0 s of audio in "
        "1.10 s (features 0.08 s, model 1.02 s; load 0.6)\n"
        "2026-10-14 10:03:00,000 INFO    gatekeeper.audio [c] transcribed 4.0 s of audio in "
        "1.05 s\n"  # before the split was logged
    )
    w = build(inputs(log_text=log))["whisper"]
    assert w["transcriptions"] == 3
    assert (w["median_model_seconds"], w["median_features_seconds"], w["median_load"]) == (
        1.02,
        0.08,
        0.6,
    )
    text = render_text(build(inputs(log_text=log)))
    assert "median 1.1 s (p90 1.8 s; model 1.0 s, features 0.1 s, CPU load 0.6)" in text


def test_silent_turns_not_transcribed_are_counted():
    log = (
        "### gatekeeper\n"
        "2026-10-14 10:00:00,000 INFO    gatekeeper.audio [-] loading Whisper model: "
        "large-v3-turbo on cuda (int8_float16)\n"
        "2026-10-14 14:30:28,000 INFO    gatekeeper.interaction [a] no speech: not transcribed\n"
        "2026-10-14 14:30:40,000 INFO    gatekeeper.interaction [a] no speech: not transcribed\n"
    )
    r = build(inputs(log_text=log))
    assert r["whisper"]["silent_turns_skipped"] == 2
    assert "no answers, 2 silent turn(s) not transcribed" in render_text(r)


def test_quick_replies_are_counted_apart_from_visits():
    greeted = visit(NOW - 3600, NOW - 3570, classification="cooperative_other")
    said = visit(
        NOW - 3600,
        NOW - 3520,
        event_id=greeted["event_id"],
        outcome="quick_reply",
        trigger="quick_reply",
        details={"reply": "coming", "result": "said", "after_seconds": 41.0, "present": True},
    )
    failed = visit(
        NOW - 3600,
        NOW - 3510,
        event_id=greeted["event_id"],
        outcome="quick_reply",
        trigger="quick_reply",
        details={"reply": "later", "result": "failed", "after_seconds": 51.0, "present": False},
    )
    r = build(inputs(visits=[greeted, said, failed], window_visits=[greeted, said, failed]))
    assert r["counts"]["visits"] == 1
    assert r["quick_replies"] == {
        "count": 2,
        "replies": {"coming": 1, "later": 1},
        "results": {"said": 1, "failed": 1},
        "nobody_there": 1,
        "median_after_seconds": 41.0,
    }
    assert failing(r)["quick_replies"] == "1 quick reply(ies) couldn't be said"
    assert (
        "Quick replies: 2 (coming 1, later 1; said 1, failed 1), 41 s after the notification "
        "(median), 1 with nobody there" in render_text(r)
    )


def test_the_time_from_someone_leaving_to_the_next_arrival_is_listed():
    # For tuning RETURN_WINDOW_SECONDS: back after 31 s (not greeted), after 121 s
    # (greeted: just outside 2 minutes), and someone 40 minutes later (not listed).
    visits = [
        visit(NOW - 9000, NOW - 8997, outcome="left"),
        visit(NOW - 8969, NOW - 8967, outcome="returned"),
        visit(NOW - 6000, NOW - 5997, outcome="left"),
        visit(NOW - 5950, NOW - 5920, trigger="button", classification="no_response"),
        visit(NOW - 5879, NOW - 5845, classification="no_response"),
        visit(NOW - 4000, NOW - 3997, outcome="left"),
        visit(NOW - 1600, NOW - 1570, classification="likely_delivery"),
    ]
    r = build(inputs(visits=visits, window_visits=visits))
    assert r["counts"]["return_gaps"] == [
        {"seconds": 31, "greeted": False},
        {"seconds": 121, "greeted": True},
    ]
    assert (
        "Next arrival after someone left by the door: 31 s (not greeted), 121 s (greeted)"
        in render_text(r)
    )


def test_return_gaps_skip_someone_stepping_out_and_count_the_household_not_greeted():
    # Someone who came out of the door and went back in isn't the next arrival: the
    # one after them is (greeted, 600 s). Someone recognized before their greeting
    # wasn't greeted; someone recognized during it was.
    visits = [
        visit(NOW - 9000, NOW - 8997, outcome="left"),
        visit(NOW - 8700, NOW - 8690, outcome="stepped_out"),
        visit(NOW - 8400, NOW - 8370, classification="no_response"),
        visit(NOW - 6000, NOW - 5997, outcome="left"),
        visit(NOW - 5700, NOW - 5690, outcome="household", details={"greeted": False}),
        visit(NOW - 4000, NOW - 3997, outcome="left"),
        visit(NOW - 3900, NOW - 3880, outcome="household", details={"greeted": True}),
    ]
    r = build(inputs(visits=visits, window_visits=visits))
    assert r["counts"]["return_gaps"] == [
        {"seconds": 600, "greeted": True},
        {"seconds": 300, "greeted": False},
        {"seconds": 100, "greeted": True},
    ]


def test_packages_and_the_doorstep_checks_are_reported():
    greeted = visit(NOW - 3600, NOW - 3570, classification="likely_delivery")
    left = visit(
        NOW - 3600,
        NOW - 3540,
        event_id="package-%.3f" % (NOW - 3600),
        outcome="package_delivered",
        trigger="package",
        details={"box": [448, 400, 512, 464], "score": 0.81, "since": NOW - 3600},
    )
    log = (
        "### gatekeeper\n"
        "2026-10-15 14:06:01,000 INFO    gatekeeper.packages [-] doorstep checked: 2 change(s)\n"
        "2026-10-15 14:06:01,200 INFO    gatekeeper.packages [-] doorstep change at "
        "[448, 400, 512, 464]: package score 0.81 (before 0.08)\n"
        "2026-10-15 14:06:01,400 INFO    gatekeeper.packages [-] doorstep change at "
        "[304, 368, 320, 480]: package score 0.11 (before 0.07)\n"
        "2026-10-15 20:56:01,000 INFO    gatekeeper.packages [-] doorstep not checked yet "
        "(exposure changed 1.80x): trying again in 60 s\n"
        "2026-10-15 21:06:01,000 INFO    gatekeeper.packages [-] doorstep not checked: "
        "the lighting didn't settle (exposure changed 1.80x)\n"
    )
    r = build(inputs(visits=[greeted, left], window_visits=[greeted, left], log_text=log))
    assert r["counts"]["visits"] == 1
    assert r["packages"] == {
        "delivered": 1,
        "picked_up": 0,
        "checked": 1,
        "not_checked": {"lighting": 1},
        "retries": 1,
        "changes": 2,
        "top_scores": [(0.81, 0.08), (0.11, 0.07)],
    }
    assert (
        "Packages: 1 delivered, 0 picked up; doorstep checked 1 time(s) (1 more not: "
        "lighting), 1 retried for the lighting, 2 change(s), highest package score 0.81"
        in render_text(r)
    )


def test_visits_where_the_speaker_did_not_connect_fail_a_check():
    failed = visit(NOW - 3600, NOW - 3590, outcome="talkback_failed")
    unavailable = visit(NOW - 2000, NOW - 1990, outcome="talkback_unavailable")
    pressed = visit(NOW - 1000, NOW - 990, outcome="pressed_during_visit", trigger="button")
    visits = [failed, unavailable, pressed]
    r = build(inputs(visits=visits, window_visits=visits))

    assert failing(r)["talkback"] == "2 visit(s) where the doorbell speaker didn't connect"
    # Not answered, so not classified: only the doorbell press is "pressed".
    assert r["counts"]["classifications"] == {"unclassified": 2, "pressed": 1}


def test_the_talkback_check_passes_when_every_visit_was_spoken_to():
    r = build(inputs())
    assert {c["name"]: c["ok"] for c in r["checks"]}["talkback"] is True


def test_ffmpeg_fixing_timestamps_after_a_dropout_is_noise():
    line = (
        "2026-10-14 00:38:00.000000000  [2026-10-13 19:38:00] ffmpeg.front_door.record"
        "       ERROR   : [vost#0:0/copy @ 0x62573f2659c0] Non-monotonic DTS; previous: "
        "439756648, current: 438004960; changing to 439756649. This may result in incorrect "
        "timestamps in the output file."
    )
    crash = (
        "2026-10-14 00:38:00.000000000  [2026-10-13 19:38:00] watchdog.front_door"
        "            ERROR   : Ffmpeg process crashed unexpectedly for front_door."
    )
    logs = summarize_logs(f"### frigate\n{line}\n{line}\n{crash}\n")

    assert logs["ignored"] == 2
    assert logs["sources"]["frigate"]["errors"] == 1  # the dropout itself still shows


def test_the_knock_classifiers_mel_filter_warning_is_noise():
    line = (
        "/usr/local/lib/python3.11/site-packages/transformers/audio_utils.py:576: "
        "UserWarning: At least one mel filter has all zero values. The value for "
        "`num_mel_filters` (128) may be set too high. Or, the value for "
        "`num_frequency_bins` (257) may be set too low."
    )
    logs = summarize_logs(f"### gatekeeper\n{line}\n")

    assert logs["ignored"] == 1
    assert logs["sources"]["gatekeeper"]["warnings"] == 0


def test_the_household_going_in_by_the_door_is_counted_apart():
    went_in = visit(NOW - 1800, NOW - 1790, outcome="entered", classification="no_response")
    r = build(
        inputs(
            visits=[went_in],
            window_visits=[went_in],
            arrivals=[{"start_time": NOW - 1800}],
            notifications=[],
        )
    )
    assert failing(r) == {}  # greeted, and not a visit to notify (it got a quiet one anyway)
    assert (r["counts"]["visits"], r["counts"]["went_inside"]) == (0, 1)
    assert (
        "Went inside without answering: 1 (greeted, then stopped; quiet notification)"
        in render_text(r)
    )


def test_each_arrival_not_greeted_says_what_gatekeeper_did_with_it():
    log = (
        "### gatekeeper\n"
        "2026-10-13 18:01:00,100 INFO    gatekeeper.sessions [-] person event 1.5-short "
        "ended before a visit: 0.4 s in the trigger zone, under the 1 s dwell\n"
    )
    r = build(
        inputs(
            arrivals=[
                {"id": "1.5-short", "start_time": NOW - 7200, "end_time": NOW - 7198.1},
                {"id": "2.5-unknown", "start_time": NOW - 5400, "end_time": None},
                {"id": "3.5-greeted", "start_time": NOW - 3601, "end_time": NOW - 3570},
            ],
            log_text=log,
        )
    )

    assert failing(r) == {"arrivals_greeted": "2 porch arrival(s) not greeted"}
    first, second = r["arrivals"]["why"]
    assert first["seconds"] == 1.9
    assert first["gatekeeper"] == (
        "ended before a visit: 0.4 s in the trigger zone, under the 1 s dwell"
    )
    assert second["seconds"] is None and second["gatekeeper"] is None
    text = render_markdown(r)
    assert f"Not greeted at {first['start']}: Frigate saw them 1.9 s; Gatekeeper: ended" in text
    assert f"Not greeted at {second['start']}: Frigate saw them (still going); " in text
    assert "Gatekeeper: nothing logged" in text


def test_the_household_recognized_by_face_is_counted_apart():
    before = visit(
        NOW - 3000, NOW - 2998, outcome="household", details={"name": "Alex", "greeted": False}
    )
    during = visit(
        NOW - 2000, NOW - 1990, outcome="household", details={"name": "Sam", "greeted": True}
    )
    log = "### gatekeeper\n" + "".join(
        f"2026-10-13 18:0{k}:00,100 INFO    gatekeeper.face_watch [-] face watch: 9 s, "
        f"30 frame(s), {n} face(s), best match {best}\n"
        for k, (n, best) in enumerate([(4, "0.61"), (0, "0.00"), (2, "0.38")])
    )
    r = build(
        inputs(
            visits=[before, during],
            window_visits=[before, during],
            arrivals=[{"start_time": NOW - 3000}, {"start_time": NOW - 2000}],
            notifications=[],
            log_text=log,
        )
    )
    assert failing(r) == {}
    rec = r["counts"]["recognized"]
    assert (rec["count"], rec["not_greeted"], rec["stopped"]) == (2, 1, 1)
    assert rec["names"] == {"Alex": 1, "Sam": 1}
    assert (rec["watches"], rec["with_faces"], rec["best_scores"]) == (3, 2, [0.61, 0.38])
    assert r["counts"]["visits"] == 0
    assert (
        "Recognized by face: 2 (not greeted 1, stopped during the visit 1): Alex 1, Sam 1; "
        "faces seen on 2 of 3 times someone was on camera" in render_text(r)
    )


def test_face_matches_that_made_no_recognition_are_reported():
    # Alex matched once (one short of FACE_MIN_MATCHES); Sam was recognized after the
    # greeting (no household record); Kim was recognized before it (a record).
    kim = visit(
        NOW - 1000, NOW - 998, outcome="household", details={"name": "Kim", "greeted": False}
    )
    lines = [
        (
            "gatekeeper.face_watch",
            "face watch: 9 s, 30 frame(s), 2 face(s), best match 0.63; Alex x1",
        ),
        ("gatekeeper.sessions", "person event 1800000000.5-sam recognized as Sam"),
        (
            "gatekeeper.face_watch",
            "face watch: 20 s, 80 frame(s), 5 face(s), best match 0.71; Sam x3",
        ),
        ("gatekeeper.sessions", f"person event {kim['event_id']} recognized as Kim"),
        (
            "gatekeeper.face_watch",
            "face watch: 9 s, 30 frame(s), 3 face(s), best match 0.66; Kim x2; Alex x1",
        ),
        ("gatekeeper.face_watch", "face watch: 9 s, 30 frame(s), 1 face(s), best match 0.40"),
    ]
    log = "### gatekeeper\n" + "".join(
        f"2026-10-13 18:0{k}:00,100 INFO    {logger} [-] {message}\n"
        for k, (logger, message) in enumerate(lines)
    )
    r = build(
        inputs(
            visits=[kim],
            window_visits=[kim],
            arrivals=[{"start_time": NOW - 1000}],
            notifications=[],
            log_text=log,
        )
    )
    rec = r["counts"]["recognized"]
    assert rec["count"] == 1
    assert rec["matched"] == {"Alex": 2, "Sam": 1, "Kim": 1}
    assert rec["no_record"] == {"Sam": 1}
    assert (
        "times someone was on camera; matched in watches: Alex 2, Sam 1, Kim 1; recognized "
        "with no household record (after the greeting, or they answered): Sam 1" in render_text(r)
    )


def test_what_was_done_on_the_faces_page_is_reported():
    log = "### gatekeeper\n" + "".join(
        f"2026-10-13 18:0{k}:00,100 INFO    {logger} [-] {message}\n"
        for k, (logger, message) in enumerate(
            [
                ("gatekeeper.face_watch", "capturing faces at the door for 120 s"),
                ("gatekeeper.web", "web: enrolled 6 face(s) for Alex"),
                ("gatekeeper.web", "web: enrolled 2 face(s) for Alex"),
                ("gatekeeper.web", "web: enrolled 4 face(s) for Sam"),
                ("gatekeeper.web", "web: deleted 3 kept face(s)"),
                ("gatekeeper.web", "web: forgot Sam"),
                ("gatekeeper.web", "web: purged every face"),
            ]
        )
    )
    r = build(inputs(log_text=log))
    assert r["counts"]["recognized"]["faces_page"] == {
        "enrolled": {"Alex": 8, "Sam": 4},
        "deleted": 3,
        "forgot": ["Sam"],
        "purged": 1,
        "captures": 1,
    }
    assert (
        "Faces page: enrolled Alex 8, Sam 4; deleted 3 kept face(s); forgot Sam; "
        "purged every face (1x); captured at the door 1 time(s)" in render_text(r)
    )


def test_nothing_done_on_the_faces_page_adds_nothing():
    r = build(inputs())
    assert "faces_page" not in r["counts"]["recognized"]
    assert "Faces page" not in render_text(r)


def stamp(ts: float) -> str:
    """A time as Gatekeeper's log writes it (local time)."""
    return report.datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") + ",100"


def test_an_arrival_while_gatekeeper_wasnt_listening_says_so():
    log = (
        "### gatekeeper\n"
        f"{stamp(NOW - 7300)} INFO    gatekeeper.app [-] Gatekeeper 0.3.0 starting: "
        "camera=front_door zones=porch mqtt=mosquitto:1883 go2rtc=http://go2rtc:1984\n"
        f"{stamp(NOW - 7200)} INFO    gatekeeper.app [-] connected to MQTT rc=Success\n"
        f"{stamp(NOW - 5000)} WARNING gatekeeper.app [-] disconnected from MQTT: "
        "Unspecified error\n"
        f"{stamp(NOW - 4900)} INFO    gatekeeper.app [-] connected to MQTT rc=Success\n"
    )
    recreated = report.datetime.datetime.fromtimestamp(NOW - 7300, report.datetime.UTC)
    r = build(
        inputs(
            arrivals=[
                {"id": "1-before", "start_time": NOW - 7400, "end_time": NOW - 7390},
                {"id": "2-starting", "start_time": NOW - 7250, "end_time": NOW - 7237},
                {"id": "3-up", "start_time": NOW - 6000, "end_time": NOW - 5990},
                {"id": "4-mqtt", "start_time": NOW - 4950, "end_time": NOW - 4940},
                {"id": "5-greeted", "start_time": NOW - 3601, "end_time": NOW - 3570},
            ],
            log_text=log,
            host={**HOST, "created": {"gatekeeper": recreated.strftime("%Y-%m-%dT%H:%M:%S")}},
        )
    )

    assert [m["gatekeeper"] for m in r["arrivals"]["why"]] == [
        "before Gatekeeper's log starts (container recreated)",
        "not listening yet (Gatekeeper starting)",
        None,  # up and listening: a real "nothing logged"
        "not listening (MQTT disconnected)",
    ]
    text = render_markdown(r)
    assert "Gatekeeper: not listening yet (Gatekeeper starting)" in text
    assert "Gatekeeper: nothing logged" in text


def test_an_arrival_after_a_start_that_never_connected_wasnt_listened_for():
    log = (
        "### gatekeeper\n"
        f"{stamp(NOW - 600)} INFO    gatekeeper.app [-] Gatekeeper 0.3.0 starting: camera=x\n"
    )
    r = build(
        inputs(
            arrivals=[{"id": "1-late", "start_time": NOW - 300, "end_time": NOW - 290}],
            log_text=log,
        )
    )
    assert r["arrivals"]["why"][0]["gatekeeper"] == "not listening yet (Gatekeeper starting)"


def test_what_the_audio_check_did_is_reported():
    def at(seconds_ago):
        return report.datetime.datetime.fromtimestamp(NOW - seconds_ago).astimezone().isoformat()

    lines = [
        f"{at(90000)} recordings silent, alerting",  # before the window
        f"{at(7200)} recordings silent, restarting Frigate",
        f"{at(7100)} recordings have audio again",
        f"{at(3600)} recordings silent; a deploy or the report held the lock for 15 min",
    ]
    r = build(inputs(host={**HOST, "audio_check": lines}))

    assert r["recording_audio"]["audio_check"] == {
        "restarted": [report.local(NOW - 7200)],
        "audio_back": 1,
        "alerted": [],
        "lock_busy": 1,
    }
    assert "recording_audio" not in failing(r)
    assert (
        f"Recording audio: OK; audio check restarted Frigate at {report.local(NOW - 7200)} "
        "(audio back 1), lock busy 1 time(s)"
    ) in render_text(r)


def test_an_audio_check_alert_fails_the_recording_audio_check():
    alert = report.datetime.datetime.fromtimestamp(NOW - 600).astimezone().isoformat()
    r = build(inputs(host={**HOST, "audio_check": [f"{alert} recordings silent, alerting"]}))

    assert failing(r)["recording_audio"] == (
        "audio check alerted: restarting Frigate didn't bring the audio back"
    )
    assert f"audio check alerted at {report.local(NOW - 600)}" in render_text(r)


def test_the_household_stepping_out_is_counted_and_covers_its_arrival():
    out = visit(NOW - 1800, NOW - 1770, outcome="stepped_out")
    r = build(
        inputs(
            visits=[out],
            window_visits=[out],
            arrivals=[{"start_time": NOW - 1800}],
            notifications=[],
        )
    )
    assert failing(r) == {}  # not an arrival "not greeted"
    assert (r["counts"]["visits"], r["counts"]["stepped_out"]) == (0, 1)
    assert "1 came out and went back in (not greeted, on purpose)" in render_text(r)


def own_log(*lines):
    return "### gatekeeper\n" + "".join(
        f"{stamp(ts)} INFO    gatekeeper.{logger} [-] {message}\n" for ts, logger, message in lines
    )


def own_track(start, end, zones):
    return (
        end + 3,
        "own_perception",
        f"own perception: person track {start:.6f}-own1 start={start:.1f} end={end:.1f} "
        f"zones={zones} path=(0.20,0.50)->(0.70,0.90) best=0.88 hits=12",
    )


def test_own_perception_is_compared_with_frigates_arrivals():
    arrivals = [
        {"start_time": NOW - 9000, "end_time": NOW - 8990},  # before it started
        {"start_time": NOW - 5000, "end_time": NOW - 4980},  # seen on the porch
        {"start_time": NOW - 4000, "end_time": NOW - 3990},  # seen, but not on the porch
        {"start_time": NOW - 3000, "end_time": None},  # missed (still going)
    ]
    log = own_log(
        (NOW - 8000, "app", "own perception on (shadow): go2rtc:8554/front_door_sub, zones porch"),
        own_track(NOW - 4997, NOW - 4975, "porch,door"),
        own_track(NOW - 3993, NOW - 3985, "-"),
        own_track(NOW - 3500, NOW - 3490, "porch"),  # Frigate had nobody then
        (
            NOW - 1000,
            "own_perception",
            "own perception stats: 10800 frames in 3600 s, 900 through the detector "
            "(median 190 ms), 0 track(s) now",
        ),
        (
            NOW - 100,
            "own_perception",
            "own perception stats: 7200 frames in 3600 s, 600 through the detector "
            "(median 180 ms on cuda), 0 track(s) now",
        ),
        (
            NOW - 50,
            "own_perception",
            "own perception stats: 0 frames in 3600 s, 0 through the detector "
            "(median 0 ms on cuda), 0 track(s) now",
        ),
    )
    r = build(
        inputs(
            arrivals=arrivals,
            log_text=log,
            trigger_zones=("porch",),
            own_perception="shadow",
            window_visits=[],
            visits=[],
        )
    )
    own = r["own_perception"]
    assert (own["arrivals"], own["seen"], own["tracks"]) == (3, 1, 3)
    assert own["seen_elsewhere"] == [report.local(NOW - 4000)]
    assert own["missed"] == [report.local(NOW - 3000)]
    assert own["extra"] == [report.local(NOW - 3500)]
    assert (own["frames"], own["detected"]) == (18000, 1500)
    # The first line predates the device being logged (the CPU); then two hours on the GPU,
    # one of them with nobody around (the detector didn't run: no median from it).
    assert own["devices"] == [
        {"device": "cpu", "hours": 1.0, "fps": 3.0, "detector_ms": 190},
        {"device": "cuda", "hours": 2.0, "fps": 1.0, "detector_ms": 180},
    ]
    assert own["device"] == "cuda"
    assert "own_perception" not in failing(r)
    assert (
        f"Own perception (shadow, since {report.local(NOW - 8000)}): saw 1 of 3 of Frigate's "
        f"porch arrivals in the zone, 1 elsewhere, missed {report.local(NOW - 3000)}; "
        f"3 track(s), 1 on the porch Frigate didn't have ({report.local(NOW - 3500)}); "
        "detector on 1500 of 18000 frames: on cpu 3.0 fps, median 190 ms (1 h); "
        "on cuda 1.0 fps, median 180 ms (2 h)" in render_text(r)
    )


def test_an_arrival_while_gatekeeper_restarted_isnt_an_own_perception_miss():
    arrivals = [
        {"id": "1-restart", "start_time": NOW - 4000, "end_time": NOW - 3990},
        {"id": "2-missed", "start_time": NOW - 2000, "end_time": NOW - 1990},
    ]
    on = "own perception on (shadow): go2rtc:8554/front_door_sub, zones porch"
    reading = "own perception reading rtsp://go2rtc:8554/front_door_sub"
    log = own_log(
        (NOW - 8000, "app", on),
        (NOW - 7990, "own_perception", reading),
        (NOW - 4010, "app", "Gatekeeper stopping (SIGTERM)"),
        (NOW - 3950, "app", "Gatekeeper 0.3.0 starting: camera=front_door"),
        (NOW - 3900, "app", "connected to MQTT rc=Success"),
        (NOW - 3850, "app", on),
        (NOW - 3845, "own_perception", reading),
    )
    r = build(
        inputs(
            arrivals=arrivals,
            log_text=log,
            trigger_zones=("porch",),
            own_perception="shadow",
            visits=[],
            window_visits=[],
        )
    )
    own = r["own_perception"]
    assert (own["arrivals"], own["seen"]) == (2, 0)
    assert own["missed"] == [report.local(NOW - 2000)]
    assert own["restarting"] == [report.local(NOW - 4000)]
    assert f"1 while Gatekeeper restarted ({report.local(NOW - 4000)})" in render_text(r)
    # And it wasn't greeted because Gatekeeper wasn't listening.
    assert [w["gatekeeper"] for w in r["arrivals"]["why"]] == [
        "not listening (Gatekeeper restarting)",
        None,
    ]


def test_own_perception_on_but_silent_fails_a_check():
    assert build(inputs())["own_perception"] is None
    assert "own_perception" not in failing(build(inputs()))
    assert failing(build(inputs(own_perception="shadow")))["own_perception"] == (
        "own perception is on but logged nothing"
    )


def button_log(*lines):
    return "### gatekeeper\n" + "".join(
        f"{stamp(ts)} {level:7s} gatekeeper.button [-] {message}\n" for ts, level, message in lines
    )


def test_the_doorbell_restarting_and_updating_is_reported():
    log = button_log(
        (
            NOW - 9000,
            "INFO",
            "listening for button presses on Reolink Video Doorbell WiFi "
            "(firmware v3.0.0.6460_2605211897)",
        ),
        (
            NOW - 6000,
            "ERROR",
            "doorbell button listener stopped: push events stopped (did the doorbell restart?)",
        ),
        (NOW - 5995, "ERROR", "doorbell button listener stopped: Cannot connect to host"),
        (
            NOW - 5850,
            "INFO",
            "listening for button presses on Reolink Video Doorbell WiFi "
            "(firmware v3.0.0.6700_2611010000)",
        ),
    )
    r = build(inputs(log_text=log))
    bell = r["doorbell"]
    assert bell["firmware"] == ["v3.0.0.6460_2605211897", "v3.0.0.6700_2611010000"]
    assert bell["listening"] and len(bell["stops"]) == 1
    assert bell["stops"][0]["back_minutes"] == 2.5
    assert "doorbell_button" not in failing(r)
    assert (
        "Doorbell: firmware v3.0.0.6460_2605211897 → v3.0.0.6700_2611010000 (updated); "
        f"button listener restarted: {report.local(NOW - 6000)}, back after 2.5 min"
        in render_text(r)
    )


def test_the_doorbell_button_not_listening_fails_a_check():
    log = button_log(
        (
            NOW - 9000,
            "INFO",
            "listening for button presses on Reolink Video Doorbell WiFi (firmware v3)",
        ),
        (NOW - 600, "ERROR", "doorbell button listener stopped: Cannot connect to host"),
    )
    r = build(inputs(log_text=log))
    assert failing(r)["doorbell_button"] == (
        f"the doorbell button isn't heard: its listener stopped at {report.local(NOW - 600)}"
    )
    assert build(inputs())["doorbell"] is None
    assert "doorbell_button" not in failing(build(inputs()))


def test_own_perception_compares_from_its_first_track_before_a_restart():
    # Running all day; a deploy restarted it in the evening, so the window's only "on"
    # line is late. The morning's arrival still counts, and was seen.
    arrivals = [{"start_time": NOW - 9000, "end_time": NOW - 8990}]
    log = own_log(
        own_track(NOW - 8998, NOW - 8985, "porch"),
        (NOW - 3000, "app", "own perception on (shadow): go2rtc:8554/front_door_sub, zones porch"),
    )
    r = build(
        inputs(
            arrivals=arrivals, log_text=log, trigger_zones=("porch",), visits=[], window_visits=[]
        )
    )
    own = r["own_perception"]
    assert (own["arrivals"], own["seen"]) == (1, 1)
    assert own["since"] == report.local(NOW - 8998)


def test_the_doorbell_restarting_isnt_a_log_error():
    log = button_log(
        (
            NOW - 600,
            "ERROR",
            "Baichuan host 192.168.40.190: lost event subscription after 10.41 s and failed to "
            "reestablished connection",
        ),
    )
    assert "gatekeeper_log_errors" not in failing(build(inputs(log_text=log)))


def test_own_perception_since_is_within_the_window():
    # The first stats line, just after the window opens, covers the hour before it.
    log = own_log(
        (
            NOW - 24 * 3600 + 600,
            "own_perception",
            "own perception stats: 10800 frames in 3600 s, 0 through the detector "
            "(median 0 ms on cuda), 0 track(s) now",
        ),
    )
    own = build(inputs(log_text=log, trigger_zones=("porch",)))["own_perception"]
    assert own["since"] == report.local(NOW - 24 * 3600)
    assert own["devices"] == [{"device": "cuda", "hours": 1.0, "fps": 3.0, "detector_ms": None}]


def test_a_couriers_further_trips_are_counted_apart():
    first = visit(NOW - 3000, NOW - 2975, classification="no_response")
    trips = [visit(NOW - 2900, NOW - 2898, outcome="delivery_trip")]
    trips.append(visit(NOW - 2850, NOW - 2848, outcome="delivery_trip"))
    records = [first, *trips]
    r = build(
        inputs(
            visits=records,
            window_visits=records,
            arrivals=[{"start_time": t} for t in (NOW - 3000, NOW - 2900, NOW - 2850)],
            notifications=[
                {"ts": NOW - 2975, "event_id": first["event_id"], "ok": True, "seconds": 0.2}
            ],
        )
    )
    assert r["counts"]["visits"] == 1 and r["counts"]["delivery_trips"] == 2
    assert failing(r) == {}
    assert "2 more trip(s) of a delivery (not greeted, on purpose)" in render_text(r)
    left = visit(NOW - 9000, NOW - 8997, outcome="left")
    trip = visit(NOW - 8900, NOW - 8898, outcome="delivery_trip")
    assert report.return_gaps([left, trip]) == [{"seconds": 100, "greeted": False}]


def test_the_delivery_guess_is_held_to_the_owners_labels():
    from gatekeeper.report import delivery_guess_info

    guesses = {
        "a": {"guess": "delivery", "at": NOW - 100},
        "b": {"guess": "other", "at": NOW - 200},
        "c": {"guess": "delivery", "at": NOW - 300},
        "d": {"guess": "other", "at": NOW - 90000},  # before the window
    }
    labels = {"a": {"label": "ups"}, "b": {"label": "food"}, "c": {"label": "household"}}
    info = delivery_guess_info(guesses, labels, NOW - 86400)
    assert info == {
        "guessed": 3,
        "deliveries_guessed": 2,
        "deliveries": {"right": 1, "of": 2},
        "others": {"right": 0, "of": 1},
    }
    r = build(inputs(delivery_guess=info))
    assert (
        "Delivery guess (shadow): 3 visit(s) guessed, 2 as deliveries; against your labels so "
        "far, deliveries 1 of 2 right, others 0 of 1" in render_text(r)
    )
    assert delivery_guess_info({}, labels, 0) is None


def test_knocks_are_counted():
    knocks = [
        {"at": NOW - 3600, "score": 0.9, "on_camera": False, "in_visit": False},
        {"at": NOW - 1800, "score": 0.7, "on_camera": True, "in_visit": True},
    ]
    r = build(inputs(knocks=knocks))
    assert r["knocks"]["heard"] == 2 and r["knocks"]["nobody_on_camera"] == 1
    assert (
        f"Knocks heard (shadow): 2 (1 with nobody on camera, 1 during a visit): "
        f"{report.local(NOW - 3600)}, {report.local(NOW - 1800)}" in render_text(r)
    )
    assert "Knocks heard (shadow): 0; no checks logged" in render_text(build(inputs(knocks=[])))
    assert "Knocks heard" not in render_text(build(inputs()))  # the listener is off


def test_the_knock_listeners_checks_show_it_listening():
    log = "### gatekeeper\n" + "".join(
        f"2026-10-28 {h}:00:00,000 INFO     gatekeeper.knock [-] knock listener stats: "
        f"{n} check(s) in 3600 s, top score {top}\n"
        for h, n, top in (("10", 12, "0.04"), ("11", 0, "0.00"), ("12", 25, "0.31"))
    )
    r = build(inputs(knocks=[], log_text=log))
    assert r["knocks"] == {
        "heard": 0,
        "nobody_on_camera": 0,
        "during_visit": 0,
        "at": [],
        "stats_hours": 3,
        "checks": 37,
        "top_score": 0.31,
    }
    assert "Knocks heard (shadow): 0; 37 sound(s) checked, top score 0.31" in render_text(r)


def test_the_owners_labels_are_compared_with_what_gatekeeper_did():
    labelled = [
        visit(NOW - 9000, NOW - 8997, outcome="left", label="food"),
        visit(NOW - 5000, NOW - 4970, classification="no_response", label="groceries"),
        visit(NOW - 4900, NOW - 4898, outcome="delivery_trip", label="groceries"),
        {"event_id": "1700000000.0-gone", "label": "ups"},  # older than the log
    ]
    r = build(inputs(labelled=labelled))
    assert r["counts"]["labels"] == {
        "food": {"left": 1},
        "groceries": {"no_response": 1, "delivery_trip": 1},
        "ups": {"?": 1},
    }
    assert (
        "Your labels (Visits page): Food delivery (pizza, DoorDash, Uber Eats...) 1 "
        "(Gatekeeper: left 1); Groceries (Instacart, Walmart...) 2 (Gatekeeper: "
        "no_response 1, delivery_trip 1); UPS 1 (Gatekeeper: ? 1)" in render_text(r)
    )


def test_which_no_answer_lines_get_people_talking():
    def silent(start, key, heard=""):
        return visit(
            start,
            start + 20,
            classification="no_response" if not heard else "cooperative_other",
            dialogue=[{"asked": "greeting", "heard": ""}, {"asked": key, "heard": heard}],
        )

    visits = [
        silent(NOW - 9000, "no_answer_3"),
        silent(NOW - 8000, "no_answer_3", heard="Oh sorry, I'm here for the meter"),
        silent(NOW - 7000, "no_answer_5"),
    ]
    texts = {"no_answer_3": "Press one to explain why you're here. Press two to leave."}
    r = build(inputs(visits=visits, window_visits=visits, line_texts=texts, notifications=[]))
    assert r["counts"]["no_answer_lines"] == {
        "no_answer_3": {"said": 2, "answered": 1},
        "no_answer_5": {"said": 1, "answered": 0},
    }
    assert r["counts"]["greeting_lines"] == {"greeting": {"said": 3, "answered": 0}}
    text = render_text(r)
    assert "Greetings: said 3 time(s), answered 0" in text
    assert (
        "No-answer lines: said 3 time(s), answered 1; answered after no_answer_3 1/2 (Press "
        "one to explain why you're here. Pr...)" in text
    )


def test_send_offs_are_counted():
    gone = visit(
        NOW - 4000, NOW - 3990, classification="no_response", details={"walked_away": True}
    )
    r = build(inputs(visits=[gone], window_visits=[gone], notifications=[]))
    assert r["counts"]["walked_away"] == 1
    assert "Walked away after the greeting: 1 (a send-off, not the no-answer line)" in render_text(
        r
    )


def test_gatekeepers_own_clips_are_reported():
    log = own_log(
        (NOW - 9000, "app", "recording clips to /clips"),
        (NOW - 5000, "recording", "recording 1800000000.100.mp4"),
        (NOW - 4980, "recording", "recorded 1800000000.100.mp4: 20 s, 1 person(s), 10.5 MB"),
        (NOW - 3000, "recording", "recorded 1800002000.100.mp4: 70 s, 2 person(s), 36.0 MB"),
        (NOW - 1000, "recording", "recording failed"),
    )
    r = build(inputs(log_text=log))
    assert r["recording"] == {"clips": 2, "minutes": 1.5, "people": 3, "mb": 46, "failed": 1}
    assert failing(r)["recording"] == "1 recording(s) failed"
    assert "Recording: 2 clip(s), 1.5 min, 46 MB, 3 person(s) pictured; 1 failed" in render_text(r)
    assert build(inputs())["recording"] is None


def test_with_own_perception_starting_the_visits_its_tracks_are_the_arrivals():
    visit_start = NOW - 5000
    greeted = visit(visit_start, visit_start + 30)
    log = own_log(
        (NOW - 9000, "app", "own perception on (visits): go2rtc:8554/front_door_sub, zones porch"),
        own_track(visit_start - 3, visit_start + 20, "porch,door"),  # greeted
        own_track(NOW - 3000, NOW - 2990, "porch"),  # not greeted
        own_track(NOW - 2000, NOW - 1990, "-"),  # on the walkway: not an arrival
    )
    r = build(
        inputs(
            arrivals=None,  # no Frigate (a hub): not a problem now
            log_text=log,
            trigger_zones=("porch",),
            perception="own",
            visits=[greeted],
            window_visits=[greeted],
        )
    )
    assert r["arrivals"]["total"] == 2 and r["arrivals"]["by"] == "own perception"
    assert [m["start"] for m in r["arrivals"]["why"]] == [report.local(NOW - 3000)]
    assert failing(r)["arrivals_greeted"] == "1 porch arrival(s) not greeted"
    assert "frigate_api" not in failing(r)
    text = render_text(r)
    assert "Porch arrivals (own perception): 2, not greeted 1" in text
    assert f"Own perception (starts the visits, since {report.local(NOW - 9000)})" in text


def test_own_perceptions_arrivals_count_from_the_switch():
    # Frigate started the visits until a restart in the evening; own perception's tracks
    # before then were shadow tracks, not arrivals it had to greet.
    log = own_log(
        (NOW - 9000, "app", "own perception on (shadow): go2rtc:8554/front_door_sub, zones porch"),
        own_track(NOW - 8000, NOW - 7990, "porch"),  # Frigate's time (and it had nobody)
        (NOW - 4000, "app", "own perception on (visits): go2rtc:8554/front_door_sub, zones porch"),
        own_track(NOW - 3000, NOW - 2990, "porch"),
    )
    r = build(
        inputs(arrivals=[], log_text=log, trigger_zones=("porch",), perception="own", visits=[])
    )
    assert r["arrivals"]["total"] == 1 and r["arrivals"]["since"] == report.local(NOW - 4000)
    assert f"Porch arrivals (own perception) since {report.local(NOW - 4000)}: 1" in render_text(r)
