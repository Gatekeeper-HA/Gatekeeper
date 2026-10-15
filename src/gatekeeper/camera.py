"""Full-resolution photos from the camera, via go2rtc.

Frigate's snapshots come from its detect stream (640x480 on the rig), too
small to read a document held up to the doorbell. go2rtc can grab a frame
from the camera's main stream (2560x1920 on the Reolink) instead.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def go2rtc_frame(api: str, stream: str, path: Path, timeout: float = 10.0) -> Path | None:
    """Save a JPEG of go2rtc stream ``stream`` to ``path``; None if it fails."""
    url = f"{api.rstrip('/')}/api/frame.jpeg?src={urllib.parse.quote(stream)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
    except Exception as e:
        log.warning("couldn't grab a frame from go2rtc: %s", e)
        return None
    if not data:
        return None
    path.write_bytes(data)
    return path


def sharpness(gray) -> float:
    """How sharp a grayscale image (a numpy array) is: the variance of its Laplacian
    over the middle of the frame, where a document held up to the doorbell is, and
    away from the edges (on the rig, the siding's sharp stripes). Higher is sharper:
    in focus, and not blurred by a moving hand."""
    import numpy as np

    h, w = gray.shape
    g = gray[h // 5 : h - h // 5, w // 5 : w - w // 5].astype(np.float32)
    lap = g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def photo_sharpness(path: Path) -> float | None:
    """The sharpness of the JPEG at ``path``, or None if it can't be read (PyAV, which
    comes with aiortc and faster-whisper, decodes it)."""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            frame = next(container.decode(video=0))
            return sharpness(frame.to_ndarray(format="gray"))
    except Exception as e:
        log.warning("couldn't read photo %s: %s", path.name, e)
        return None
