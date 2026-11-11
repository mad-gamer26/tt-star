"""The bot's log: printed (startman keeps stdout) and kept in memory for the
web interface's Log page. Each entry names the server it is about, or None
for the bot itself (STAR connections, the web interface, the process)."""

from __future__ import annotations

import collections
import itertools
import sys
import threading
import time

LEVELS = {"INFO": 20, "WARNING": 30, "ERROR": 40}
KEEP = 3000


class LogBuffer:
    def __init__(self, keep: int = KEEP):
        self._records: collections.deque[dict] = collections.deque(maxlen=keep)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def add(self, message: str, server: str | None, level: str) -> None:
        with self._lock:
            self._records.append(
                {
                    "id": next(self._ids),
                    "time": time.time(),
                    "server": server,
                    "level": level,
                    "message": message,
                }
            )

    def records(
        self,
        after: int = 0,
        server: str | None = None,
        bot_only: bool = False,
        min_level: str = "INFO",
        limit: int = 300,
    ) -> list[dict]:
        minimum = LEVELS.get(min_level, 20)
        with self._lock:
            found = [
                dict(record)
                for record in self._records
                if record["id"] > after
                and LEVELS.get(record["level"], 20) >= minimum
                and (not bot_only or record["server"] is None)
                and (server is None or record["server"] == server)
            ]
        return found[-limit:]


buffer = LogBuffer()


def log(message: str, server: str | None = None, level: str = "INFO") -> None:
    """Prints a line (with the server it is about) and keeps it for the Log page."""
    prefix = f"[{server}] " if server else ""
    stream = sys.stderr if level == "ERROR" else sys.stdout
    print(f"{prefix}{message}", file=stream, flush=True)
    buffer.add(message, server, level)
