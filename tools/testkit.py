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


def skip_is_failure(what: str, e: BaseException) -> bool:
    """Stage 9 (D-141, B14-08): a section that cannot run is a FAILURE unless the run was started
    with ``--allow-skip`` -- a regression that breaks an import must not vanish as "skip" under an
    OK result. Prints the line; returns True when the caller must count a failure."""
    import sys
    allowed = "--allow-skip" in sys.argv
    print(f"  {'skip' if allowed else 'FAIL'}  {what} unavailable ({e.__class__.__name__}: {e})"
          + ("" if allowed else " -- run with --allow-skip to accept a partial run"))
    return not allowed


# Stage 9 (D-142, B14-09): module globals replaced by a test are put back after every test function,
# so tests that share a process (stage4, a runner) never see another test's fake password store or
# fake pipe.
_PATCHES: list = []


def patch(obj, name: str, value):
    """``setattr(obj, name, value)``, remembering the original for restore_all()."""
    _PATCHES.append((obj, name, getattr(obj, name)))
    setattr(obj, name, value)
    return value


def restore_all() -> None:
    while _PATCHES:
        obj, name, orig = _PATCHES.pop()
        setattr(obj, name, orig)


def run_restoring(*tests) -> None:
    """Run each test function, restoring every patch() after each one (even when it raises)."""
    for fn in tests:
        try:
            fn()
        finally:
            restore_all()
