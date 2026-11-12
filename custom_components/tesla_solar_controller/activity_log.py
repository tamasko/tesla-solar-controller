"""Bounded, rotating JSON-lines activity files written outside the HA loop."""

from __future__ import annotations

import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Queue

MAX_LOG_BYTES = 1024 * 1024
LOG_BACKUPS = 3


class ActivityLog:
    """One shared writer per HA instance; reloads append to the existing file."""

    def __init__(self, path, *, max_bytes=MAX_LOG_BYTES, backups=LOG_BACKUPS):
        # Construct and close in HA's executor, never on the event loop.
        self.path = Path(path)
        self.backups = backups
        self._queue = Queue()
        self._handler = RotatingFileHandler(
            self.path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._queue_handler = QueueHandler(self._queue)
        self._listener = QueueListener(self._queue, self._handler)
        self._closed = False
        self._listener.start()

    def record(self, payload):
        """Enqueue an immutable snapshot without filesystem IO."""
        if self._closed:
            return
        message = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        self._queue_handler.handle(
            logging.LogRecord(__name__, logging.INFO, "", 0, message, (), None)
        )

    def read(self):
        """Read a consistent oldest-to-newest snapshot, including rotated files."""
        self._queue.join()
        self._handler.acquire()
        try:
            if self._handler.stream:
                self._handler.flush()
            paths = [
                Path(f"{self.path}.{number}")
                for number in range(self.backups, 0, -1)
            ] + [self.path]
            return "".join(
                path.read_text(encoding="utf-8") for path in paths if path.exists()
            )
        finally:
            self._handler.release()

    def close(self):
        """Drain pending records and close the file on HA shutdown."""
        if not self._closed:
            self._closed = True
            self._listener.stop()
            self._queue_handler.close()
            self._handler.close()
