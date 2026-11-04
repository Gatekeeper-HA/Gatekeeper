"""Make src/gatekeeper/sounds/halloween-witch.wav, the Halloween press sound's witch: Kokoro's
bf_emma saying "Heh heh heh heh heh heh!", made higher and faster (resampled, like a tape
sped up), with a cackling tremolo and a short echo. Kokoro and its voices are Apache-2.0.

Run it in the Gatekeeper image (it has Kokoro and its model cache):

  docker run --rm -v "$PWD:/src" -v /docker/gatekeeper/cache:/root/.cache \\
    --entrypoint python gatekeeper:local /src/scripts/sounds/halloween_witch.py \\
    /src/src/gatekeeper/sounds/halloween-witch.wav
"""

import sys

import numpy as np
import soundfile as sf
from kokoro import KPipeline

RATE = 24000


def main(out: str) -> None:
    pipeline = KPipeline(lang_code="b")
    voice = [a for _, _, a in pipeline("Heh heh heh heh heh heh!", voice="bf_emma", speed=1.1)]
    x = np.concatenate([np.asarray(c, dtype=np.float32) for c in voice])
    x = np.interp(np.arange(0, len(x) - 1, 1.4), np.arange(len(x)), x)  # higher, faster
    t = np.arange(len(x)) / RATE
    x = x * (0.6 + 0.4 * (0.5 + 0.5 * np.sin(2 * np.pi * 11 * t)))  # the cackle
    out_x = np.concatenate([x, np.zeros(int(0.21 * RATE))])
    for seconds, gain in ((0.09, 0.3), (0.21, 0.15)):  # a short echo
        d = int(seconds * RATE)
        out_x[d : d + len(x)] += gain * x
    fade = int(0.15 * RATE)
    out_x[-fade:] *= np.linspace(1, 0, fade)
    sf.write(out, 0.7 * out_x / np.max(np.abs(out_x)), RATE, subtype="PCM_16")


if __name__ == "__main__":
    main(sys.argv[1])
