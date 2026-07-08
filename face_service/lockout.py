"""Face-auth lockout after repeated failures (Stage 2, Step 6).

After ``max_attempts`` consecutive failed face logins the face path is locked for
``lockout_seconds`` -- during the lockout ``unlock`` refuses without touching the camera and the
user falls back to PIN/password (always available). A single successful face login clears the
counter. State is persisted to a small JSON file in APP_DIR so killing/restarting the service
does not reset an active lockout (a spoofer can't wipe it by bouncing the process). The clock is
injectable so the state machine is unit-testable without waiting real seconds.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Lockout:
    """Consecutive-failure lockout with persistence. Thread-safe."""

    def __init__(self, path: Path, max_attempts: int, lockout_seconds: int,
                 clock=time.time) -> None:
        self.path = Path(path)
        self.max_attempts = max(1, int(max_attempts))
        self.lockout_seconds = max(0, int(lockout_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._fails = 0
        self._locked_until = 0.0
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._fails = int(data.get("fails", 0))
            self._locked_until = float(data.get("locked_until", 0.0))
        except (FileNotFoundError, ValueError, OSError):
            self._fails = 0
            self._locked_until = 0.0

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({
                    "fails": self._fails,
                    "locked_until": self._locked_until,
                    "updated": self._clock(),
                }),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)   # atomic: never leave a half-written state file
        except OSError as e:
            log.warning("lockout state save failed: %s", e)

    # ---------- state ----------

    def remaining(self) -> float:
        """Seconds left on the current lockout, or 0.0 if not locked."""
        with self._lock:
            return max(0.0, self._locked_until - self._clock())

    def locked(self) -> bool:
        return self.remaining() > 0.0

    def record(self, success: bool) -> bool:
        """Record one face attempt. Success clears the counter; a failure increments it and,
        on reaching ``max_attempts``, starts a lockout (and resets the counter). Returns True
        if a lockout is active after this call.
        """
        with self._lock:
            if success:
                changed = self._fails != 0 or self._locked_until != 0.0
                self._fails = 0
                self._locked_until = 0.0
                if changed:
                    self._save()
                return False
            # failure
            if self._locked_until - self._clock() > 0:
                return True                      # already locked; don't stack the window
            self._fails += 1
            locked_now = self._fails >= self.max_attempts
            if locked_now:
                self._locked_until = self._clock() + self.lockout_seconds
                self._fails = 0
                log.warning("face auth locked out for %ds after %d consecutive failures",
                            self.lockout_seconds, self.max_attempts)
            self._save()
            return locked_now

    def reset(self) -> None:
        """Clear any lockout and the failure counter (admin / tray / test)."""
        with self._lock:
            self._fails = 0
            self._locked_until = 0.0
            self._save()

    def reconfigure(self, max_attempts: int, lockout_seconds: int) -> None:
        """Apply new thresholds live (e.g. after reload_config). Does not clear active state."""
        with self._lock:
            self.max_attempts = max(1, int(max_attempts))
            self.lockout_seconds = max(0, int(lockout_seconds))

    def status(self) -> dict:
        """Snapshot for status/telemetry."""
        with self._lock:
            rem = max(0.0, self._locked_until - self._clock())
            return {
                "locked": rem > 0.0,
                "remaining_s": round(rem, 1),
                "fails": self._fails,
                "max_attempts": self.max_attempts,
            }
