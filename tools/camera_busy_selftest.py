"""tools/camera_busy_selftest.py -- Stage 3 / Step 4 busy-camera proof (no camera).

Covers the busy-camera handling:
  [1] open_with_retry -- the pure bounded loop: opens immediately / after k tries / never;
      a raised attempt is retried; the wall-clock timeout stops it early (never hangs); retries=0
      means a single attempt.
  [2] _acquire_camera -- BUG (1) half-open fixed: a failed open leaves self._cam None (not a stuck
      half-open handle), a later open retries fresh and can succeed.
  [3] unlock handler -- BUG (2) reason fixed: a busy webcam yields reason "camera-busy" (NOT
      "exception: Cannot open camera..."), LOCKOUT-NEUTRAL (record untouched), audited; and a
      second busy unlock re-detects busy instead of AssertionError-ing on a broken camera.
  [4] presence -- busy returns benign ("present", True) (no absence strikes), verify_frame untouched.
  [5] lease != busy -- a leased unlock returns "no-match" and never even constructs a camera.
  [6] Config.validate -- camera_open_retries / camera_open_timeout_s bounds fail loud.
  [7] BoundedOpener (7b-2) -- one attempt is waited on for at most cap_s; blowing the ceiling
      returns at once and does NOT retry; the abandoned attempt closes its own capture when it
      lands late; a second open is refused while that worker is alive, and works again after.

No camera / no GPU. Run from the repo root:
    python -m tools.camera_busy_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service.camera_open import BoundedOpener, open_with_retry
from face_service.config import Config


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


class FakeTime:
    """Injected clock/sleep so open_with_retry runs without real time. sleep advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


class OpenFn:
    """Fake open_fn: fails until call #succeed_at (then True); optionally raises on the first call."""

    def __init__(self, succeed_at=None, raise_first=False):
        self.calls = 0
        self.succeed_at = succeed_at
        self.raise_first = raise_first

    def __call__(self):
        self.calls += 1
        if self.raise_first and self.calls == 1:
            raise RuntimeError("driver hiccup")
        if self.succeed_at is not None and self.calls >= self.succeed_at:
            return True
        return False


