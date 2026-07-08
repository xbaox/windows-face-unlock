"""tools/watchdog_selftest.py -- Stage 3 / Step 5 watchdog decision + pause proof (no processes).

Covers the pure watchdog policy in face_service.watchdog:
  [1] should_restart -- fires at the threshold, not below, and never while paused.
  [2] pause lifecycle -- write/is_paused/clear; a FRESH pause suppresses restart, an EXPIRED pause
      is ignored AND deleted (self-heal), corrupt/missing markers are treated as not paused.
  [3] end-to-end -- threshold reached + fresh pause -> no restart; + expired pause -> restart.

No real service / no pywin32. Run from the repo root:
    python -m tools.watchdog_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service import watchdog as W


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def main(argv=None) -> int:
    t = T()

    # --- 1) should_restart ------------------------------------------------------------------
    print("[1] should_restart(consecutive_fails, threshold, paused)")
    t.ok(W.should_restart(3, 3, False) is True, "fails == threshold & not paused -> restart")
    t.ok(W.should_restart(5, 3, False) is True, "fails > threshold & not paused -> restart")
    t.ok(W.should_restart(2, 3, False) is False, "fails < threshold -> no restart")
    t.ok(W.should_restart(0, 3, False) is False, "no fails -> no restart")
    t.ok(W.should_restart(9, 3, True) is False, "paused suppresses restart even far past threshold")

    # --- 1b) restart_outcome (kill-then-start survival) -------------------------------------
    print("\n[1b] restart_outcome(alive_after_start)")
    t.ok(W.restart_outcome(1) == "started", "1 pythonw survived the start -> started (normal)")
    t.ok(W.restart_outcome(3) == "started", ">=1 survived -> started")
    t.ok(W.restart_outcome(0) == "unrecoverable",
         "0 survived -> unrecoverable (non-pythonw dev instance holds the mutex / misconfig -> back off)")

    # --- 2) pause lifecycle -----------------------------------------------------------------
    print("\n[2] pause file: write / is_paused / clear, with self-expiry")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "watchdog.pause"

        t.ok(W.is_paused(p, now=100.0) is False, "missing marker -> not paused")

        W.write_pause(p, now=100.0, ttl_s=300.0)   # valid until 400
        t.ok(p.exists(), "write_pause creates the marker")
        t.ok(W.is_paused(p, now=150.0) is True, "FRESH pause (now<until) -> paused")
        t.ok(p.exists(), "a fresh pause is NOT deleted")

        t.ok(W.is_paused(p, now=400.0) is False, "at expiry (now==until) -> not paused")
        t.ok(not p.exists(), "EXPIRED pause is DELETED (self-heal)")

        # re-create then let it expire strictly past
        W.write_pause(p, now=0.0, ttl_s=10.0)       # valid until 10
        t.ok(W.is_paused(p, now=5.0) is True, "fresh again -> paused")
        t.ok(W.is_paused(p, now=20.0) is False and not p.exists(),
             "well-past expiry -> not paused AND deleted")

        # clear_pause removes an active marker; no error if already gone
        W.write_pause(p, now=1000.0, ttl_s=300.0)
        W.clear_pause(p)
        t.ok(not p.exists(), "clear_pause deletes an active marker")
        W.clear_pause(p)   # idempotent -> must not raise
        t.ok(True, "clear_pause on a missing file does not raise")

        # corrupt marker -> not paused AND removed
        p.write_text("not-json{{", encoding="utf-8")
        t.ok(W.is_paused(p, now=1.0) is False and not p.exists(),
             "corrupt marker -> not paused AND deleted")

    # --- 3) end-to-end: threshold + pause state ---------------------------------------------
    print("\n[3] threshold reached, gated by pause state")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "watchdog.pause"
        # deliberate stop just happened: fresh pause -> even at threshold, no restart
        W.write_pause(p, now=1000.0, ttl_s=300.0)
        paused_fresh = W.is_paused(p, now=1000.0)
        t.ok(W.should_restart(3, 3, paused_fresh) is False,
             "threshold reached but pause is FRESH -> NOT restarting (deliberate stop respected)")
        # much later: pause expired -> restart allowed (self-heal), marker gone
        paused_expired = W.is_paused(p, now=2000.0)
        t.ok(W.should_restart(3, 3, paused_expired) is True and not p.exists(),
             "same fault later: pause EXPIRED -> restart allowed and stale marker deleted")

    print()
    if t.fail:
        print(f"WATCHDOG SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("WATCHDOG SELFTEST OK: restart fires at threshold, pause suppresses a deliberate stop, "
          "and a stale/expired pause self-heals (never silences the watchdog forever).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
