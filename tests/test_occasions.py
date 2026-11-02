import datetime
import re
import wave

import numpy as np
import pytest

from gatekeeper.config import (
    GREETING_LINES,
    HALLOWEEN_GOODBYE_LINES,
    HALLOWEEN_GREETING_LINES,
    HALLOWEEN_NO_ANSWER_LINES,
    HALLOWEEN_REPLY_LINES,
    Settings,
)
from gatekeeper.occasions import SPOOKY_RATE, is_halloween, spooky_sound


def at(month, day, hour, minute=0):
    return datetime.datetime(2026, month, day, hour, minute)


def test_halloween_is_the_evening_of_october_31(settings):
    assert settings.halloween == "auto"
    assert is_halloween(settings, at(10, 31, 16))
    assert is_halloween(settings, at(10, 31, 21, 59))
    assert not is_halloween(settings, at(10, 31, 15, 59))
    assert not is_halloween(settings, at(10, 31, 22))
    assert not is_halloween(settings, at(10, 30, 18))
    assert not is_halloween(settings, at(11, 1, 18))


def test_the_hours_and_the_switch(settings):
    settings.halloween_start_hour, settings.halloween_end_hour = 17, 21
    assert not is_halloween(settings, at(10, 31, 16, 30))
    assert is_halloween(settings, at(10, 31, 20))
    settings.halloween = "on"
    assert is_halloween(settings, at(3, 1, 9))
    settings.halloween = "off"
    assert not is_halloween(settings, at(10, 31, 19))


def test_the_hours_must_make_sense():
    with pytest.raises(ValueError, match="HALLOWEEN_START_HOUR"):
        Settings(halloween_start_hour=22, halloween_end_hour=16)


def test_the_spooky_sound(tmp_path):
    path = spooky_sound(tmp_path / "boo.wav")
    with wave.open(str(path)) as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, SPOOKY_RATE)
        samples = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
    assert len(samples) / SPOOKY_RATE == pytest.approx(2.4)
    peak = np.abs(samples).max() / 32767
    assert 0.5 < peak <= 0.61  # loud enough, never clipping
    assert abs(int(samples[0])) < 300 and abs(int(samples[-1])) < 300  # fades in and out


def test_the_treats_decide_the_answer(settings):
    for treats in ("unset", "bowl", "none"):
        settings.halloween_treats = treats
        assert list(settings.halloween_reply_texts().values()) == list(
            HALLOWEEN_REPLY_LINES[treats]
        )
    settings.halloween_reply_lines = ["Boo!"]
    assert settings.halloween_reply_texts() == {"halloween_reply_1": "Boo!"}


def test_every_halloween_line_is_presynthesized_unless_its_off(settings):
    texts = settings.halloween_texts()
    assert len(texts) == (
        len(HALLOWEEN_GREETING_LINES)
        + len(HALLOWEEN_NO_ANSWER_LINES)
        + len(HALLOWEEN_GOODBYE_LINES)
        + len(HALLOWEEN_REPLY_LINES["unset"])
    )
    assert all(k.startswith("halloween_") for k in texts)
    settings.halloween = "off"
    assert settings.halloween_texts() == {}


def test_the_halloween_lines_keep_the_house_rules():
    # Every greeting says the door is watched or recorded, like GREETING_LINES.
    assert all(re.search("watch|record", line) for line in HALLOWEEN_GREETING_LINES)
    assert all(re.search("monitor|record|watch|camera", line) for line in GREETING_LINES)
    every = (
        HALLOWEEN_GREETING_LINES
        + HALLOWEEN_NO_ANSWER_LINES
        + HALLOWEEN_GOODBYE_LINES
        + sum(HALLOWEEN_REPLY_LINES.values(), ())
    )
    # Nothing says anyone is home or will come to the door (the treats are in a bowl).
    banned = r"\b(home|come in|coming|be right|i'll get|someone will|answer the door)\b"
    assert not [line for line in every if re.search(banned, line, re.I)]
