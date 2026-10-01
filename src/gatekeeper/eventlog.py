"""The visit log, one JSON object per line (``events.jsonl``).

The current day's visits go to ``events.jsonl``; on the first write of a new
day the previous file is renamed ``events-YYYY-MM-DD.jsonl`` (its last write's
local date). ``retention`` deletes old daily files.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path


class EventLog:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self._rotate_if_new_day()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _rotate_if_new_day(self) -> None:
        if not self.path.exists():
            return
        last_write = date.fromtimestamp(self.path.stat().st_mtime)
        if last_write >= date.fromtimestamp(self._clock()):
            return
        target = self.path.with_name(f"{self.path.stem}-{last_write.isoformat()}{self.path.suffix}")
        n = 1
        while target.exists():
            target = self.path.with_name(
                f"{self.path.stem}-{last_write.isoformat()}.{n}{self.path.suffix}"
            )
            n += 1
        self.path.rename(target)
