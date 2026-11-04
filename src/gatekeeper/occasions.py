"""Occasions: evenings with their own sound and lines. Halloween for now.

On Halloween (``HALLOWEEN``: "auto" is October 31 from ``HALLOWEEN_START_HOUR`` to
``HALLOWEEN_END_HOUR``, local time; "on" and "off" force it), a doorbell press plays a spooky
sound before the greeting (``HALLOWEEN_PRESS_SOUND``: a witch's cackle, a theremin, or the
owner's own file), the greetings, no-answer lines and send-offs are Halloween ones,
and "trick or treat" gets a treat line (classify.py, config.HALLOWEEN_*). A covered face
means nothing that night: it's a costume.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from gatekeeper.config import Settings

log = logging.getLogger(__name__)

SPOOKY_KEY = "halloween_sound"  # its phrase key: played like a pre-synthesized line
SPOOKY_RATE = 24000  # Kokoro's rate, like every other clip
SOUNDS = Path(__file__).parent / "sounds"
# The press sounds that ship (HALLOWEEN_PRESS_SOUND). The witch is Kokoro's bf_emma saying
# "Heh heh heh heh heh heh!", made higher and faster with a cackling tremolo and an echo
# (scripts/sounds/halloween_witch.py): Kokoro and its voices are Apache-2.0, so it's ours.
BUILT_IN = {"witch": "halloween-witch.wav"}
MAX_CUSTOM_SECONDS = 10  # the owner's own sound is cut there: it plays before the greeting


def is_halloween(settings: Settings, when: datetime.datetime) -> bool:
    """Is it Halloween evening at ``when`` (local time)?"""
    if settings.halloween != "auto":
        return settings.halloween == "on"
    return (
        when.month == 10
        and when.day == 31
        and settings.halloween_start_hour <= when.hour < settings.halloween_end_hour
    )


def convert(source: Path, dest: Path) -> bool:
    """A sound file of any kind ffmpeg reads, as a mono WAV at Kokoro's rate."""
    from gatekeeper.audio import run_until

    code, stderr, _ = run_until(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-ac", "1",
         "-ar", str(SPOOKY_RATE), "-t", str(MAX_CUSTOM_SECONDS), str(dest)],
        timeout=30,
    )  # fmt: skip
    if code != 0:
        log.warning("couldn't convert %s: %s", source, stderr.decode(errors="replace")[-200:])
    return code == 0 and dest.exists()


def press_sound(
    choice: str, out_dir: Path, convert: Callable[[Path, Path], bool] = convert
) -> Path:
    """The Halloween press sound (``HALLOWEEN_PRESS_SOUND``), written to ``out_dir`` like a
    pre-synthesized line: "witch" (shipped), "theremin" (made here), or a path to the
    owner's own file, converted once (an unknown name or a file that won't convert falls
    back to the witch)."""
    path = out_dir / f"_presynth_{SPOOKY_KEY}.wav"
    if choice == "theremin":
        return spooky_sound(path)
    if choice in BUILT_IN:
        shutil.copyfile(SOUNDS / BUILT_IN[choice], path)
        return path
    source = Path(choice)
    if source.is_file() and convert(source, path):
        log.info("Halloween press sound: %s", source)
        return path
    log.warning(
        "HALLOWEEN_PRESS_SOUND %r isn't a sound file Gatekeeper can play; the witch", choice
    )
    return press_sound("witch", out_dir)


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
