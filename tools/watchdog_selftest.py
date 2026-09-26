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
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_wdself_")

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
    print("\n[1b] restart_outcome(pong_after_start) and the back-off (Stage 9, R11)")
    t.ok(W.restart_outcome(True) == "started", "pong after the start -> started")
    t.ok(W.restart_outcome(False) == "unrecoverable", "no pong in the window -> unrecoverable")
    t.ok([W.restart_backoff_s(n) for n in range(7)] == [60, 120, 240, 480, 960, 1800, 1800],
         "back-off 60 s x 2^n, capped at 30 min")
    t.ok(W.restart_backoff_s(10 ** 6) == 1800 and W.restart_backoff_s(-3) == 60,
         "back-off is bounded for any n")
    t.ok(W.POST_RESTART_PONG_S == 30.0 and W.HEALTHY_RESET_S == 600.0,
         "pong window 30 s, reset after 10 min of health")

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
    saved_si = TW._single_instance
    TW._single_instance = lambda: True
    try:
        rc = TW.main()
    finally:
        TW.ping, TW.time.sleep, TW._setup_logging = saved
        TW._load_config = saved_cfg
        TW._single_instance = saved_si
    t.ok(rc == 0 and calls["n"] == 3, f"three failing iterations, loop survived (n={calls['n']})")

    # --- 6) the process criterion: path + session (F-36), normcase/realpath (F-253), argv ------
    print("\n[6] service-process criterion (psutil, no PowerShell -- F-251)")
    me = os.getpid()
    sess = TW._session_of(me)
    tgt = r"C:\Program Files\Face Unlock\face_service.exe"
    inst = dict(installed=True, target=tgt, session=sess)
    t.ok(TW._is_service_proc({"pid": me, "exe": tgt}, **inst), "installed: exact path matches")
    t.ok(TW._is_service_proc({"pid": me, "exe": tgt.upper()}, **inst),
         "installed: case differences do not matter (F-253)")
    t.ok(not TW._is_service_proc({"pid": me, "exe": r"C:\Other\face_service.exe"}, **inst),
         "installed: same name elsewhere is not ours")
    t.ok(not TW._is_service_proc({"pid": me, "exe": tgt}, installed=True, target=tgt,
                                 session=(sess or 0) + 7777), "installed: another session is not ours")
    dev = dict(installed=False, target="", session=sess)
    t.ok(TW._is_service_proc({"pid": me, "name": "pythonw.exe",
                              "cmdline": [r"C:\x\pythonw.exe", "-m", "face_service"]}, **dev),
         "dev: pythonw -m face_service matches")
    t.ok(not TW._is_service_proc({"pid": me, "name": "pythonw.exe",
                                  "cmdline": [r"C:\face_service_repo\.venv\Scripts\pythonw.exe",
                                              "-m", "presence_monitor"]}, **dev),
         "dev: a checkout path containing face_service does not match the tray")
    t.ok(not TW._is_service_proc({"pid": me, "name": "python.exe",
                                  "cmdline": ["python.exe", "-m", "face_service"]}, **dev),
         "dev: a debugging python.exe console instance is spared")
    src = Path(TW.__file__).read_text(encoding="utf-8")
    t.ok("powershell" not in src.lower().replace("no powershell", ""),
         "the runner starts no PowerShell child")

    # --- 7) N-21 / N-22 / N-23: pause lift, restart grace, single instance --------------------
    print("\n[7] R11 runner: pause lift (N-21), restart grace (N-22), single instance (N-23)")
    from face_service import config as C
    pause = Path(os.environ["FACE_UNLOCK_HOME"]) / "watchdog.pause"
    C.WATCHDOG_PAUSE_PATH = pause
    clock = {"t": 1000.0}
    script = {"ok": False}
    restarts = []
    saved7 = (TW.ping, TW.restart_service, TW.time.monotonic)
    TW.ping = lambda _t: (script["ok"], None if script["ok"] else "no-pipe")
    TW.time.monotonic = lambda: clock["t"]

    def fake_restart(_timeout):
        restarts.append(clock["t"])
        return 1, script.get("pong", False)
    TW.restart_service = fake_restart
    try:
        st = TW.State(clock["t"])
        W.write_pause(pause, now=__import__("time").time(), ttl_s=300.0)
        for _ in range(10):
            TW._iteration(st, 2.0, 3, 300.0)
            clock["t"] += 30
        t.ok(st.fails == 0 and not restarts, "N-21: ten failed pings during a pause -> counter 0, no restart")
        W.clear_pause(pause)
        TW._iteration(st, 2.0, 3, 300.0)
        t.ok(st.fails == 1 and not restarts, "N-21: pause lifted -> counting starts from 1, no instant restart")
        TW._iteration(st, 2.0, 3, 300.0)
        TW._iteration(st, 2.0, 3, 300.0)
        t.ok(len(restarts) == 1, "threshold reached after the lift -> one restart")
        # no pong -> failed restart; next attempt only after 60 s, then 120 s
        for _ in range(3):
            clock["t"] += 10
            TW._iteration(st, 2.0, 3, 300.0)
        t.ok(len(restarts) == 1, "N-22: a failed restart is not repeated before its 60 s back-off")
        clock["t"] = restarts[0] + 61
        for _ in range(3):
            TW._iteration(st, 2.0, 3, 300.0)
        t.ok(len(restarts) == 2, "N-22: after 60 s the second restart runs")
        clock["t"] = restarts[1] + 100
        for _ in range(3):
            TW._iteration(st, 2.0, 3, 300.0)
        t.ok(len(restarts) == 2, "N-22: the third waits 120 s")
        clock["t"] = restarts[1] + 121
        for _ in range(3):
            TW._iteration(st, 2.0, 3, 300.0)
        t.ok(len(restarts) == 3, "N-22: ...and then runs")
        # healthy for 10 minutes -> back-off reset
        script["ok"] = True
        for _ in range(25):
            clock["t"] += 30
            TW._iteration(st, 2.0, 3, 300.0)
        t.ok(st.restarts == 0, "N-22: 10 minutes of pongs reset the back-off")
        # refusing = alive
        saved_state = TW.last_ping_state
        TW.last_ping_state = "refusing:custody"
        TW._iteration(st, 2.0, 3, 300.0)
        t.ok(st.fails == 0 and st.refusing == "refusing:custody", "a refusing service is alive (R11)")
        TW.last_ping_state = saved_state
    finally:
        TW.ping, TW.restart_service, TW.time.monotonic = saved7
    t.ok("clear_pause" not in src.split("def _iteration", 1)[1], "F-250: no clear_pause after a restart")
    h1 = TW._single_instance()
    h2 = TW._single_instance()
    t.ok(h1 not in (None, True) and h2 is None, "N-23: a second watchdog in the session is refused")
    del h1

    print()
    if t.fail:
        print(f"WATCHDOG SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("WATCHDOG SELFTEST OK: restart fires at threshold, pause suppresses a deliberate stop "
          "and resets the count, a stale/expired pause self-heals; a non-finite or far-future "
          "pause is refused or clamped, a broken marker never raises, a failing iteration does "
          "not end the loop, restarts wait for a pong and back off 60 s x 2^n, a refusing "
          "service is alive, a second watchdog is refused, and the kill is path+session scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
