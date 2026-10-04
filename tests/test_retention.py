import os
import time

from gatekeeper.eventlog import EventLog
from gatekeeper.retention import DAY, Retention

NOW = time.time()


def touch(path, age_days, content=b"x"):
    path.write_bytes(content)
    t = NOW - age_days * DAY
    os.utime(path, (t, t))
    return path


def test_removes_old_audio_and_logs_but_keeps_presynth(settings):
    old_in = touch(settings.in_dir / "old.wav", 8)
    new_in = touch(settings.in_dir / "new.wav", 1)
    old_reply = touch(settings.out_dir / "e1_reply.wav", 8)
    presynth = touch(settings.out_dir / "_presynth_greeting.wav", 300)
    old_log = touch(settings.log_dir / "events-2026-08-01.jsonl", 31)
    new_log = touch(settings.log_dir / "events-2026-09-25.jsonl", 6)
    old_notifications = touch(settings.log_dir / "notifications-2026-08-01.jsonl", 31)
    current = touch(settings.event_log_file, 40)  # only daily files are purged

    removed = Retention(settings, clock=lambda: NOW).run_once()

    assert removed == {"audio_in": 1, "audio_out": 1, "event_logs": 2}
    assert not old_in.exists() and not old_reply.exists() and not old_log.exists()
    assert not old_notifications.exists()
    assert new_in.exists() and presynth.exists() and new_log.exists() and current.exists()


def test_zero_days_keeps_everything(settings):
    settings.audio_retention_days = 0
    settings.event_log_retention_days = 0
    old = touch(settings.in_dir / "old.wav", 1000)
    old_log = touch(settings.log_dir / "events-2020-01-01.jsonl", 1000)
    assert Retention(settings, clock=lambda: NOW).run_once() == {
        "audio_in": 0, "audio_out": 0, "event_logs": 0,
    }  # fmt: skip
    assert old.exists() and old_log.exists()


def test_event_log_rotates_daily(tmp_path):
    path = tmp_path / "events.jsonl"
    clock = [NOW]
    log = EventLog(path, clock=lambda: clock[0])
    log.append({"n": 1})
    yesterday = NOW - DAY
    os.utime(path, (yesterday, yesterday))

    log.append({"n": 2})

    day = time.strftime("%Y-%m-%d", time.localtime(yesterday))
    rotated = tmp_path / f"events-{day}.jsonl"
    assert rotated.read_text(encoding="utf-8") == '{"n": 1}\n'
    assert path.read_text(encoding="utf-8") == '{"n": 2}\n'


def test_event_log_same_day_appends(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path, clock=lambda: NOW)
    log.append({"n": 1})
    log.append({"n": 2})
    assert path.read_text(encoding="utf-8").splitlines() == ['{"n": 1}', '{"n": 2}']
    assert list(tmp_path.iterdir()) == [path]
