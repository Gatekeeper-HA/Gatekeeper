from __future__ import annotations

import wave
from pathlib import Path

import pytest

from gatekeeper.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def write_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(audio_dir=tmp_path / "audio", log_dir=tmp_path / "logs")
    for d in (s.in_dir, s.out_dir, s.log_dir):
        d.mkdir(parents=True)
    return s


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
