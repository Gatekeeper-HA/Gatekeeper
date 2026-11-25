"""Gatekeeper's models, loaded and run inside an image, through Gatekeeper's own code: what
the test suite can't check, since its tests don't load the models (a dependency update once
broke Whisper only there).

    docker run --rm -v "$PWD/scripts:/scripts:ro" --entrypoint python gatekeeper:local \
        /scripts/image-check.py            # on the CPU (CI: .github/workflows/image.yml)
    ... --gpus all ... gatekeeper:local-cuda /scripts/image-check.py --gpu

It downloads the models it doesn't find (about 1 GB). Kokoro says a sentence and Whisper must
hear it; the knock classifier and the package model must load and run; PyAV must open audio
the way faster-whisper does; aiortc must make an offer.
"""

from __future__ import annotations

import asyncio
import importlib.metadata as md
import logging
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
GPU = "--gpu" in sys.argv[1:]
failed: list[str] = []


def check(what: str, ok: bool, extra: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what} {extra}".rstrip(), flush=True)
    if not ok:
        failed.append(what)


def attempt(what: str, run) -> None:
    """``run()`` returns (ok, extra); an exception fails the check, not the script."""
    start = time.time()
    try:
        ok, extra = run()
    except Exception as e:  # noqa: BLE001 (any failure is the check's)
        ok, extra = False, f"{type(e).__name__}: {e}"
    check(what, ok, f"({time.time() - start:.1f}s) {extra}".rstrip())


print("python", sys.version.split()[0])
for name in ("torch", "ctranslate2", "onnxruntime", "onnxruntime-gpu", "faster-whisper",
             "huggingface-hub", "transformers", "kokoro", "misaki", "spacy", "av", "aiortc",
             "numpy"):  # fmt: skip
    try:
        print(f"  {name} {md.version(name)}")
    except md.PackageNotFoundError:
        pass

wav = Path(tempfile.mkdtemp()) / "hello.wav"


def speak():
    from gatekeeper.audio import Synthesizer

    said = Synthesizer("af_heart").synthesize("Hello, I have a package for you.", wav)
    return said and wav.stat().st_size > 10_000, ""


def hear():
    from gatekeeper.audio import Transcriber

    device, compute = ("cuda", "int8_float16") if GPU else ("cpu", "int8")
    whisper = Transcriber("tiny", compute_type=compute, device=device)
    whisper.load()
    ran_on = getattr(whisper._model.model, "device", "?")
    text = "".join(w.text for w in whisper.transcribe(wav))
    return ran_on == device and "package" in text.lower(), f"on {ran_on}: {text.strip()!r}"


def knock():
    from gatekeeper.knock import Classifier

    p = Classifier(device="cuda" if GPU else "cpu").knock(np.zeros(32000, np.int16))
    return 0 <= p <= 1, f"silence: {p:.3f}"


def packages():
    from gatekeeper.packages import PackageModel

    PackageModel().load()
    return True, ""


def pyav():
    import av

    # As faster-whisper opens audio (faster_whisper/audio.py).
    with av.open(str(wav), mode="r", metadata_errors="ignore") as clip:
        frames = sum(1 for _ in clip.decode(audio=0))
    return frames > 0, f"{frames} frames"


def webrtc():
    from aiortc import RTCPeerConnection

    async def offer() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        sdp = (await pc.createOffer()).sdp
        await pc.close()
        return sdp

    return "m=video" in asyncio.run(offer()), ""


attempt("Kokoro speaks (spaCy, misaki, torch)", speak)
if wav.exists():
    attempt(f"Whisper hears it ({'GPU' if GPU else 'CPU'})", hear)
    attempt("PyAV opens audio as faster-whisper does", pyav)
attempt("the knock classifier (transformers)", knock)
attempt("the package model (CLIP)", packages)
attempt("aiortc makes an offer", webrtc)
print("all passed" if not failed else f"{len(failed)} failed: {', '.join(failed)}")
sys.exit(1 if failed else 0)
