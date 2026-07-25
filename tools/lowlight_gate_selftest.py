"""tools/lowlight_gate_selftest.py -- Stage 3 / Step 3.2 low-light gate proof (no camera).

Covers the shipping low-light gate:
  [1] evaluate_low_light -- the pure decision: force "too-dark" below the floor even when
      recognition would grant; strict boundary; pass-through above the floor; floor 0 disables.
  [2] Config.validate -- low_light_luma_min in [0,255] passes; out-of-range fails loud.
  [3] lockout-NEUTRALITY -- through the REAL FaceService.unlock handler: a too-dark unlock never
      calls Lockout.record (no strike, no reset), audits outcome "too-dark" with scene_luma, and
      returns reason "too-dark" -- while an above-floor unlock is byte-for-byte the old behaviour
      (records the attempt, reason "no-match").
  [4] scene_luma -- the canonical metric equals the exact cvtColor->mean formula the 3.1 probe
      measured the floor with (probe and prod import the same function; this guards it).

No camera / no GPU. Run from the repo root:
    python -m tools.lowlight_gate_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service import lowlight as LL
from face_service.config import Config


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def _raises_value_error(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


def main(argv=None) -> int:
    t = T()
    floor = 45.0
    TOO_DARK = LL.TOO_DARK_REASON
    t.ok(TOO_DARK == "too-dark", f'reason token is "too-dark" (got {TOO_DARK!r})')

    # --- 1) evaluate_low_light: the pure decision ------------------------------------------
    print("\n[1] evaluate_low_light gate coverage")

    g, r, td = LL.evaluate_low_light(60.0, floor, True)
    t.ok(g is True and r is None and td is False, "scene>=floor & would_grant -> grant, no reason")

    g, r, td = LL.evaluate_low_light(10.0, floor, True)
    t.ok(g is False and r == TOO_DARK and td is True,
         "scene<floor & would_grant=True -> DENY too-dark (match blocked in the dark)")

    g, r, td = LL.evaluate_low_light(10.0, floor, False)
    t.ok(g is False and r == TOO_DARK and td is True,
         "scene<floor & would_grant=False -> DENY too-dark (honest reason, not generic)")

    g, r, td = LL.evaluate_low_light(80.0, floor, False)
    t.ok(g is False and r is None and td is False,
         "scene>=floor & would_grant=False -> pass-through deny (reason NOT too-dark)")

    g, r, td = LL.evaluate_low_light(floor, floor, True)
    t.ok(g is True and r is None and td is False,
         "scene == floor -> NOT too-dark (strict '<'); grant passes through")

    g, r, td = LL.evaluate_low_light(floor - 1e-6, floor, True)
    t.ok(td is True and r == TOO_DARK, "scene = floor - 1e-6 -> too-dark")

    # floor 0 disables the gate: nothing (a non-negative mean) is ever < 0.
    g, r, td = LL.evaluate_low_light(0.0, 0.0, True)
    t.ok(g is True and r is None and td is False, "floor=0 & scene=0 -> disabled (not too-dark), grant")
    g, r, td = LL.evaluate_low_light(0.0, 0.0, False)
    t.ok(td is False and r is None, "floor=0 & scene=0 & no grant -> pass-through (not too-dark)")

    # --- 2) Config.validate: floor range -----------------------------------------------------
    print("\n[2] Config.validate low_light_luma_min range")
    t.ok(Config().low_light_luma_min == 45.0, "default low_light_luma_min == 45.0")

    def _cfg(v):
        c = Config()
        c.low_light_luma_min = v
        return c

    t.ok(not _raises_value_error(_cfg(45.0).validate), "45.0 validates")
    t.ok(not _raises_value_error(_cfg(0.0).validate), "0.0 validates (gate disabled)")
    t.ok(not _raises_value_error(_cfg(255.0).validate), "255.0 validates (upper bound)")
    t.ok(_raises_value_error(_cfg(-1.0).validate), "-1.0 -> fail-loud ValueError")
    t.ok(_raises_value_error(_cfg(256.0).validate), "256.0 -> fail-loud ValueError")

    # --- 3) lockout-neutrality through the REAL unlock handler -------------------------------
    print("\n[3] lockout-neutrality: too-dark unlock never touches Lockout.record")
    try:
        from face_service.service import FaceService, VerifyOutcome
    except Exception as e:   # pragma: no cover - pywin32 absent in this context
        print(f"  skip  service import unavailable ({e.__class__.__name__}); lockout proof skipped")
    else:
        class _LockoutSpy:
            def __init__(self):
                self.records = []      # every record(success) call lands here

            def remaining(self):
                return 0.0             # never locked out for the test

            def record(self, success):
                self.records.append(success)
                return False

        class _AuditStub:
            def __init__(self):
                self.records = []

            def write(self, event, record):
                self.records.append((event, dict(record)))

        def _svc(cfg, outcome):
            s = FaceService.__new__(FaceService)   # bypass heavy __init__ (camera/pywin32/files)
            s.cfg = cfg
            s._lockout = _LockoutSpy()
            s._audit = _AuditStub()
            s._camera_paused_until = 0.0            # _camera_leased_out() -> False
            s._capture_and_verify = lambda: outcome
            # Neutralise the Step-3.3 boost the same way lowlight_boost_selftest.py:197 does.
            # A dark scene sends the unlock path through _maybe_boost (service.py:805), and
            # THIS fake has no _cam_lock, so the real one would raise AttributeError at
            # service.py:477 -- swallowed by the defensive handler at :492, but it would then
            # be one added attribute away from calling _acquire_camera() against a real
            # device in a harness that never patches SVC.Camera. Returning the outcome
            # unchanged with an empty audit dict is exactly what the swallow produced, so
            # every assertion below is unaffected (service.py:806 merges {} harmlessly).
            s._maybe_boost = lambda r: (r, {})
            return s

        def _svc_cfg():
            c = Config()
            # This harness calls _handle({"cmd": "unlock"}) with NO pipe handle, so the
            # Stage-5 SID gate resolves the client SID to None and refuses with
            # "not-authorized" before the low-light path is ever reached. The gate is not
            # what these cases exercise -- same reason and same shape as
            # tools/camera_busy_selftest.py:173 (block6-A-fix, bc4e25b). The gate keeps its
            # own dedicated coverage in tools/pipe_hardening_selftest.py:148-160, which
            # asserts both that it REFUSES a non-SYSTEM caller when on (:148-153) and that
            # it allows one through when off (:155-160). Test scaffold only: production
            # behaviour and the Stage-4/5 perimeter are untouched.
            c.pipe_unlock_require_system = False
            return c

        cfg = _svc_cfg()  # low_light_luma_min = 45.0
        detail_dark = {"verdict": "PASS", "distance": 0.36, "scene_luma": 10.0}

        # (a) too-dark AND recognition WOULD have matched -> still denied, NO lockout touch.
        svc = _svc(cfg, VerifyOutcome(True, 0.36, True, dict(detail_dark), None, 10.0))
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "too-dark" and resp.get("ok") is False,
             "too-dark + would-match -> deny reason 'too-dark' (match forced off in the dark)")
        t.ok(svc._lockout.records == [],
             "too-dark + would-match -> Lockout.record NOT called (no strike, no reset)")
        ev, rec = svc._audit.records[-1]
        t.ok(ev == "unlock" and rec.get("outcome") == "too-dark" and rec.get("scene_luma") == 10.0,
             "too-dark audited as outcome 'too-dark' with scene_luma")

        # (b) too-dark AND no-match -> honest 'too-dark' (NOT 'no-match'), still no lockout touch.
        svc = _svc(cfg, VerifyOutcome(False, 0.90, True, {"verdict": "NOT_LIVE", "scene_luma": 8.0}, None, 8.0))
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "too-dark" and svc._lockout.records == [],
             "too-dark + no-match -> reason 'too-dark', Lockout.record NOT called")

        # (c) ABOVE floor + no-match -> unchanged: records the failed attempt, reason 'no-match'.
        svc = _svc(cfg, VerifyOutcome(False, 0.50, True, {"verdict": "NOT_LIVE", "scene_luma": 80.0}, None, 80.0))
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "no-match" and svc._lockout.records == [False],
             "above floor + no-match -> reason 'no-match', Lockout.record(False) called (unchanged)")

        # (d) gate disabled (floor 0): even a dark scene passes through to the normal path.
        cfg0 = _svc_cfg()
        cfg0.low_light_luma_min = 0.0
        svc = _svc(cfg0, VerifyOutcome(False, 0.50, True, {"verdict": "NOT_LIVE", "scene_luma": 5.0}, None, 5.0))
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "no-match" and svc._lockout.records == [False],
             "floor=0 -> gate off: dark scene still takes the normal path (record called)")

        # (e) no scene measured (camera leased / no frame): scene_luma None -> gate skipped.
        svc = _svc(cfg, VerifyOutcome(False, 1.0, False, {"verdict": "SKIPPED", "reason": "camera-leased"}, None, None))
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "no-match" and svc._lockout.records == [False],
             "scene_luma None -> not mislabeled too-dark (falls through to existing path)")

    # --- 4) scene_luma is the canonical cvtColor->mean formula (probe == prod) ---------------
    print("\n[4] scene_luma == canonical cvtColor(BGR2GRAY).mean()")
    try:
        import cv2
        import numpy as np
    except Exception as e:   # pragma: no cover - cv2 absent in this context
        print(f"  skip  cv2/numpy unavailable ({e.__class__.__name__}); numeric-identity check skipped")
    else:
        rng = np.random.default_rng(7)
        img = rng.integers(0, 256, (48, 64, 3), dtype=np.uint8)
        raw = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
        t.ok(LL.scene_luma(img) == raw, "scene_luma() bit-identical to the probe's original formula")
        t.ok(isinstance(LL.scene_luma(img), float), "scene_luma returns a plain float")

    print()
    if t.fail:
        print(f"LOW-LIGHT GATE SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("LOW-LIGHT GATE SELFTEST OK: below-floor forces honest too-dark; lockout stays neutral; "
          "above-floor behaviour unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
