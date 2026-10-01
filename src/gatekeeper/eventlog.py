"""The visit log, one JSON object per line (``events.jsonl``)."""

from __future__ import annotations

import json
from pathlib import Path


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
