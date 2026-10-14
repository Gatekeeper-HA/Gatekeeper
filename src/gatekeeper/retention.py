"""Deleting old visitor audio and visit logs.

Visitor recordings and transcripts are third parties' voice data; keep them
only as long as configured (0 = keep forever).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from gatekeeper.config import Settings

log = logging.getLogger(__name__)

DAY = 86400.0


def purge(paths: Iterable[Path], max_age_days: float, now: float) -> int:
    """Delete files last modified more than ``max_age_days`` ago; return the count."""
    if max_age_days <= 0:
        return 0
    cutoff = now - max_age_days * DAY
    removed = 0
    for path in paths:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as e:
            log.warning("could not remove %s: %s", path, e)
    return removed


class Retention:
    def __init__(self, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._clock = clock

    def run_once(self) -> dict[str, int]:
        s = self.settings
        now = self._clock()
        log_file = s.event_log_file
        removed = {
            "audio_in": purge(s.in_dir.glob("*.wav"), s.audio_retention_days, now),
            # Photos kept during conversations (a warrant held up to the camera).
            "snapshots": purge(s.snapshot_dir.glob("*.jpg"), s.audio_retention_days, now),
            # Per-visit synthesized speech; the pre-synthesized phrases stay.
            "audio_out": purge(
                (p for p in s.out_dir.glob("*.wav") if not p.name.startswith("_presynth_")),
                s.audio_retention_days,
                now,
            ),
            "event_logs": purge(
                (
                    p
                    for log in (log_file, s.notification_log_file)
                    for p in log.parent.glob(f"{log.stem}-*{log.suffix}")
                ),
                s.event_log_retention_days,
                now,
            ),
        }
        if any(removed.values()):
            log.info("retention: removed %s", removed)
        return removed

    def run_forever(self, interval: float = 3600.0) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("retention error")
            time.sleep(interval)
