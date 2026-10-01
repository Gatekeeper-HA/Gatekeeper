"""Audio capture (ffmpeg), speech-to-text (faster-whisper) and text-to-speech (Kokoro).

The speech libraries are imported lazily so the rest of the package works
without them (install the ``speech`` extra for the real thing).
"""

from __future__ import annotations

import logging
import subprocess
import wave
from pathlib import Path

log = logging.getLogger(__name__)


def run_cmd(args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_bytes,
        capture_output=True,
        check=False,
    )


def capture_audio_clip(rtsp_url: str, wav_path: Path, seconds: int) -> Path | None:
    """Record ``seconds`` of 16 kHz mono audio from ``rtsp_url`` into ``wav_path``."""
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vn",
        "-map", "0:a:0?",
        "-ac", "1",
        "-ar", "16000",
        "-t", str(seconds),
        "-y",
        str(wav_path),
    ]  # fmt: skip
    proc = run_cmd(cmd)
    if proc.returncode != 0 or not wav_path.exists():
        log.warning("audio capture failed: %s", proc.stderr.decode("utf-8", errors="ignore"))
        return None

    try:
        if wav_path.stat().st_size < 1024:
            log.warning("audio capture too small or empty")
            return None
    except OSError:
        return None

    return wav_path


def wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / float(rate) if rate else 0.0
    except Exception:
        return 3.0


class Transcriber:
    def __init__(self, model_name: str, compute_type: str) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self._model = None

    def transcribe(self, wav_path: Path) -> str:
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading Whisper model: %s (%s)", self.model_name, self.compute_type)
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type
            )

        try:
            segments, _info = self._model.transcribe(
                str(wav_path),
                beam_size=1,
                language="en",
                vad_filter=True,
                condition_on_previous_text=False,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception:
            log.exception("transcription error")
            return ""


class Synthesizer:
    def __init__(self, voice: str) -> None:
        self.voice = voice
        self._pipeline = None

    def synthesize(self, text: str, wav_path: Path) -> bool:
        try:
            import numpy as np
            import soundfile as sf
            from kokoro import KPipeline
        except ImportError:
            log.error("kokoro not available")
            return False

        try:
            if self._pipeline is None:
                log.info("loading Kokoro pipeline (voice=%s)", self.voice)
                self._pipeline = KPipeline(lang_code="a")

            chunks = [audio for _, _, audio in self._pipeline(text, voice=self.voice, speed=1.0)]
            if not chunks:
                log.error("no audio generated")
                return False

            sf.write(str(wav_path), np.concatenate(chunks), 24000, subtype="PCM_16")
            return True
        except Exception:
            log.exception("synthesis failed")
            return False


def presynth_all(synth: Synthesizer, out_dir: Path, texts: dict[str, str]) -> dict[str, Path]:
    """Pre-synthesize fixed phrases at startup so playback is instant."""
    done: dict[str, Path] = {}
    for key, text in texts.items():
        path = out_dir / f"_presynth_{key}.wav"
        if synth.synthesize(text, path):
            done[key] = path
            log.info("presynth %s: ok", key)
        else:
            log.error("presynth FAILED: %s", key)
    return done
