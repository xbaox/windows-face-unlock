"""tools/camera_policy_selftest.py -- Stage 9 (act 9b R10) camera and presence policy (no camera).

  [1] defaults -- persistent_camera=false, auto_lock=false, camera_name="" (+ validation).
  [2] N-16 release policy -- on demand every request closes its capture; the warm hold of a locked
      session keeps it; the unlock edge and the 60 s expiry release it; persistent keeps it.
  [3] camera by NAME -- resolve_index (exact / unique case-insensitive / absent); a name that is
      not connected opens nothing (no index-0 fallback); unlock answers camera-error, presence
      unknown/not-found.
  [4] bounded read (F-144) -- a read that never returns raises CameraReadTimeout within the cap,
      abandons the capture; the service answers camera-error and never hands that capture out.
  [5] presence "unknown" (F-143) -- leased / busy / black / zero frames are unknown with a cause,
      never "present"; a black but streaming capture is not reopened on the presence path (F-145);
      an all-exception detection burst is an error, not an absence (F-133).
  [6] boost restore (F-129) -- AUTO_EXPOSURE is restored with EXPOSURE and read back; a device
      that does not come back disables the boost and drops the capture.
  [7] F-138 -- the heal retry / boost re-capture start only with budget left for one more burst.
  [8] monitor -- unknown or an unheard-of state is no decision; auto_lock off = no camera probe;
      a failed LockWorkStation is not counted (F-149); Pause persists (F-147) and shows "paused".
  [9] remote detection -- SM_REMOTECONTROL counts (F-148); an established connection of an idle
      remote tool does not (F-142).

Run from the repo root:  python -m tools.camera_policy_selftest    Exit 0 = all green.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_campol_")

import numpy as np

from face_service.camera_open import BoundedOpener
from face_service.config import Config


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


class FakeCamera:
    """Stand-in for face_service.camera.Camera. Class knobs drive what the next one does."""
    made: list = []
    next_open = True
    not_found_names: set = set()
    frames = None          # callable(cam) -> frame | None | raises

    def __init__(self, index, warmup, name="", read_cap_s=5.0):
        FakeCamera.made.append(self)
        self.index, self.name, self.read_cap_s = index, name, read_cap_s
        self.not_found = False
        self._cap = None
        self.closed = 0

    def open_fast(self, deadline=None):
        if self.name and self.name in FakeCamera.not_found_names:
            self.not_found = True
            return False
        if FakeCamera.next_open:
            self._cap = object()
            return True
        return False

    def read(self):
        f = FakeCamera.frames
        return f(self) if f else np.full((48, 64, 3), 120, np.uint8)

    def close(self):
        self.closed += 1
        self._cap = None


class _Audit:
    def __init__(self):
        self.records = []

    def write(self, event, rec):
        self.records.append((event, dict(rec)))


class _Lockout:
    def __init__(self):
        self.records = []
        self.store_ok = True

    def remaining(self):
        return 0.0

    def record(self, success):
        self.records.append(success)
        return False


def _svc(SVC, cfg):
    s = SVC.FaceService.__new__(SVC.FaceService)
    s._caller_sid = lambda h: "S-1-5-18"
    s.cfg = cfg
    s._cam = None
    s._cam_lock = threading.Lock()
    s._cam_heal_at = 0.0
    s._opener = BoundedOpener()
    s._lockout = _Lockout()
    s._audit = _Audit()
    s._camera_paused_until = 0.0
    s._gesture_slot = None
    s._report_slot = None
    return s


def _cfg(**over):
    c = Config(language="en")
    c.camera_open_retries = 0
    for k, v in over.items():
        setattr(c, k, v)
    return c


def main(argv=None) -> int:
    t = T()

    # --- 1) defaults ------------------------------------------------------------------------
    print("[1] R10 defaults")
    c = Config()
    t.ok(c.persistent_camera is False and c.auto_lock is False and c.camera_name == "",
         "persistent_camera=false, auto_lock=false, camera_name=''")
    for bad in (3, "x" * 300, "cam\x07"):
        try:
            Config(camera_name=bad).validate()
            t.ok(False, f"camera_name={bad!r:.20} rejected")
        except ValueError:
            t.ok(True, f"camera_name={bad!r:.20} rejected")
    ex = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
    t.ok("persistent_camera = false" in ex and "auto_lock = false" in ex and 'camera_name = ""' in ex,
         "config.example.toml carries the new defaults and camera_name")

    import face_service.service as SVC
    orig_camera = SVC.Camera
    SVC.Camera = FakeCamera
    try:
        # --- 2) N-16 release policy -------------------------------------------------------
        print("\n[2] N-16 camera release policy")
        FakeCamera.made, FakeCamera.next_open, FakeCamera.frames = [], True, None
        s = _svc(SVC, _cfg())
        with s._cam_lock:
            cam, busy = s._acquire_camera()
            s._done_with(cam)
        t.ok(not busy and cam.closed == 1 and s._cam is None,
             "on demand: the capture is closed at the end of the request")
        s._refusal = lambda: None
        was = s._lock_watch_step(True, False)
        t.ok(was is True and s._cam is not None and s._warm_until > time.monotonic(),
             "session locks -> the camera is opened and held warm")
        warm = s._cam
        with s._cam_lock:
            cam2, _ = s._acquire_camera()
            s._done_with(cam2)
        t.ok(cam2 is warm and s._cam is warm and warm.closed == 0,
             "during the warm hold a request reuses the warm capture and leaves it open")
        was = s._lock_watch_step(False, True)
        t.ok(was is False and s._cam is None and warm.closed == 1 and s._warm_until == 0.0,
             "session unlocks -> the camera is released")
        s._lock_watch_step(True, False)
        held = s._cam
        s._warm_until = time.monotonic() - 1           # the 60 s hold ran out, still locked
        s._lock_watch_step(True, True)
        t.ok(s._cam is None and held.closed == 1, "the hold expires after 60 s even while locked")
        t.ok(SVC.CAMERA_WARM_HOLD_S == 60.0, "hold ceiling is 60 s")
        s._lock_watch_step(None, False)
        t.ok(s._cam is None, "a lock probe with no opinion changes nothing")
        s._camera_paused_until = time.monotonic() + 60
        s._lock_watch_step(True, False)
        t.ok(s._cam is None, "no warming while the wizard holds the lease")
        s._camera_paused_until = 0.0
        sp = _svc(SVC, _cfg(persistent_camera=True))
        with sp._cam_lock:
            c3, _ = sp._acquire_camera()
            sp._done_with(c3)
        t.ok(sp._cam is c3 and c3.closed == 0, "persistent_camera=true keeps the capture")

        # --- 3) camera by name --------------------------------------------------------------
        print("\n[3] camera by name (F-141)")
        from face_service.camera_devices import CameraDevice, resolve_index
        devs = [CameraDevice(0, "USB2.0 HD UVC WebCam", r"\\?\usb#1"),
                CameraDevice(1, "OBS Virtual Camera", "")]
        t.ok(resolve_index("OBS Virtual Camera", devs) == 1, "exact name -> its DSHOW index")
        t.ok(resolve_index("usb2.0 hd uvc webcam", devs) == 0, "unique case-insensitive match")
        t.ok(resolve_index("Logitech C920", devs) is None and resolve_index("", devs) is None,
             "absent or empty name -> None")
        from face_service import camera as CAM
        import cv2
        opened = []
        orig_vc, orig_res = cv2.VideoCapture, None
        import face_service.camera_devices as CD
        orig_res = CD.resolve_index
        cv2.VideoCapture = lambda *a: opened.append(a) or (_ for _ in ()).throw(RuntimeError("no"))
        CD.resolve_index = lambda name, devices=None: None
        try:
            real = CAM.Camera(0, 0, name="Logitech C920")
            ok = real.open_fast()
        finally:
            cv2.VideoCapture, CD.resolve_index = orig_vc, orig_res
        t.ok(ok is False and real.not_found and opened == [],
             "a named camera that is not connected opens NOTHING (no index-0 fallback)")
        FakeCamera.not_found_names = {"Logitech C920"}
        s = _svc(SVC, _cfg(camera_name="Logitech C920"))
        r = s._handle({"cmd": "unlock", "v": 2})
        t.ok(r == {"ok": False, "reason": "camera-error"} and s._lockout.records == [],
             "unlock with the named camera missing -> camera-error, lockout-neutral")
        t.ok(s._camera_problem == "camera-not-found", "status carries camera-not-found")
        s._refusal = lambda: None
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "not-found",
             "presence with the camera missing -> unknown / not-found")
        FakeCamera.not_found_names = set()

        # --- 4) bounded read ----------------------------------------------------------------
        print("\n[4] bounded read (F-144)")

        class HangCap:
            released = threading.Event()

            def __init__(self):
                self.gate = threading.Event()

            def read(self):
                self.gate.wait(5)
                return True, None

            def release(self):
                HangCap.released.set()

        cam = CAM.Camera(0, 0, read_cap_s=0.3)
        hc = HangCap()
        cam._cap = hc
        t0 = time.monotonic()
        try:
            cam.read()
            t.ok(False, "a hung read raises CameraReadTimeout")
        except CAM.CameraReadTimeout:
            t.ok(time.monotonic() - t0 < 1.5, "a hung read raises CameraReadTimeout within the cap")
        t.ok(cam._cap is None, "the capture is abandoned (never handed out again)")
        hc.gate.set()
        t.ok(HangCap.released.wait(3), "the abandoned capture is released once the call returns")

        def hang(c):
            raise CAM.CameraReadTimeout("x")
        FakeCamera.frames = hang
        s = _svc(SVC, _cfg())
        s._refusal = lambda: None
        r = s._handle({"cmd": "unlock", "v": 2})
        t.ok(r == {"ok": False, "reason": "camera-error"} and s._lockout.records == [],
             "unlock whose read hangs -> camera-error, lockout-neutral")
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "camera-error",
             "presence whose read hangs -> unknown / camera-error")
        FakeCamera.frames = None
        sp = _svc(SVC, _cfg(persistent_camera=True))
        with sp._cam_lock:
            c4, _ = sp._acquire_camera()
        c4._cap = None                                   # what a hung read leaves behind
        with sp._cam_lock:
            c5, _ = sp._acquire_camera()
        t.ok(c5 is not c4, "a cached capture abandoned by a hung read is never reused")

        # --- 5) presence unknown --------------------------------------------------------------
        print("\n[5] presence unknown (F-143, F-145, F-133)")
        s = _svc(SVC, _cfg())
        s._refusal = lambda: None
        s._camera_paused_until = time.monotonic() + 30
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "leased" and pr.get("present") is False,
             "leased -> unknown / leased (not present)")
        s._camera_paused_until = 0.0
        FakeCamera.next_open = False
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "busy", "busy -> unknown / busy")
        FakeCamera.next_open = True
        FakeCamera.frames = lambda c: np.zeros((48, 64, 3), np.uint8)
        s.recog = type("R", (), {"verify_frame": lambda self, f: (False, 1.0, True)})()
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "black", "black frames -> unknown / black")
        FakeCamera.frames = lambda c: None
        pr = s._handle({"cmd": "presence"})
        t.ok(pr.get("state") == "unknown" and pr.get("why") == "zero-frames",
             "no frames -> unknown / zero-frames")
        sp = _svc(SVC, _cfg(persistent_camera=True))
        with sp._cam_lock:
            cached, _ = sp._acquire_camera()
        dropped = sp._note_camera_health(5, 0.5, "probe-recog")
        t.ok(not dropped and sp._cam is cached, "presence path keeps a black-but-streaming capture (F-145)")
        dropped = sp._note_camera_health(0, None, "probe-recog")
        t.ok(dropped and sp._cam is None, "...and drops it on zero frames")
        FakeCamera.frames = None
        s2 = _svc(SVC, _cfg(presence_mode="detection"))
        s2._refusal = lambda: None
        s2.detector = type("D", (), {"has_face": lambda self, f: (_ for _ in ()).throw(FileNotFoundError("yunet"))})()
        pr = s2._handle({"cmd": "presence"})
        t.ok(pr.get("ok") is False and pr.get("state") == "error",
             "detection mode, detector failing on every frame -> error, not absent (F-133)")

        # --- 6) boost restore --------------------------------------------------------------------
        print("\n[6] boost restore (F-129)")
        from face_service.camera_boost import try_exposure_boost

        class Cap:
            def __init__(self, sticky_manual):
                self.v = {cv2.CAP_PROP_EXPOSURE: -6.0, cv2.CAP_PROP_AUTO_EXPOSURE: 0.75}
                self.sticky = sticky_manual
                self.sets = []

            def get(self, prop):
                return self.v.get(prop, 0.0)

            def set(self, prop, val):
                self.sets.append((prop, val))
                self.v[prop] = val
                if prop == cv2.CAP_PROP_EXPOSURE:
                    self.v[cv2.CAP_PROP_AUTO_EXPOSURE] = 0.25    # the driver switches to manual
                if prop == cv2.CAP_PROP_AUTO_EXPOSURE and self.sticky:
                    self.v[prop] = 0.25                          # ...and refuses to go back
                return True

        good = Cap(False)
        out = try_exposure_boost(good, 2.0, lambda: "rc")
        t.ok(out.applied and out.restored and good.v[cv2.CAP_PROP_AUTO_EXPOSURE] == 0.75,
             "AUTO_EXPOSURE is restored with EXPOSURE and read back")
        t.ok(any(p == cv2.CAP_PROP_AUTO_EXPOSURE for p, _ in good.sets), "the auto mode is written back")
        bad = Cap(True)
        out = try_exposure_boost(bad, 2.0, lambda: "rc")
        t.ok(out.restored is False and out.audit().get("boost_restore_failed") is True,
             "a device that stays manual -> restored=False, audited")
        s = _svc(SVC, _cfg(low_light_boost=True))
        with s._cam_lock:
            pass
        FakeCamera.next_open = True
        orig_boost = SVC.try_exposure_boost
        SVC.try_exposure_boost = lambda cap, step, rc: type(out)(
            True, True, -6.0, -4.0, -4.0, None, restored=False)
        try:
            dark = SVC.VerifyOutcome(False, 1.0, True, {"latency_ms": 100.0}, None, 10.0)
            r, audit = s._maybe_boost(dark)
            t.ok(s._boost_disabled and s._cam is None, "restore failure -> boost off, capture dropped")
            r2, audit2 = s._maybe_boost(dark)
            t.ok(audit2.get("boost_disabled") is True and r2 is dark,
                 "the next dark unlock does not boost again (until restart)")
        finally:
            SVC.try_exposure_boost = orig_boost

        # --- 7) F-138 ------------------------------------------------------------------------------
        print("\n[7] F-138 budget before an extra burst")
        s = _svc(SVC, _cfg())
        r = SVC.VerifyOutcome(False, 1.0, True, {"latency_ms": 3000.0}, None, 10.0)
        s._req_started = time.monotonic()
        s._client_budget_s = None
        t.ok(s._room_for_burst(r, 11.0), "fresh request: room for another 3 s burst")
        s._req_started = time.monotonic() - 8.0
        t.ok(not s._room_for_burst(r, 11.0), "8 s spent: no room for 3 s + margin")
        s._req_started = time.monotonic() - 1.0
        s._client_budget_s = 4.0
        t.ok(not s._room_for_burst(r, 11.0), "the client's own smaller budget counts")
        s._req_started = None
        t.ok(s._room_for_burst(r, 11.0), "no request stamp -> no deadline")
    finally:
        SVC.Camera = orig_camera

    # --- 8) monitor --------------------------------------------------------------------------------
    print("\n[8] presence monitor")
    import presence_monitor.monitor as M
    calls = []
    answers = {"presence": {"ok": True, "state": "unknown", "why": "busy", "present": False}}

    def fake_pipe(req, timeout_s=30.0):
        calls.append(req["cmd"])
        if req["cmd"] == "status":
            return {"ok": True, "lockout": {}}
        return answers["presence"]

    saved = {n: getattr(M, n) for n in ("pipe_call", "_is_session_locked", "is_remote_context",
                                          "_input_idle_seconds", "_lock_workstation",
                                          "_fullscreen_active", "PAUSE_PATH")}
    home = Path(os.environ["FACE_UNLOCK_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    M.PAUSE_PATH = home / "presence_paused.json"
    M.pipe_call = fake_pipe
    M._is_session_locked = lambda: False
    M.is_remote_context = lambda: (False, "")
    M._input_idle_seconds = lambda: None
    M._fullscreen_active = lambda: False
    locks = {"n": 0, "ok": False}

    def fake_lock():
        locks["n"] += 1
        return locks["ok"]
    M._lock_workstation = fake_lock
    try:
        m = M.PresenceMonitor(_cfg(auto_lock=True, presence_absent_strikes=1,
                                   presence_confirm_delay_s=0.0))
        m._tick()
        snap = m.snapshot()
        t.ok(snap["last_result"] == "unknown" and snap["last_why"] == "busy" and snap["strikes"] == 0,
             "unknown -> no decision, cause shown, no strike")
        answers["presence"] = {"ok": True, "state": "sideways", "present": False}
        m._tick()
        t.ok(m.snapshot()["last_result"] == "unknown" and m._strikes == 0,
             "a state the monitor does not know -> no decision")
        answers["presence"] = {"ok": True, "state": "absent", "present": False}
        m._tick()
        t.ok(locks["n"] == 1 and m.snapshot()["lock_count"] == 0
             and m.snapshot()["last_result"] == "error",
             "LockWorkStation returning FALSE is not counted as a lock (F-149)")
        locks["ok"] = True
        m._tick()
        t.ok(m.snapshot()["lock_count"] == 1, "a lock that happened is counted")
        calls.clear()
        m_off = M.PresenceMonitor(_cfg(auto_lock=False))
        m_off._tick()
        t.ok("presence" not in calls and "status" in calls
             and m_off.snapshot()["last_reason"] == "auto-lock-off",
             "auto_lock off -> status poll only, not a single camera probe")
        m.pause()
        t.ok(M.PAUSE_PATH.exists(), "Pause is persisted (F-147)")
        m2 = M.PresenceMonitor(_cfg(auto_lock=True))
        t.ok(m2.is_paused(), "a new monitor (tray restart) starts paused")
        M._input_idle_seconds = lambda: 1.0            # the user is typing
        calls.clear()
        m2._tick()
        t.ok(m2.snapshot()["last_result"] == "skipped" and m2.snapshot()["last_reason"] == "paused"
             and "presence" not in calls, "while paused Status says paused, not 'present src=input'")
        m2.resume()
        t.ok(not M.PAUSE_PATH.exists() and not M.PresenceMonitor(_cfg()).is_paused(),
             "Resume clears the persisted pause")
    finally:
        for n, v in saved.items():
            setattr(M, n, v)

    # --- 9) remote detection ------------------------------------------------------------------------
    print("\n[9] remote detection (F-142, F-148)")
    import presence_monitor.remote_session as RS
    orig = RS.win32api.GetSystemMetrics
    try:
        RS.win32api.GetSystemMetrics = lambda m: 1 if m == 0x2001 else 0
        t.ok(RS.is_rdp_session(), "SM_REMOTECONTROL (console being driven) counts as remote")
        RS.win32api.GetSystemMetrics = lambda m: 0
        t.ok(not RS.is_rdp_session(), "neither metric -> local")
    finally:
        RS.win32api.GetSystemMetrics = orig
    src = Path(RS.__file__).read_text(encoding="utf-8")
    t.ok("net_connections" not in src and "CONN_ESTABLISHED" not in src,
         "an established connection of an idle remote tool no longer counts")
    t.ok("anydesk.exe" not in RS.SESSION_MARKERS and "teamviewer_desktop.exe" in RS.SESSION_MARKERS,
         "only per-connection helpers are markers")

    print()
    if t.fail:
        print(f"CAMERA POLICY SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("CAMERA POLICY SELFTEST OK: on-demand camera with a warm hold only while locked, camera by "
          "name with no fallback, bounded reads, presence 'unknown' instead of 'present', boost "
          "restore verified, burst budget respected, monitor treats unknown as no decision, "
          "persisted pause, checked LockWorkStation, and a stricter remote detector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
