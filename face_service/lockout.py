"""Face-auth lockout after repeated failures (Stage 2, Step 6).

After ``max_attempts`` consecutive failed face logins the face path is locked for
``lockout_seconds`` -- during the lockout ``unlock`` refuses without touching the camera and the
user falls back to PIN/password (always available). A single successful face login clears the
counter. State is persisted to a small JSON file in APP_DIR so killing/restarting the service
does not reset an active lockout (a spoofer can't wipe it by bouncing the process). The clock is
injectable so the state machine is unit-testable without waiting real seconds.

Stage 9 (act 9b R9, F-112): the promise above only holds if the state reaches the disk. When a
save fails (full disk, a locked file) ``store_ok`` goes False and the service refuses face unlock
with ``lockout-store-error`` until a save succeeds again (``retry_save`` on every check) -- an
active lockout can no longer live in memory only and vanish with a restart. The numbers (5 tries,
300 s) are unchanged.

Stage 9 (F-135): the lockout runs on the wall clock, so a clock set BACK used to stretch it by the
size of the step, persisted across restarts. Both the loaded value and remaining() are now capped
at ``lockout_seconds`` from now.
"""
from __future__ import annotations

import json
import logging
import math
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
        self.store_ok = True          # False while the last save failed (R9, F-112)
        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        """Stage 8b (F-15). Defect: only FileNotFoundError / ValueError / OSError were caught, so a
        lockout.json holding a JSON list, a null, or a non-numeric value raised out of __init__;
        and a non-finite locked_until was accepted. Consequence: the service crashed at start on
        every restart -- a crash loop the watchdog cannot end -- or stayed locked "forever". Fix:
        ANY failure to read a sane state starts clean, with a WARNING (a missing file is the normal
        first start and stays quiet)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            fails = int(data.get("fails", 0))
            locked_until = float(data.get("locked_until", 0.0))
            if fails < 0 or not math.isfinite(locked_until) or locked_until < 0:
                raise ValueError(f"out of range (fails={fails}, locked_until={locked_until})")
            # F-135: never further in the future than one full lockout from now.
            locked_until = min(locked_until, self._clock() + self.lockout_seconds)
            self._fails, self._locked_until = fails, locked_until
        except FileNotFoundError:
            self._fails = 0
            self._locked_until = 0.0
        except Exception as e:
            log.warning("lockout state %s unreadable (%r) -- starting clean", self.path, e)
            self._fails = 0
            self._locked_until = 0.0

    def _save(self) -> bool:
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
            if self.store_ok:
                log.error("lockout state save failed: %s -- face unlock is refused "
                          "(lockout-store-error) until the state can be saved", e)
            self.store_ok = False
            return False
        if not self.store_ok:
            log.info("lockout state saved again; face unlock resumes")
        self.store_ok = True
        return True

    def retry_save(self) -> bool:
        """After a failed save: try again (the service calls this before every face attempt).
        True when the state on disk is current."""
        with self._lock:
            return self.store_ok or self._save()

    # ---------- state ----------

    def _remaining_locked(self) -> float:
        # F-135: capped at one full lockout, whatever the clock did since it started.
        return min(max(0.0, self._locked_until - self._clock()), float(self.lockout_seconds))

    def remaining(self) -> float:
        """Seconds left on the current lockout, or 0.0 if not locked."""
        with self._lock:
            return self._remaining_locked()

    def locked(self) -> bool:
        """Test helper (Stage 9, D-84: no production caller)."""
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
            if self._remaining_locked() > 0:
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
        """Clear any lockout and the failure counter. Test helper (Stage 9, D-84): no production
        path resets a lockout since 8b removed reset_lockout (F-21); a successful face does."""
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
            rem = self._remaining_locked()
            return {
                "locked": rem > 0.0,
                "remaining_s": round(rem, 1),
                "fails": self._fails,
                "max_attempts": self.max_attempts,
                "store_ok": self.store_ok,
            }
