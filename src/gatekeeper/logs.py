"""Logging setup: text or JSON lines, with the visit's event id on every record."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

# The Frigate event id of the visit being handled, or "-" outside a visit.
# Set at the start of each visit worker; asyncio tasks and asyncio.to_thread
# inherit it, plain threading.Thread does not.
event_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("event_id", default="-")

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(event_id)s] %(message)s"


class EventIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.event_id = event_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event_id": getattr(record, "event_id", "-"),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(EventIdFilter())
    handler.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter(TEXT_FORMAT))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
