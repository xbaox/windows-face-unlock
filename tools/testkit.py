"""Shared helpers for the selftests (Stage 9).

Protocol v2 (act 9b §2.1) splits a grant in two: the reply is delivered, then the Credential
Provider reports what Windows did with it. The selftests drive FaceService._handle directly, so
they need to play both halves of the lock screen:

  * LOCK_SCREEN_SID / as_lock_screen(svc) -- the SYSTEM caller identity (no config key lifts the
    gate any more, R2);
  * deliver_and_report(svc, ok) -- what _serve_one does after the write (_finish_grant) followed
    by the CP's report_result for the grant just armed.
"""
from __future__ import annotations

LOCK_SCREEN_SID = "S-1-5-18"


def as_lock_screen(svc):
    """Make ``svc`` see every caller as the lock screen (SYSTEM)."""
    svc._caller_sid = lambda handle: LOCK_SCREEN_SID
    return svc


def deliver_and_report(svc, ok: bool = True) -> dict:
    """Settle the grant the last unlock / unlock_gesture reply armed: mark it delivered, then send
    the report_result the CP would send. Returns the report reply ({} when nothing was armed)."""
    svc._finish_grant(True)
    slot = getattr(svc, "_report_slot", None)
    if slot is None:
        return {}
    return svc._handle({"cmd": "report_result", "v": 2, "grant_id": slot["grant_id"], "ok": ok},
                       None)
