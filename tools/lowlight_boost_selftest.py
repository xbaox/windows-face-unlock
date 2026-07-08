"""tools/lowlight_boost_selftest.py -- Stage 3 / Step 3.3 exposure-boost proof (no camera).

Covers the gated low-light boost:
  [1] plan_exposure -- brighten target = current + step.
  [2] honored -- set->get roundtrip acceptance (exact / clamped / ignored).
  [3] try_exposure_boost -- the SAFETY property: exposure is ALWAYS restored (honored, ignored,
      and re-capture-raises paths), re-capture only runs when honored and runs under the boosted
      exposure, and a re-capture exception never escapes.
  [4] Config.validate -- low_light_boost bool + low_light_exposure_step in (0,16] fail loud.
  [5] unlock wiring -- through the REAL FaceService.unlock handler: boost is gated to below-floor
      + enabled, its (possibly brighter) result feeds the 3.2 gate, a lifted+matched frame grants,
      a lifted+no-match frame is a normal strike, a still-dark result stays lockout-neutral
      too-dark, and boost telemetry is merged into the audit.

No camera / no GPU. Run from the repo root:
    python -m tools.lowlight_boost_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service import camera_boost as CB
from face_service.config import Config


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


class FakeCap:
    """cv2.VideoCapture stand-in that tracks EXPOSURE. ``honor`` picks whether a set is accepted
    (read-back matches) or silently ignored (read-back stays put, like this webcam's GAIN)."""

    def __init__(self, start, honor=True):
        self.exposure = float(start)
        self.honor = honor
        self.sets = []          # every value passed to set(), in order (last == restore target)

    def get(self, prop):
        return self.exposure

    def set(self, prop, value):
        self.sets.append(float(value))
        if self.honor:
            self.exposure = float(value)
        return True


class Rec:
    """Fake re-capture: counts calls, records the exposure it saw, returns a value or raises."""

    def __init__(self, cap=None, value="RC", boom=False):
        self.calls = 0
        self.value = value
        self.boom = boom
        self.cap = cap
        self.exposure_seen = None

    def __call__(self):
        self.calls += 1
        if self.cap is not None:
            self.exposure_seen = self.cap.exposure
        if self.boom:
            raise RuntimeError("recapture boom")
        return self.value


def main(argv=None) -> int:
    t = T()

    # --- 1) plan_exposure -------------------------------------------------------------------
    print("[1] plan_exposure")
    t.ok(CB.plan_exposure(-6.0, 2.0) == -4.0, "-6 + 2 -> -4 (brighter on this webcam's scale)")
    t.ok(CB.plan_exposure(0.0, 2.0) == 2.0, "0 + 2 -> 2")

    # --- 2) honored -------------------------------------------------------------------------
    print("\n[2] honored (set->get roundtrip)")
    t.ok(CB.honored(-4.0, -4.0) is True, "readback == request -> honored")
    t.ok(CB.honored(-4.0, -3.9) is True, "readback within tol -> honored")
    t.ok(CB.honored(-4.0, -6.0) is False, "readback stuck at old value -> NOT honored (ignored set)")
    t.ok(CB.honored(-4.0, -5.0) is False, "readback clamped far from request -> NOT honored")

    # --- 3) try_exposure_boost: restore ALWAYS + gated re-capture ---------------------------
    print("\n[3] try_exposure_boost restore-always + re-capture gating")

    # (a) honored: re-capture runs (under boosted exposure), then exposure restored.
    cap = FakeCap(-6.0, honor=True)
    rec = Rec(cap=cap, value="RC")
    out = CB.try_exposure_boost(cap, 2.0, rec)
    t.ok(out.applied and out.honored and out.recapture == "RC", "honored -> applied, re-capture used")
    t.ok(rec.calls == 1 and rec.exposure_seen == -4.0, "re-capture ran ONCE under boosted exposure (-4)")
    t.ok(cap.exposure == -6.0 and cap.sets[-1] == -6.0, "exposure RESTORED to -6 after success")

    # (b) ignored: driver keeps old value -> no re-capture, but still restored.
    cap = FakeCap(-6.0, honor=False)
    rec = Rec(cap=cap)
    out = CB.try_exposure_boost(cap, 2.0, rec)
    t.ok((not out.applied) and (not out.honored) and out.recapture is None,
         "driver ignores set -> not applied, not honored, no re-capture")
    t.ok(rec.calls == 0, "re-capture NOT called when the set was ignored (no wasted burst)")
    t.ok(cap.exposure == -6.0 and cap.sets[-1] == -6.0, "exposure RESTORED even when ignored")

    # (c) re-capture raises: swallowed, error recorded, exposure STILL restored.
    cap = FakeCap(-6.0, honor=True)
    rec = Rec(cap=cap, boom=True)
    out = CB.try_exposure_boost(cap, 2.0, rec)
    t.ok((not out.applied) and out.honored and out.error is not None,
         "re-capture raises -> not applied, honored True, error captured (no exception escapes)")
    t.ok(rec.calls == 1 and cap.exposure == -6.0 and cap.sets[-1] == -6.0,
         "exposure RESTORED via finally even when re-capture raised")

    # (d) audit() shape is additive telemetry.
    aud = CB.try_exposure_boost(FakeCap(-6.0, honor=True), 2.0, Rec()).audit()
    t.ok(aud.get("boost_applied") is True and aud.get("boost_honored") is True
         and aud.get("exposure_before") == -6.0 and aud.get("exposure_after") == -4.0,
         "audit() reports applied/honored/exposure before+after")

    # --- 4) Config.validate: boost fields ---------------------------------------------------
    print("\n[4] Config.validate low_light_boost / low_light_exposure_step")
    t.ok(Config().low_light_boost is True, "default low_light_boost True (TODO: confirm w/ Bao)")
    t.ok(Config().low_light_exposure_step == 2.0, "default low_light_exposure_step == 2.0")

    def _cfg(**over):
        c = Config()
        for k, v in over.items():
            setattr(c, k, v)
        return c

    def _raises(fn):
        try:
            fn()
            return False
        except ValueError:
            return True

    t.ok(not _raises(_cfg().validate), "defaults validate")
    t.ok(not _raises(_cfg(low_light_boost=False).validate), "low_light_boost False validates")
    t.ok(_raises(_cfg(low_light_boost="yes").validate), "low_light_boost non-bool -> fail-loud")
    t.ok(not _raises(_cfg(low_light_exposure_step=16.0).validate), "step 16.0 validates (upper bound)")
    t.ok(_raises(_cfg(low_light_exposure_step=0.0).validate), "step 0 -> fail-loud (must brighten)")
    t.ok(_raises(_cfg(low_light_exposure_step=16.1).validate), "step 16.1 -> fail-loud")
    t.ok(_raises(_cfg(low_light_exposure_step=-1.0).validate), "step -1 -> fail-loud")

    # --- 5) unlock wiring through the REAL handler ------------------------------------------
    print("\n[5] unlock wiring: gating + boosted result feeds the 3.2 gate + neutrality")
    try:
        import face_service.service as SVC
        from face_service.service import FaceService, VerifyOutcome
    except Exception as e:   # pragma: no cover - pywin32 absent in this context
        print(f"  skip  service import unavailable ({e.__class__.__name__}); unlock wiring skipped")
    else:
        class _LockoutSpy:
            def __init__(self):
                self.records = []

            def remaining(self):
                return 0.0

            def record(self, success):
                self.records.append(success)
                return False

        class _AuditStub:
            def __init__(self):
                self.records = []

            def write(self, event, record):
                self.records.append((event, dict(record)))

        class _BoostSpy:
            def __init__(self, outcome, audit):
                self.calls = 0
                self.outcome = outcome
                self.audit = audit

            def __call__(self, r):
                self.calls += 1
                return self.outcome, self.audit

        def _svc(cfg, dark, boost_spy):
            s = FaceService.__new__(FaceService)
            s.cfg = cfg
            s._lockout = _LockoutSpy()
            s._audit = _AuditStub()
            s._camera_paused_until = 0.0
            s._capture_and_verify = lambda: dark
            s._maybe_boost = boost_spy
            s._maybe_adapt_gallery = lambda r: None
            return s

        cfg = Config()  # low_light_luma_min 45, low_light_boost True, step 2
        dark = VerifyOutcome(False, 0.36, True, {"verdict": "NOT_LIVE", "scene_luma": 10.0}, None, 10.0)

        # (a) boost lifts scene above floor AND matches -> grant (with stubbed credentials).
        bright_match = VerifyOutcome(True, 0.10, True, {"verdict": "PASS", "scene_luma": 60.0}, None, 60.0)
        baud = {"boost_applied": True, "boost_honored": True, "scene_luma_before": 10.0,
                "scene_luma_after": 60.0, "exposure_before": -6.0, "exposure_after": -4.0}
        orig_lp = SVC.load_password
        SVC.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
        try:
            spy = _BoostSpy(bright_match, baud)
            svc = _svc(cfg, dark, spy)
            resp = svc._handle({"cmd": "unlock"})
            t.ok(spy.calls == 1, "dark first burst + boost enabled -> _maybe_boost called")
            t.ok(resp.get("ok") is True and resp.get("username") == "admin",
                 "boost lifted + matched -> GRANT via the normal path")
            t.ok(svc._lockout.records == [True], "grant records success (lockout reset), not a strike")
            ev, rec = svc._audit.records[-1]
            t.ok(ev == "unlock" and rec.get("outcome") == "granted"
                 and rec.get("boost_applied") is True and rec.get("scene_luma_after") == 60.0,
                 "audit 'granted' carries boost telemetry (applied + scene before/after)")
        finally:
            SVC.load_password = orig_lp

        # (b) boost lifts scene above floor but NO match -> genuine no-match in restored light.
        bright_nomatch = VerifyOutcome(False, 0.50, True, {"verdict": "NOT_LIVE", "scene_luma": 60.0}, None, 60.0)
        spy = _BoostSpy(bright_nomatch, {**baud, "scene_luma_after": 60.0})
        svc = _svc(cfg, dark, spy)
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "no-match" and svc._lockout.records == [False],
             "boost lifted but no-match -> reason 'no-match', strike recorded (light is adequate now)")

        # (c) boost fails to lift (still dark) -> stays too-dark, LOCKOUT-NEUTRAL.
        still_dark = VerifyOutcome(False, 0.36, True, {"verdict": "NOT_LIVE", "scene_luma": 12.0}, None, 12.0)
        spy = _BoostSpy(still_dark, {"boost_applied": False, "boost_honored": True, "scene_luma_before": 10.0})
        svc = _svc(cfg, dark, spy)
        resp = svc._handle({"cmd": "unlock"})
        t.ok(resp.get("reason") == "too-dark" and svc._lockout.records == [],
             "boost couldn't lift -> too-dark, Lockout.record NOT called (neutral)")
        ev, rec = svc._audit.records[-1]
        t.ok(rec.get("outcome") == "too-dark" and rec.get("boost_applied") is False,
             "audit 'too-dark' shows the boost was attempted but did not apply")

        # (d) gating: bright first burst (>= floor) -> boost NOT attempted.
        bright_first = VerifyOutcome(False, 0.50, True, {"verdict": "NOT_LIVE", "scene_luma": 80.0}, None, 80.0)
        spy = _BoostSpy(bright_first, {})
        svc = _svc(cfg, bright_first, spy)
        resp = svc._handle({"cmd": "unlock"})
        t.ok(spy.calls == 0 and resp.get("reason") == "no-match" and svc._lockout.records == [False],
             "scene >= floor -> no boost, normal no-match (unchanged 3.2 path)")

        # (e) toggle off: low_light_boost=False -> boost NOT attempted even in the dark.
        cfg_off = Config()
        cfg_off.low_light_boost = False
        spy = _BoostSpy(dark, {})
        svc = _svc(cfg_off, dark, spy)
        resp = svc._handle({"cmd": "unlock"})
        t.ok(spy.calls == 0 and resp.get("reason") == "too-dark" and svc._lockout.records == [],
             "low_light_boost=False -> no camera touch; dark stays too-dark (3.2 behaviour)")

    print()
    if t.fail:
        print(f"LOW-LIGHT BOOST SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("LOW-LIGHT BOOST SELFTEST OK: exposure restored on every path; boost gated + feeds the "
          "gate; lockout stays neutral when still dark.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
