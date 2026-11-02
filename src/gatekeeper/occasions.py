"""Occasions: evenings with their own sound and lines. Halloween for now.

On Halloween (``HALLOWEEN``: "auto" is October 31 from ``HALLOWEEN_START_HOUR`` to
``HALLOWEEN_END_HOUR``, local time; "on" and "off" force it), a doorbell press plays a spooky
sound before the greeting, the greetings, no-answer lines and send-offs are Halloween ones,
and "trick or treat" gets a treat line (classify.py, config.HALLOWEEN_*). A covered face
means nothing that night: it's a costume.
"""

from __future__ import annotations

import datetime
import wave
from pathlib import Path

import numpy as np

from gatekeeper.config import Settings

SPOOKY_KEY = "halloween_sound"  # its phrase key: played like a pre-synthesized line
SPOOKY_RATE = 24000  # Kokoro's rate, like every other clip


def is_halloween(settings: Settings, when: datetime.datetime) -> bool:
    """Is it Halloween evening at ``when`` (local time)?"""
    if settings.halloween != "auto":
        return settings.halloween == "on"
    return (
        when.month == 10
        and when.day == 31
        and settings.halloween_start_hour <= when.hour < settings.halloween_end_hour
    )


def spooky_sound(path: Path, rate: int = SPOOKY_RATE) -> Path:
    """Write the press sound: a theremin-like "ooo-EEE-ooo" of about 2.4 s, two wavering
    tones a fifth apart gliding up and down, with a little echo. Made here, so there's no
    recording to license."""
    t = np.arange(int(2.4 * rate)) / rate
    pitch = np.interp(t, [0.0, 0.5, 1.2, 1.9, 2.4], [330.0, 520.0, 700.0, 440.0, 380.0])
    pitch = pitch * (1 + 0.025 * np.sin(2 * np.pi * 5.5 * t))  # the waver
    phase = 2 * np.pi * np.cumsum(pitch) / rate
    tone = np.sin(phase) + 0.35 * np.sin(1.5 * phase) + 0.15 * np.sin(2 * phase)
    sound = tone * np.minimum(1, t / 0.25) * np.minimum(1, (t[-1] - t) / 0.6)
    delay = int(0.22 * rate)
    sound[delay:] += 0.35 * sound[:-delay].copy()
    sound *= np.minimum(1, (t[-1] - t) / 0.3)  # the echo fades out too: no click at the end
    sound = 0.6 * sound / np.max(np.abs(sound))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes((sound * 32767).astype("<i2").tobytes())
    return path
