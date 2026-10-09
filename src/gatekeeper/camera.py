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
