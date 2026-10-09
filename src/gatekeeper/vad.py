"""Voice activity detection, and deciding when a visitor has finished answering.

StreamingVAD runs Silero VAD (the ONNX model bundled with faster-whisper) on
16 kHz audio as it arrives, 32 ms at a time, keeping the model's state between
chunks. Endpointer turns those speech probabilities into the end of a turn:
the visitor answered and then fell silent, never started, or talked too long.

A prompt's own echo (our speech through the doorbell speaker, picked up by its
mic) is excluded by time: speech only counts from ``listen_from``, just before
the echo is expected to end. Silero also rarely classes that echo as speech.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512  # 32 ms: the window Silero expects at 16 kHz
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE
CONTEXT_SAMPLES = 64


class StreamingVAD:
    """Speech probability for each 512-sample chunk of a 16 kHz stream."""

    def __init__(self, session=None) -> None:
        if session is None:
            # faster-whisper ships Silero VAD v6 and runs it with onnxruntime.
            from faster_whisper.vad import get_vad_model

            session = get_vad_model().session
        self._session = session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> list[float]:
        """Add samples (int16, or float in [-1, 1]); return the probability of each
        chunk completed by them. Leftover samples wait for the next call."""
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        buf = np.concatenate([self._pending, samples.astype(np.float32, copy=False)])
        n = len(buf) // CHUNK_SAMPLES
        probs = []
        for i in range(n):
            chunk = buf[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES]
            model_input = np.concatenate([self._context, chunk])[np.newaxis, :]
            out, self._h, self._c = self._session.run(
                None, {"input": model_input, "h": self._h, "c": self._c}
            )
            probs.append(float(np.asarray(out).reshape(-1)[0]))
            self._context = chunk[-CONTEXT_SAMPLES:]
        self._pending = buf[n * CHUNK_SAMPLES :]
        return probs


@dataclass(frozen=True)
class TurnEnd:
    """Why and when a turn ended. Times are on the clock the probabilities were fed on.

    reason: "answered" (spoke, then fell silent), "no_input" (didn't start in
    time) or "max" (still talking at the limit).
    """

    reason: str
    at: float
    speech_start: float | None = None
    speech_end: float | None = None


class Endpointer:
    """Feed it each chunk's (end time, speech probability) in order; it returns a
    TurnEnd once the visitor's turn is over, and None until then.

    - Speech is tracked with hysteresis: it starts at ``threshold`` and only ends
      below ``neg_threshold`` (faster-whisper's defaults), so a soft syllable
      doesn't split an answer.
    - Only speech from ``listen_from`` on counts as the visitor's. Speech already
      under way then (talking over the prompt's end) counts from ``listen_from``.
    - Blips shorter than ``min_speech`` in total (a cough, a car door) don't make
      an answer.
    """

    def __init__(
        self,
        *,
        listen_from: float,
        no_input_by: float,
        threshold: float = 0.5,
        neg_threshold: float | None = None,
        end_silence: float = 0.7,
        min_speech: float = 0.25,
        max_answer: float = 15.0,
    ) -> None:
        self.listen_from = listen_from
        self.no_input_by = no_input_by
        self.threshold = threshold
        self.neg_threshold = max(threshold - 0.15, 0.01) if neg_threshold is None else neg_threshold
        self.end_silence = end_silence
        self.min_speech = min_speech
        self.max_answer = max_answer
        self._in_speech = False
        self._speech_total = 0.0
        self._speech_start: float | None = None
        self._speech_end: float | None = None

    @property
    def heard_speech(self) -> bool:
        """Has the visitor said enough (``min_speech``) since ``listen_from``?"""
        return self._speech_total >= self.min_speech

    def update(self, t: float, prob: float) -> TurnEnd | None:
        """``t`` is the end time of the chunk whose speech probability is ``prob``."""
        start = t - CHUNK_SECONDS
        if self._in_speech:
            if prob < self.neg_threshold:
                self._in_speech = False
        elif prob >= self.threshold:
            self._in_speech = True

        if t <= self.listen_from:
            return None

        if self._in_speech:
            counted_from = max(start, self.listen_from)
            self._speech_total += t - counted_from
            if self._speech_start is None:
                self._speech_start = counted_from
            self._speech_end = t
            if t - self._speech_start >= self.max_answer:
                return self._end("max", t)
            return None

        answered = self._speech_total >= self.min_speech
        if answered and t - self._speech_end >= self.end_silence:
            return self._end("answered", t)
        if not answered and t >= self.no_input_by:
            return self._end("no_input", t)
        if t >= self.no_input_by + self.max_answer:
            return self._end("max", t)
        return None

    def _end(self, reason: str, t: float) -> TurnEnd:
        answered = self._speech_total >= self.min_speech
        return TurnEnd(
            reason,
            t,
            self._speech_start if answered else None,
            self._speech_end if answered else None,
        )


def speech_seconds(
    path, start: float, end: float, threshold: float = 0.5, vad: StreamingVAD | None = None
) -> float:
    """Seconds of speech Silero hears between ``start`` and ``end`` of a 16 kHz
    mono WAV. Whisper can "hear" words in noise or silence ("Thanks for
    watching!"); Silero rarely does, so this confirms an answer was spoken."""
    import wave

    with wave.open(str(path), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    probs = (vad or StreamingVAD()).feed(samples)
    first, last = max(0, int(start / CHUNK_SECONDS)), int(end / CHUNK_SECONDS) + 1
    return sum(p >= threshold for p in probs[first:last]) * CHUNK_SECONDS
