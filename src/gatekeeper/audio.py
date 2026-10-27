"""Audio capture (ffmpeg), speech-to-text (faster-whisper) and text-to-speech (Kokoro).

The speech libraries are imported lazily so the rest of the package works
without them (install the ``speech`` extra for the real thing).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path

from gatekeeper.listen import Word

log = logging.getLogger(__name__)


# ffmpeg's RTSP socket timeout. Without it, a stream that connects but never
# sends data hangs ffmpeg forever (-rw_timeout does not apply to RTSP in 5.1).
RTSP_IO_TIMEOUT_US = 5_000_000
# Backstop: stop ffmpeg if it runs this much longer than the clip.
CAPTURE_GRACE_SECONDS = 10
# After asking a process to stop, kill it if it hasn't within this long.
STOP_GRACE_SECONDS = 3


def run_until(args: list[str], timeout: float) -> tuple[int, bytes, bool]:
    """Run ``args``; after ``timeout`` seconds ask it to stop (SIGTERM, on which
    ffmpeg finishes writing its output file), and kill it if it hasn't stopped
    STOP_GRACE_SECONDS later. Returns (returncode, stderr, timed_out)."""
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        _, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stderr, False
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
        return proc.returncode, stderr, True


def capture_audio_clip(rtsp_url: str, wav_path: Path, seconds: int) -> Path | None:
    """Record ``seconds`` of 16 kHz mono audio from ``rtsp_url`` into ``wav_path``."""
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-timeout", str(RTSP_IO_TIMEOUT_US),
        "-i", rtsp_url,
        "-vn",
        "-map", "0:a:0?",
        "-ac", "1",
        "-ar", "16000",
        "-t", str(seconds),
        "-y",
        str(wav_path),
    ]  # fmt: skip
    limit = seconds + CAPTURE_GRACE_SECONDS
    returncode, stderr, timed_out = run_until(cmd, limit)
    if timed_out:
        # Slow to connect or to start (seen on an overloaded VM): what it did
        # record may still hold the visitor's answer.
        log.warning("audio capture stopped after %d s; keeping what it recorded", limit)
    elif returncode != 0:
        log.warning("audio capture failed: %s", stderr.decode("utf-8", errors="ignore"))
        return None

    try:
        if wav_path.stat().st_size < 1024:
            log.warning("audio capture too small or empty")
            return None
    except OSError:
        log.warning("audio capture produced no file")
        return None

    return wav_path


def wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / float(rate) if rate else 0.0
    except Exception:
        return 3.0


def cuda_devices() -> int:
    """How many NVIDIA GPUs CTranslate2 (Whisper's engine) can use."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def whisper_device(device: str, gpus: int) -> str:
    """"auto" means the GPU when there is one."""
    if device == "auto":
        return "cuda" if gpus > 0 else "cpu"
    return device


def whisper_compute_type(compute_type: str, device: str) -> str:
    """"auto" means int8 on the CPU and int8_float16 on a GPU: 2-4x faster than float16
    on a GTX 1660 Ti (no tensor cores) with the same transcripts, and fine on any NVIDIA
    GPU since Pascal."""
    if compute_type == "auto":
        return "int8_float16" if device == "cuda" else "int8"
    return compute_type


def _silent_wav(seconds: float = 1.0):
    """A WAV of silence in memory, to run a model once."""
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    buf.seek(0)
    return buf


class Transcriber:
    def __init__(
        self,
        model_name: str,
        compute_type: str = "auto",
        device: str = "auto",
        hotwords: str = "",
        gpus: Callable[[], int] = cuda_devices,
    ) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.device = device
        # Words and names to expect (e.g. local candidates'); passed to Whisper as hotwords.
        self.hotwords = hotwords.strip() or None
        self._gpus = gpus
        self._model = None

    def load(self) -> None:
        """Load the model now, so the first visit doesn't pay for it. On a GPU, also
        run it once: missing CUDA libraries only show up then. If the GPU doesn't
        work, Whisper runs on the CPU instead."""
        if self._model is not None:
            return
        from faster_whisper import WhisperModel

        device = whisper_device(self.device, self._gpus() if self.device == "auto" else 0)
        compute_type = whisper_compute_type(self.compute_type, device)
        log.info("loading Whisper model: %s on %s (%s)", self.model_name, device, compute_type)
        try:
            model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
            if device == "cuda":
                list(model.transcribe(_silent_wav(), language="en")[0])
        except Exception:
            if device != "cuda":
                raise
            log.exception("Whisper couldn't run on the GPU; using the CPU")
            compute_type = whisper_compute_type(self.compute_type, "cpu")
            model = WhisperModel(self.model_name, device="cpu", compute_type=compute_type)
        self._model = model

    def transcribe(self, wav_path: Path) -> list[Word]:
        """Return the words spoken in ``wav_path`` with their times in the clip.

        Whisper's VAD filter is off: it drops the greeting's echo from the
        doorbell speaker (TTS through a small speaker isn't classed as
        speech), and the echo is needed to find where the answer starts.
        Whisper's no-speech/log-prob thresholds still suppress text on silence.
        """
        self.load()
        started = time.monotonic()
        try:
            segments, info = self._model.transcribe(
                str(wav_path),
                beam_size=1,
                language="en",
                vad_filter=False,
                condition_on_previous_text=False,
                word_timestamps=True,
                hotwords=self.hotwords,
                # No re-decoding at higher temperatures when the output looks like
                # garbage: on noise it took up to 7 s and only changed the garbage.
                temperature=0.0,
            )
            # transcribe() reads the audio and computes its features before it
            # returns; the model (encoder, decoder, word timing) runs as the
            # segments are read.
            prepared = time.monotonic()
            words = [
                Word(w.word, w.start, w.end) for seg in segments for w in (seg.words or [])
            ]
            done = time.monotonic()
            # The daily report reads this line (gatekeeper.report).
            log.info(
                "transcribed %.1f s of audio in %.2f s (features %.2f s, model %.2f s; load %.1f)",
                getattr(info, "duration", 0.0),
                done - started,
                prepared - started,
                done - prepared,
                load_average(),
            )
            return words
        except Exception:
            log.exception("transcription error")
            return []


def load_average() -> float:
    """The host's 1-minute load average (how busy the CPUs are), or -1 where
    there's none (Windows)."""
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return -1.0


def kokoro_lang_code(voice: str) -> str:
    """Kokoro's language code is the voice's first letter: af_heart -> "a"
    (American English), bm_george -> "b" (British English)."""
    return voice[:1].lower() or "a"


class Synthesizer:
    def __init__(self, voice: str) -> None:
        self.voice = voice
        self._pipeline = None
        self._lock = threading.Lock()  # one at a time: phrases also synthesize in the background

    def synthesize(self, text: str, wav_path: Path) -> bool:
        with self._lock:
            return self._synthesize(text, wav_path)

    def _synthesize(self, text: str, wav_path: Path) -> bool:
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
    """Pre-synthesize fixed phrases at startup so playback is instant.

    A phrase whose text and voice haven't changed since it was last synthesized
    is reused (the ``.txt`` beside each WAV records what it says): each takes
    ~10 s on a 4-core CPU.
    """
    done: dict[str, Path] = {}
    for key, text in texts.items():
        path = out_dir / f"_presynth_{key}.wav"
        stamp = path.with_suffix(".txt")
        signature = f"{synth.voice}\n{text}"
        try:
            cached = path.exists() and stamp.read_text(encoding="utf-8") == signature
        except OSError:
            cached = False
        if cached:
            done[key] = path
            log.info("presynth %s: cached", key)
        elif synth.synthesize(text, path):
            stamp.write_text(signature, encoding="utf-8")
            done[key] = path
            log.info("presynth %s: ok", key)
        else:
            log.error("presynth FAILED: %s", key)
    return done
