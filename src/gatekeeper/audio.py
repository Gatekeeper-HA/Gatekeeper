"""Audio capture (ffmpeg), speech-to-text (faster-whisper) and text-to-speech (Kokoro).

The speech libraries are imported lazily so the rest of the package works
without them (install the ``speech`` extra for the real thing).
"""

from __future__ import annotations

import logging
import subprocess
import wave
from pathlib import Path

from gatekeeper.listen import Word

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

    def load(self) -> None:
        """Load the model now, so the first visit doesn't pay for it."""
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("loading Whisper model: %s (%s)", self.model_name, self.compute_type)
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type=self.compute_type
            )

    def transcribe(self, wav_path: Path) -> list[Word]:
        """Return the words spoken in ``wav_path`` with their times in the clip.

        Whisper's VAD filter is off: it drops the greeting's echo from the
        doorbell speaker (TTS through a small speaker isn't classed as
        speech), and the echo is needed to find where the answer starts.
        Whisper's no-speech/log-prob thresholds still suppress text on silence.
        """
        self.load()
        try:
            segments, _info = self._model.transcribe(
                str(wav_path),
                beam_size=1,
                language="en",
                vad_filter=False,
                condition_on_previous_text=False,
                word_timestamps=True,
            )
            return [
                Word(w.word, w.start, w.end) for seg in segments for w in (seg.words or [])
            ]
        except Exception:
            log.exception("transcription error")
            return []


def kokoro_lang_code(voice: str) -> str:
    """Kokoro's language code is the voice's first letter: af_heart -> "a"
    (American English), bm_george -> "b" (British English)."""
    return voice[:1].lower() or "a"


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
                lang = kokoro_lang_code(self.voice)
                log.info("loading Kokoro pipeline (voice=%s, lang=%s)", self.voice, lang)
                self._pipeline = KPipeline(lang_code=lang)

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
