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


def test_detection_that_drops_frames_fails():
    stats = {"detectors": {}, "cameras": {"front_door": {"camera_fps": 5.0, "skipped_fps": 2.0}}}
    assert failing(build(inputs(stats=stats)))["detection_keeps_up"] == (
        "detection skips 2.0 of 5.0 fps"
    )


def test_a_camera_dropout_is_fine_but_frigate_failing_all_day_is_not():
    dropout = "### frigate\n" + FRIGATE_LOG
    assert "frigate_log_errors" not in failing(build(inputs(log_text=dropout)))

    all_day = "### frigate\n" + (FRIGATE_LOG.splitlines()[0] + "\n") * (report.FRIGATE_ERRORS + 1)
    assert failing(build(inputs(log_text=all_day)))["frigate_log_errors"] == (
        f"{report.FRIGATE_ERRORS + 1} error(s) in Frigate's log"
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
    assert r["arrivals"] == {"total": 1, "missed": []}
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
        "2026-10-15 21:06:01,000 INFO    gatekeeper.packages [-] doorstep not checked: "
        "exposure changed 1.80x\n"
    )
    r = build(inputs(visits=[greeted, left], window_visits=[greeted, left], log_text=log))
    assert r["counts"]["visits"] == 1
    assert r["packages"] == {
        "delivered": 1,
        "picked_up": 0,
        "checked": 1,
        "not_checked": {"lighting": 1},
        "changes": 2,
        "top_scores": [(0.81, 0.08), (0.11, 0.07)],
    }
    assert (
        "Packages: 1 delivered, 0 picked up; doorstep checked 1 time(s) (1 more not: "
        "lighting), 2 change(s), highest package score 0.81" in render_text(r)
    )
