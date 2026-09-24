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
# Stage 8b: a private home, so nothing below can reach a real ~/.face-unlock (see [5]).
os.environ["FACE_UNLOCK_HOME"] = tempfile.mkdtemp(prefix="faceunlock_wdself_")

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

    # --- 4) Stage 8b (F-29): a hostile or broken marker can never silence supervision ------------
    print("\n[4] F-29 pause markers: non-finite / far-future / unusable")
    import json
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "watchdog.pause"
        for label, raw in (("NaN until", '{"until": NaN, "created": 0}'),
                           ("Infinity until", '{"until": Infinity, "created": 0}'),
                           ("list body", "[1, 2]"), ("string until", '{"until": "soon"}')):
            p.write_text(raw, encoding="utf-8")
            t.ok(W.is_paused(p, now=100.0) is False and not p.exists(),
                 f"{label} -> not paused, marker removed")
        p.write_text(json.dumps({"until": 1e12, "created": 1.0}), encoding="utf-8")
        t.ok(W.is_paused(p, now=100.0, ttl_s=300.0) is True, "far-future until -> paused, but...")
        t.ok(abs(json.loads(p.read_text(encoding="utf-8"))["until"] - 400.0) < 1e-6,
             "...clamped to now + ttl and written back")
        t.ok(W.is_paused(p, now=401.0, ttl_s=300.0) is False and not p.exists(),
             "the clamped pause expires on schedule")
        for bad in (float("nan"), float("inf")):
            try:
                W.write_pause(p, now=0.0, ttl_s=bad)
                t.ok(False, f"write_pause refuses ttl={bad}")
            except ValueError:
                t.ok(True, f"write_pause refuses ttl={bad}")
        W.write_pause(p, now=0.0, ttl_s=1e9)
        t.ok(json.loads(p.read_text(encoding="utf-8"))["until"] == W.MAX_PAUSE_TTL_S,
             "write_pause caps the TTL at MAX_PAUSE_TTL_S")
        # a marker that cannot be read or deleted must not raise out of either helper
        d = Path(td) / "is_a_dir.pause"
        d.mkdir()
        try:
            got = W.is_paused(d, now=1.0)
            W.clear_pause(d)
            t.ok(got is False, "unreadable + undeletable marker -> not paused, no exception")
        except Exception as e:
            t.ok(False, f"unreadable + undeletable marker raised {e!r}")

    # --- 5) Stage 8b (F-29): one failing iteration does not end the loop ----------------------
    print("\n[5] F-29 per-iteration guard in tools.watchdog.main")
    import tools.watchdog as TW
    calls = {"n": 0}
    saved = (TW.ping, TW.time.sleep, TW._setup_logging)

    def boom(_timeout):
        calls["n"] += 1
        raise RuntimeError("simulated iteration failure")

    def fake_sleep(_s):
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    from face_service.config import Config
    saved_cfg = TW._load_config
    # Plain dataclass defaults: this test never reads a config.toml (nor any file of a real home).
    TW.ping, TW.time.sleep, TW._setup_logging = boom, fake_sleep, (lambda: None)
    TW._load_config = lambda: Config(language="en")
    try:
        rc = TW.main()
    finally:
        TW.ping, TW.time.sleep, TW._setup_logging = saved
        TW._load_config = saved_cfg
    t.ok(rc == 0 and calls["n"] == 3, f"three failing iterations, loop survived (n={calls['n']})")

    # --- 6) Stage 8b (F-36): the installed kill filter is path + session scoped ---------------
    print("\n[6] F-36 installed kill filter")
    ps = TW._match_ps(True)
    t.ok("ExecutablePath -eq '" in ps and "face_service.exe'" in ps,
         "installed filter pins the full exe path")
    t.ok("SessionId -eq " in ps, "installed filter pins the session")
    t.ok("CommandLine" in TW._match_ps(False), "dev filter unchanged (command-line needle)")

    print()
    if t.fail:
        print(f"WATCHDOG SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("WATCHDOG SELFTEST OK: restart fires at threshold, pause suppresses a deliberate stop, "
          "and a stale/expired pause self-heals (never silences the watchdog forever); a "
          "non-finite or far-future pause is refused or clamped, a broken marker never raises, "
          "a failing iteration does not end the loop, and the installed kill is path+session "
          "scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
