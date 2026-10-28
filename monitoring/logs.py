"""
The server's latest log lines, in memory, for the /admin "Activity & logs" page.

A ``logging.Handler`` that keeps the newest ``capacity`` records in a ring.
Nothing is written to disk: the buffer is this process's, and starts empty on
each restart. The dashboard polls it with ``after_id`` to get only new lines.
"""

import logging
import threading
from collections import deque
from typing import Any, Dict, List, Optional

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class LogBuffer(logging.Handler):
    """Keeps the last ``capacity`` log records, each with an increasing id."""

    def __init__(self, capacity: int = 2000, level: int = logging.DEBUG):
        super().__init__(level)
        self.capacity = capacity
        self._lines: "deque[Dict[str, Any]]" = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._next_id = 1
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - a broken record must not take the server down
            message = record.getMessage()
        with self._lock:
            self._lines.append({
                "id": self._next_id,
                "ts": record.created,
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message,
            })
            self._next_id += 1

    def install(self, *logger_names: Optional[str]) -> "LogBuffer":
        """Attach to the named loggers (``None`` is the root), once each."""
        for name in logger_names:
            target = logging.getLogger(name)
            if self not in target.handlers:
                target.addHandler(self)
        return self

    def lines(self, after_id: int = 0, limit: int = 500, level: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lines newer than ``after_id``, oldest first, at ``level`` or above, the newest ``limit`` of them."""
        floor = logging.getLevelName(level.upper()) if level else logging.NOTSET
        if not isinstance(floor, int):
            floor = logging.NOTSET
        with self._lock:
            lines = [l for l in self._lines if l["id"] > after_id and l["levelno"] >= floor]
        return lines[-limit:]

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)
