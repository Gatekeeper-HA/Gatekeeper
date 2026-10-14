"""Replay recorded visit clips through VAD endpointing and compare with the fixed window.

Each clip in audio/in was recorded from the moment a prompt (the greeting, or the
no-answer reply for "-2" clips) started playing, for a fixed window. For each clip this:
- finds the prompt's echo and the visitor's answer with Whisper (as a visit does);
- runs StreamingVAD + Endpointer as the persistent tap would, with the echo expected to
  end at the prompt's duration + TALKBACK_LATENCY_SECONDS;
- re-transcribes only up to the endpoint and checks that no answer words are lost.

Run it in the gatekeeper image with this checkout's code:
  docker run --rm -e PYTHONPATH=/repo/src -v <repo>:/repo:ro -v <data>:/data:ro \
    gatekeeper:local python /repo/scripts/replay/endpointing.py /data/audio/in /data/audio/out

audio/out holds the prompts' _presynth_*.wav. The clips are household recordings: keep
them out of the repository.
"""

import re
import statistics
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

from gatekeeper.audio import Transcriber, wav_duration
from gatekeeper.config import Settings
from gatekeeper.interaction import ECHO_MARGIN_SECONDS, TALKBACK_LATENCY_SECONDS
from gatekeeper.listen import join_words, strip_greeting
from gatekeeper.vad import CHUNK_SECONDS, Endpointer, StreamingVAD


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def save(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio.tobytes())


def norm(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def frac_above(probs, t0, t1, thr):
    sel = [p for i, p in enumerate(probs) if t0 <= (i + 0.5) * CHUNK_SECONDS <= t1]
    return (sum(p >= thr for p in sel) / len(sel)) if sel else None


def main(
    clip_dir: str,
    prompt_dir: str,
    model: str = "base.en",
    threshold: float | None = None,
    end_silence: float | None = None,
) -> None:
    s = Settings()
    threshold = s.vad_threshold if threshold is None else threshold
    end_silence = s.end_silence_ms / 1000 if end_silence is None else end_silence
    prompts = {
        1: (s.greeting, wav_duration(Path(prompt_dir) / "_presynth_greeting.wav")),
        2: (s.reply_no_answer, wav_duration(Path(prompt_dir) / "_presynth_no_answer.wav")),
    }
    stt = Transcriber(model, "int8")
    rows = []
    tmp = Path(tempfile.mkdtemp())
    for clip in sorted(Path(clip_dir).glob("*.wav")):
        turn = 2 if clip.stem.endswith("-2") else 1
        prompt, prompt_seconds = prompts[turn]
        audio = load(clip)
        clip_seconds = len(audio) / 16000
        probs = StreamingVAD().feed(audio)

        words = stt.transcribe(clip)
        answer, method = strip_greeting(words, prompt)
        echo = [w for w in words if w not in answer]
        echo_start = echo[0].start if echo else None
        echo_end = echo[-1].end if echo else None

        expected_echo_end = prompt_seconds + TALKBACK_LATENCY_SECONDS
        ep = Endpointer(
            listen_from=expected_echo_end - ECHO_MARGIN_SECONDS,
            no_input_by=expected_echo_end + s.listen_seconds,
            threshold=threshold,
            end_silence=end_silence,
        )
        turn_end = None
        for i, p in enumerate(probs):
            turn_end = ep.update((i + 1) * CHUNK_SECONDS, p)
            if turn_end:
                break
        reason = turn_end.reason if turn_end else "clip_end"
        at = turn_end.at if turn_end else clip_seconds

        cut = tmp / clip.name
        save(cut, audio[: int(at * 16000)])
        answer_cut, _ = strip_greeting(stt.transcribe(cut), prompt)
        full, kept = norm(join_words(answer)), norm(join_words(answer_cut))
        lost = [w for w in full if w not in kept]

        rows.append(
            {
                "clip": clip.name,
                "answer": join_words(answer),
                "method": method,
                "reason": reason,
                "at": at,
                "clip_seconds": clip_seconds,
                "last_word": answer[-1].end if answer else None,
                "speech_start": turn_end.speech_start if turn_end else None,
                "lost": lost,
                "echo_vad": frac_above(probs, echo_start, echo_end, threshold) if echo else None,
                "answer_vad": frac_above(probs, answer[0].start, answer[-1].end, threshold)
                if answer
                else None,
                "echo_end_error": (echo_end - expected_echo_end) if echo_end else None,
            }
        )
        last = "-" if answer == [] else f"{answer[-1].end:.1f}"
        print(
            f"{clip.name:<40} {reason:<9} end {at:5.1f}/{clip_seconds:4.1f} s"
            f"  last word {last:>5}  lost {len(lost)}  heard {join_words(answer)!r}",
            flush=True,
        )

    answered = [r for r in rows if r["answer"]]
    silent = [r for r in rows if not r["answer"]]
    print(f"\n{len(rows)} clips: {len(answered)} with an answer, {len(silent)} without")

    def med(xs):
        xs = [x for x in xs if x is not None]
        if not xs:
            return "n=0"
        return (
            f"median {statistics.median(xs):.2f}, "
            f"min {min(xs):.2f}, max {max(xs):.2f} (n={len(xs)})"
        )

    print("With an answer:")
    print(
        "  endpoint reasons:",
        {
            k: sum(r["reason"] == k for r in answered)
            for k in ("answered", "no_input", "max", "clip_end")
        },
    )
    print("  endpoint - last answer word end:", med([r["at"] - r["last_word"] for r in answered]))
    print(
        "  old window end - endpoint (time saved):",
        med([r["clip_seconds"] - r["at"] for r in answered]),
    )
    print(
        "  clips that lost answer words:", [(r["clip"], r["lost"]) for r in answered if r["lost"]]
    )
    print("  answer chunks with VAD speech:", med([r["answer_vad"] for r in answered]))
    print("Without an answer:")
    print(
        "  endpoint reasons:",
        {
            k: sum(r["reason"] == k for r in silent)
            for k in ("answered", "no_input", "max", "clip_end")
        },
    )
    print(
        "  old window end - endpoint (time saved):",
        med([r["clip_seconds"] - r["at"] for r in silent]),
    )
    print("Echo:")
    print("  echo chunks with VAD speech:", med([r["echo_vad"] for r in rows]))
    print("  heard echo end - expected echo end:", med([r["echo_end_error"] for r in rows]))


if __name__ == "__main__":
    main(*sys.argv[1:3], **({"model": sys.argv[3]} if len(sys.argv) > 3 else {}))
