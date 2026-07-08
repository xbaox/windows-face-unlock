"""Structured audit trail for face-auth attempts (Stage 2, Step 7).

One JSON object per line (JSONL) appended to a file in APP_DIR: verify / unlock / challenge
attempts with the verdict, distance, liveness signals, mode and latency. Never contains the
password -- only the outcome. Size-based rotation keeps ``audit.jsonl`` plus a few numbered
backups so the file can't grow without bound. Thread-safe; clock injectable for tests.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class AuditLog:
    def __init__(self, path: Path, max_mb: float = 5.0, backups: int = 2,
                 enabled: bool = True, clock=time.time) -> None:
        self.path = Path(path)
        self.max_bytes = int(max(0.0, float(max_mb)) * 1_000_000)
        self.backups = max(0, int(backups))
        self.enabled = bool(enabled)
        self._clock = clock
        self._lock = threading.Lock()

    def _backup_name(self, i: int) -> Path:
        return self.path.with_name(self.path.name + f".{i}")

    def _rotate(self) -> None:
        oldest = self._backup_name(self.backups)
        if self.backups == 0:
            try:
                self.path.unlink()
            except OSError:
                pass
            return
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(self.backups - 1, 0, -1):
            src, dst = self._backup_name(i), self._backup_name(i + 1)
            if src.exists():
                try:
                    os.replace(src, dst)
                except OSError:
                    pass
        if self.path.exists():
            try:
                os.replace(self.path, self._backup_name(1))
            except OSError:
                pass

    def write(self, event: str, record: dict) -> None:
        """Append one attempt record. Adds ts/epoch/event; rotates first if needed."""
        if not self.enabled:
            return
        ts = datetime.datetime.fromtimestamp(
            self._clock(), datetime.timezone.utc
        ).isoformat(timespec="milliseconds")
        line = json.dumps(
            {"ts": ts, "epoch": round(self._clock(), 3), "event": event, **record},
            ensure_ascii=False,
        ) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if (self.max_bytes > 0 and self.path.exists()
                        and self.path.stat().st_size + len(data) > self.max_bytes):
                    self._rotate()
                with open(self.path, "ab") as f:
                    f.write(data)
            except OSError as e:
                log.warning("audit write failed: %s", e)

    def reconfigure(self, enabled: bool, max_mb: float) -> None:
        with self._lock:
            self.enabled = bool(enabled)
            self.max_bytes = int(max(0.0, float(max_mb)) * 1_000_000)

    def status(self) -> dict:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"enabled": self.enabled, "path": str(self.path), "size": size}
