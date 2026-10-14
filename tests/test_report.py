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