def main(argv=None) -> int:
    t = T()

    # --- 1) open_with_retry: the pure bounded loop -----------------------------------------
    print("[1] open_with_retry")
    ft = FakeTime()
    f = OpenFn(succeed_at=1)
    t.ok(open_with_retry(f, retries=2, pause_s=0.3, timeout_s=9e9, clock=ft.clock, sleep=ft.sleep)
         and f.calls == 1, "opens on first attempt -> True, 1 call")

    f = OpenFn(succeed_at=3)
    t.ok(open_with_retry(f, retries=5, pause_s=0.0, timeout_s=9e9, clock=ft.clock, sleep=ft.sleep)
         and f.calls == 3, "opens on the 3rd attempt -> True, 3 calls")

    f = OpenFn(succeed_at=None)   # never
    ft = FakeTime()
    got = open_with_retry(f, retries=2, pause_s=0.0, timeout_s=9e9, clock=ft.clock, sleep=ft.sleep)
    t.ok(got is False and f.calls == 3, "never opens (retries=2) -> False, exactly 3 attempts")

    f = OpenFn(succeed_at=2, raise_first=True)
    t.ok(open_with_retry(f, retries=3, pause_s=0.0, timeout_s=9e9, clock=ft.clock, sleep=ft.sleep)
         and f.calls == 2, "a raised attempt is retried (not propagated) -> opens on the 2nd")

    f = OpenFn(succeed_at=None)
    ft = FakeTime()   # pause 1.0 advances clock; timeout 2.5 -> stops after ~3 attempts, never hangs
    got = open_with_retry(f, retries=100, pause_s=1.0, timeout_s=2.5, clock=ft.clock, sleep=ft.sleep)
    t.ok(got is False and f.calls <= 4, f"wall-clock timeout stops the loop early ({f.calls} attempts, not 101)")

    f = OpenFn(succeed_at=None)
    t.ok(open_with_retry(f, retries=0, pause_s=0.3, timeout_s=9e9, clock=ft.clock, sleep=ft.sleep) is False
         and f.calls == 1, "retries=0 -> a single attempt")

    # --- service-level tests (real handler; only Camera stubbed) ----------------------------
    try:
        import face_service.service as SVC
        from face_service.service import FaceService, VerifyOutcome  # noqa: F401
    except Exception as e:   # pragma: no cover - pywin32 absent in this context
        print(f"\n  skip  service import unavailable ({e.__class__.__name__}); [2]-[5] skipped")
    else:
        class FakeCamera:
            """Stand-in for face_service.camera.Camera. open_fast() returns FakeCamera.next_open;
            'made' counts constructions so a test can assert no open was even attempted (lease)."""
            made = 0
            next_open = True

            def __init__(self, index, warmup):
                FakeCamera.made += 1
                self.index = index
                self.warmup = warmup
                self._cap = None

            def open_fast(self, deadline=None):
                # Signature mirrors the real one since 7b-2: BoundedOpener always passes a
                # deadline. The fake ignores it -- it never blocks, so it can never blow a cap.
                if FakeCamera.next_open:
                    self._cap = object()
                    return True
                return False

            def read(self):
                return None

            def close(self):
                self._cap = None

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

        def _svc(cfg):
            # __new__ bypasses __init__, so this mirrors the __init__ state that the paths under
            # test actually touch: a field added there has to be added here too, or the first case
            # that reaches it fails with AttributeError instead of testing anything.
            s = FaceService.__new__(FaceService)
            s.cfg = cfg
            s._cam = None
            s._cam_lock = threading.Lock()
            s._cam_heal_at = 0.0
            s._opener = BoundedOpener()
            s._lockout = _LockoutSpy()
            s._audit = _AuditStub()
            s._camera_paused_until = 0.0
            return s

        def _cfg():
            c = Config()
            c.camera_open_retries = 0   # single fast attempt -> no real sleeps in the tests
            # This harness calls _handle({"cmd":"unlock"}) with NO pipe handle, so the
            # Stage-5 SID gate resolves the client SID to None and refuses with
            # "not-authorized" before the camera-busy branch is ever reached. The gate
            # is not what these cases exercise -- same reason and same shape as
            # tools/pipe_hardening_selftest.py:155.
            c.pipe_unlock_require_system = False
            return c

        orig_camera = SVC.Camera
        SVC.Camera = FakeCamera
        try:
            # --- 2) _acquire_camera: half-open fixed (bug 1) --------------------------------
            print("\n[2] _acquire_camera -- half-open fixed (bug 1)")
            FakeCamera.made = 0
            FakeCamera.next_open = False
            s = _svc(_cfg())
            cam, busy = s._acquire_camera()
            t.ok(cam is None and busy is True, "busy device -> (None, True)")
            t.ok(s._cam is None, "self._cam stays None on failure (no stuck half-open handle)")
            cam2, busy2 = s._acquire_camera()
            t.ok(busy2 is True and s._cam is None and FakeCamera.made == 2,
                 "next acquire retries a FRESH camera (not a broken cached one)")
            FakeCamera.next_open = True
            cam3, busy3 = s._acquire_camera()
            t.ok(busy3 is False and cam3 is not None and s._cam is cam3,
                 "device frees up -> opens and persists self._cam only now")

            # --- 3) unlock: reason "camera-busy" (bug 2) + lockout-neutral + re-detect -------
            print("\n[3] unlock handler -- reason 'camera-busy' (bug 2), neutral, re-detects")
            FakeCamera.made = 0
            FakeCamera.next_open = False
            s = _svc(_cfg())
            resp = s._handle({"cmd": "unlock"})
            t.ok(resp.get("reason") == "camera-busy" and resp.get("ok") is False,
                 "busy unlock -> reason 'camera-busy' (NOT 'exception: Cannot open camera...')")
            t.ok(s._lockout.records == [], "camera-busy does NOT touch the lockout counter (neutral)")
            ev, rec = s._audit.records[-1]
            t.ok(ev == "unlock" and rec.get("outcome") == "camera-busy",
                 "audit records outcome 'camera-busy'")
            t.ok(s._cam is None, "after a busy unlock self._cam is None (no stuck handle)")
            resp2 = s._handle({"cmd": "unlock"})
            t.ok(resp2.get("reason") == "camera-busy" and s._lockout.records == [],
                 "2nd busy unlock RE-DETECTS busy (no AssertionError on a broken camera)")

            # --- 4) presence: busy -> benign ("present", True) ------------------------------
            # 7c-6 turned the probe's return into (state, real); busy still means "do not punish
            # the user for a camera we cannot open", which is now spelled "present".
            print("\n[4] presence -- busy -> ('present', True), no strikes")
            FakeCamera.next_open = False
            s = _svc(_cfg())
            t.ok(s._presence_probe_recognition() == ("present", True),
                 "recognition presence on busy -> ('present', True) (don't punish when blind)")
            s = _svc(_cfg())
            t.ok(s._presence_probe_detection() == ("present", True),
                 "detection presence on busy -> ('present', True)")

            # --- 5) lease != busy -----------------------------------------------------------
            print("\n[5] enrollment lease is NOT camera-busy")
            FakeCamera.made = 0
            FakeCamera.next_open = False   # would be busy IF an open were attempted
            s = _svc(_cfg())
            s._camera_paused_until = 1e18  # our own enrollment holds the camera
            resp = s._handle({"cmd": "unlock"})
            t.ok(resp.get("reason") == "no-match" and s._lockout.records == [],
                 "leased unlock -> 'no-match' (existing path), lockout untouched -- NOT 'camera-busy'")
            t.ok(FakeCamera.made == 0, "leased path never even constructs / opens a camera")
        finally:
            SVC.Camera = orig_camera

    # --- 6) Config.validate: new fields -----------------------------------------------------
    print("\n[6] Config.validate camera_open_retries / camera_open_timeout_s")
    t.ok(Config().camera_open_retries == 2 and Config().camera_open_timeout_s == 3.0,
         "defaults: retries=2, timeout=3.0")

    def _cv(**over):
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

    t.ok(not _raises(_cv().validate), "defaults validate")
    t.ok(not _raises(_cv(camera_open_retries=0).validate), "retries=0 validates")
    t.ok(not _raises(_cv(camera_open_retries=10).validate), "retries=10 validates (upper bound)")
    t.ok(_raises(_cv(camera_open_retries=-1).validate), "retries=-1 -> fail-loud")
    t.ok(_raises(_cv(camera_open_retries=11).validate), "retries=11 -> fail-loud")
    t.ok(_raises(_cv(camera_open_retries=2.5).validate), "retries non-int -> fail-loud")
    t.ok(_raises(_cv(camera_open_retries=True).validate), "retries bool -> fail-loud")
    t.ok(not _raises(_cv(camera_open_timeout_s=0.001).validate), "timeout 0.001 validates")
    t.ok(not _raises(_cv(camera_open_timeout_s=30.0).validate), "timeout 30 validates (upper bound)")
    t.ok(_raises(_cv(camera_open_timeout_s=0.0).validate), "timeout 0 -> fail-loud")
    t.ok(_raises(_cv(camera_open_timeout_s=-1.0).validate), "timeout -1 -> fail-loud")
    t.ok(_raises(_cv(camera_open_timeout_s=30.1).validate), "timeout 30.1 -> fail-loud")

    # --- 7) BoundedOpener: a hard ceiling on ONE attempt (7b-2, still no camera) -------------
    print("\n[7] BoundedOpener -- hard wait ceiling on a single open attempt")

    class FakeOpen:
        """Scripted open_fn/close_fn pair. Records every call, and can sleep so that an attempt
        outlives the cap -- which is the whole point being tested. No camera, no cv2."""

        def __init__(self, results, sleep_s=0.0):
            self.results = list(results)
            self.sleep_s = sleep_s
            self.calls = 0
            self.deadlines = []
            self.closes = 0

        def open_fn(self, deadline):
            self.calls += 1
            self.deadlines.append(deadline)
            if self.sleep_s:
                time.sleep(self.sleep_s)
            return self.results.pop(0) if self.results else False

        def close_fn(self):
            self.closes += 1

    op = BoundedOpener()

    f = FakeOpen([True])
    t.ok(op.open(open_fn=f.open_fn, close_fn=f.close_fn, retries=2, pause_s=0.0,
                 timeout_s=9e9, cap_s=1.0) is True and f.calls == 1,
         "opens on the first attempt -> True, exactly 1 open_fn call")
    t.ok(len(f.deadlines) == 1, "open_fn is handed a deadline")
    t.ok(f.closes == 0, "a completed attempt is never closed behind the caller's back")

    f = FakeOpen([False, False, False])
    t.ok(op.open(open_fn=f.open_fn, close_fn=f.close_fn, retries=2, pause_s=0.0,
                 timeout_s=9e9, cap_s=1.0) is False and f.calls == 3,
         "never opens (retries=2) -> False, exactly 3 attempts (same shape as open_with_retry)")
    t.ok(len(f.deadlines) == 3
         and all(f.deadlines[i] <= f.deadlines[i + 1] for i in range(2)),
         "every attempt gets its own, non-decreasing deadline")
    t.ok(f.closes == 0, "no cap breach -> close_fn never called")

    # An attempt that outlives the ceiling: return at once, do NOT retry, reclaim it later.
    wedged = FakeOpen([True], sleep_s=0.4)
    t0 = time.monotonic()
    got = op.open(open_fn=wedged.open_fn, close_fn=wedged.close_fn, retries=3, pause_s=0.0,
                  timeout_s=9e9, cap_s=0.05)
    waited = time.monotonic() - t0
    t.ok(got is False and wedged.calls == 1,
         f"attempt outliving the cap -> False and NO retry ({wedged.calls} call, retries=3)")
    t.ok(waited < 0.25,
         f"returns at the ceiling, not when the wedged attempt finishes ({waited:.2f}s of 0.4s)")

    blocked = FakeOpen([True])
    t.ok(op.open(open_fn=blocked.open_fn, close_fn=blocked.close_fn, retries=0, pause_s=0.0,
                 timeout_s=9e9, cap_s=1.0) is False and blocked.calls == 0,
         "a second open while that worker is still in flight -> False, its open_fn NOT called")

    time.sleep(0.5)   # let the abandoned attempt finish and run its own cleanup
    t.ok(wedged.closes == 1,
         "the abandoned attempt closed its capture exactly once, from its own thread")

    again = FakeOpen([True])
    t.ok(op.open(open_fn=again.open_fn, close_fn=again.close_fn, retries=0, pause_s=0.0,
                 timeout_s=9e9, cap_s=1.0) is True and again.calls == 1,
         "once the wedged worker has exited, the next open works again")
    t.ok(again.closes == 0, "and that clean open is not closed behind the caller's back")

    # Bounds for the knob this section exercises. Same shape as [6]'s checks, kept here rather
    # than added to [6] so that section stays exactly as it was.
    t.ok(_raises(_cv(camera_open_attempt_cap_s=0.0).validate), "cap 0 -> fail-loud")
    t.ok(_raises(_cv(camera_open_attempt_cap_s=-1.0).validate), "cap -1 -> fail-loud")
    t.ok(not _raises(_cv(camera_open_attempt_cap_s=60.0).validate),
         "cap 60 validates (upper bound)")
    t.ok(_raises(_cv(camera_open_attempt_cap_s=60.1).validate), "cap 60.1 -> fail-loud")

    print()
    if t.fail:
        print(f"CAMERA-BUSY SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("CAMERA-BUSY SELFTEST OK: the open loop is attempt-bounded AND each attempt is "
          "wait-capped (wedged -> no retry, late capture reclaimed, no second attempt in "
          "flight); busy -> clean 'camera-busy' (no exception, no half-open, lockout-neutral); "
          "lease stays distinct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
