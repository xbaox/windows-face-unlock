"""tools/live9e_selftest.py -- findings of the live 9e run in a VM (9e-f2), the parts that need no
wizard window. No camera, no service process, no pipe.

  [F2-01] the wizard's last line never says "All set" over a failed calibration or a PC that is too
          slow; the head-turn row says calibrated (with the delta) / not calibrated; the service
          reports the current camera's calibration in `status` (additive).
  [F2-03] bring_to_front takes the topmost back at once (never a lasting topmost); the camera's
          process opts out of background throttling and hands it back (SetProcessInformation,
          read back with GetProcessInformation); the preview does not wait for a slow detector
          (the detector on its own thread: detect_due, and a camera session over a synthetic
          camera with a 250 ms detector keeps drawing at the camera loop's pace).
  [F2-05] the speed estimate from synthetic measurements -> the right level, colour and text
          (quick >= 4, slow 2..4, too slow < 2; the 9e VM numbers 1.1-1.6 -> too slow, 2.3-2.5 ->
          slow); attempts before the photo build; seeded from the audit's last records; `status`
          carries it (additive); the tray Status row.
  [F2-07] the CPU build of onnxruntime logs "CUDAExecutionProvider not available" at INFO, the GPU
          build at WARNING; the tray waits quietly (DEBUG) for the service's pipe at start.

Run:  python -m tools.live9e_selftest      Exit 0 = all green.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import testhome  # noqa: E402  (isolation before any product import)
testhome.isolate("faceunlock_live9e_")

import logging  # noqa: E402
import queue  # noqa: E402

import numpy as np  # noqa: E402

from face_service import speed as SP  # noqa: E402
from face_service.i18n import set_language, t  # noqa: E402

FAILS: list = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f"  (got={got!r})"))
    if not cond:
        FAILS.append(name)


class _Rec(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.recs = []

    def emit(self, r):
        self.recs.append((r.levelno, r.getMessage()))


def test_speed():
    print("[F2-05] speed estimate -> level, colour, text")
    for lang in ("en", "ru"):
        set_language(lang)
        for fps, lv, colour in ((1.1, "too-slow", "err"), (1.6, "too-slow", "err"), (1.96, "slow", "warn"),
                                (2.3, "slow", "warn"), (2.5, "slow", "warn"), (3.99, "quick", "ok"),
                                (4.0, "quick", "ok"), (12.0, "quick", "ok")):
            text, c = SP.ready_text({"fps": round(fps, 1), "source": "attempts"})
            check(f"{lang}: {fps} frames/s -> {lv} ({colour})", SP.level(round(fps, 1)) == lv and c == colour
                  and f"{round(fps, 1):.1f}" in text, (SP.level(fps), c, text))
        text, c = SP.ready_text({"fps": None})
        check(f"{lang}: nothing measured -> 'not measured yet', neutral", c == "info"
              and text == t("enroll.ready.speed.unknown"), text)
    set_language("en")
    check("the 9e VM, 4 vCPU (1.1-1.6 frames/s) -> 'too slow ... run out of time'",
          "too slow" in SP.ready_text(SP.estimate([1.1, 1.6, 1.2], None))[0]
          and "run out of time" in SP.ready_text(SP.estimate([1.1, 1.6, 1.2], None))[0])
    check("the 9e VM, 8 vCPU (2.3-2.5 frames/s) -> 'slow (up to ~10 s)'",
          "up to ~10 s" in SP.ready_text(SP.estimate([2.3, 2.5], None))[0])
    e = SP.estimate([], 0.8)
    check("photo build 0.8 s/photo (VM 4 vCPU) -> 1.2 frames/s from 'enroll' -> too slow",
          e == {"fps": 1.2, "source": "enroll", "samples": 1} and SP.level(e["fps"]) == "too-slow", e)
    e = SP.estimate([], 0.5)
    check("photo build 0.5 s/photo (VM 8 vCPU) -> 2.0 frames/s -> slow", e["fps"] == 2.0
          and SP.level(e["fps"]) == "slow", e)
    e = SP.estimate([9.0, 1.0, 2.0, 2.2, 2.4, 2.6, 2.1], 0.1)
    check("real attempts win over the photo build; the median of the last 5", e["source"] == "attempts"
          and e["fps"] == 2.2 and e["samples"] == 5, e)
    check("zero / negative / junk values are ignored", SP.estimate([0, -1, "x", None], None)["fps"] is None)
    check("thresholds 4 and 2 (from the 9e measurements)", SP.FPS_QUICK == 4.0 and SP.FPS_MIN == 2.0)
    fps, s = SP.from_records([
        {"event": "unlock", "fps": 2.3, "faces": 5}, {"event": "unlock", "fps": 9.9, "faces": 0},
        {"event": "gesture_telemetry", "fps": 2.5, "faces": 12}, {"event": "enroll_build", "ok": True,
                                                                  "s_per_image": 0.52},
        {"event": "enroll_build", "ok": False, "s_per_image": 3.0}, {"event": "verify", "fps": 7.0, "faces": 3}])
    check("audit records: rounds with a face, the last good build (a faceless burst reads fast -- skipped)",
          fps == [2.3, 2.5] and s == 0.52, (fps, s))
    # the tray Status row
    check("tray Status: 'about 2.4 frames/s (sign-in attempts) -- slow: up to ~10 s'",
          SP.status_text({"fps": 2.4, "source": "attempts"}) == "about 2.4 frames/s (sign-in attempts) -- slow: up to ~10 s",
          SP.status_text({"fps": 2.4, "source": "attempts"}))
    check("tray Status: from the photo build, too slow",
          "building the face profile" in SP.status_text({"fps": 1.2, "source": "enroll"})
          and "too slow" in SP.status_text({"fps": 1.2, "source": "enroll"}))
    set_language("ru")
    check("ru: tray Status row", SP.status_text({"fps": 3.0, "source": "enroll"}).startswith("около 3.0 кадр/с"),
          SP.status_text({"fps": 3.0, "source": "enroll"}))
    set_language("en")
    from presence_monitor import gui as G
    check("the tray Status window has the Speed row", ("status.speed", "speed") in G.STATUS_ROWS)


def test_service_status():
    print("[F2-05 / F2-01] the service: speed from rounds, builds and the audit; status fields")
    from face_service.audit import AuditLog
    from face_service.config import Config
    from face_service.service import FaceService
    s = FaceService.__new__(FaceService)
    s.cfg = Config()
    s._speed_fps, s._speed_enroll = [], None
    check("nothing measured -> fps None", s._speed() == {"fps": None, "source": None, "samples": 0}, s._speed())

    class _R:
        last_enroll_info = {"images": 15, "accepted": 15}
    s.recog = _R()
    sp = s._note_enroll_speed(7.5)
    check("a build of 15 photos in 7.5 s -> 0.5 s/photo -> 2.0 frames/s ('enroll')",
          sp == 0.5 and s._speed() == {"fps": 2.0, "source": "enroll", "samples": 1}, (sp, s._speed()))
    s._note_speed(9.0, 0)
    check("a round without a face does not count", s._speed()["source"] == "enroll")
    for f in (1.1, 1.6, 1.3):
        s._note_speed(f, 5)
    check("rounds with a face -> 'attempts', median", s._speed() == {"fps": 1.3, "source": "attempts", "samples": 3},
          s._speed())
    with tempfile.TemporaryDirectory() as td:
        a = AuditLog(Path(td) / "audit.jsonl")
        a.write("enroll_build", {"mode": "add", "ok": True, "accepted": 15, "s_per_image": 0.8})
        a.write("unlock", {"outcome": "needs-gesture", "fps": 2.3, "faces": 5})
        a.write("gesture_telemetry", {"fps": 2.5, "faces": 12})
        check("AuditLog.tail + from_records seed the estimate at start",
              SP.estimate(*SP.from_records(a.tail())) == {"fps": 2.4, "source": "attempts", "samples": 2},
              SP.from_records(a.tail()))
        (Path(td) / "audit.jsonl").write_bytes(b"x" * 70000 + b"\n" + b'{"event": "unlock", "fps": 3.0, "faces": 1}\n')
        check("a cut first line and junk are skipped", SP.from_records(a.tail()) == ([3.0], None),
              SP.from_records(a.tail()))
    # the turn calibration of the current camera
    s.cfg = Config(camera_name="Cam A")
    s._calibration = {"cameras": {"name:Cam A": {"left_is_negative_yaw": False, "delta_deg": 55.1}}}
    check("status: the current camera calibrated (delta 55.1)",
          s._turn_calibration() == {"calibrated": True, "delta_deg": 55.1}, s._turn_calibration())
    s.cfg = Config(camera_name="Cam B")
    check("status: another camera -> not calibrated", s._turn_calibration() == {"calibrated": False, "delta_deg": None})
    s._calibration = {}
    check("status: no calibration -> not calibrated", s._turn_calibration()["calibrated"] is False)
    import inspect
    src = inspect.getsource(FaceService._status)
    check("status carries 'speed' and 'turn_calibration' (additive keys)",
          '"speed": self._speed()' in src and '"turn_calibration": self._turn_calibration()' in src)
    usrc = inspect.getsource(FaceService._unlock) + inspect.getsource(FaceService._unlock_gesture)
    check("both phases note their frame rate", usrc.count("self._note_speed(") == 2)
    bsrc = inspect.getsource(FaceService._build_add) + inspect.getsource(FaceService._build_replace)
    check("both builds note and audit their seconds per photo", bsrc.count("_note_enroll_speed(") == 2
          and bsrc.count('"s_per_image"') == 2)


def test_final_line():
    print("[F2-01] the wizard's last line and the head-turn row")
    import presence_monitor.enroll_gui as E
    for lang in ("en", "ru"):
        set_language(lang)
        fail = t("enroll.calib.too_small")
        check(f"{lang}: a failed calibration keeps its own text (never 'All set')",
              E.final_line(True, False, "slow", fail) == (fail, "warn"))
        check(f"{lang}: not calibrated, no failure this session -> a warning with what to do",
              E.final_line(True, False, "quick", None) == (t("enroll.ready.all_ok_uncalibrated"), "warn"))
        check(f"{lang}: calibrated and quick -> All set", E.final_line(True, True, "quick", None)
              == (t("enroll.ready.all_ok"), "ok"))
        check(f"{lang}: too slow -> the speed warning, not All set",
              E.final_line(True, True, "too-slow", None) == (t("enroll.ready.too_slow"), "err"))
        check(f"{lang}: a failed check -> no last line (its row says what to do)",
              E.final_line(False, True, "quick", None) is None)
        check(f"{lang}: turn row 'calibrated (delta +55.1)'", E.turn_text({"calibrated": True, "delta_deg": 55.1})
              == (t("enroll.ready.turn.ok", delta="+55.1"), "ok") and "55.1" in E.turn_text(
                  {"calibrated": True, "delta_deg": 55.1})[0])
        check(f"{lang}: turn row 'not calibrated -- the default direction'",
              E.turn_text({"calibrated": False}) == (t("enroll.ready.turn.bad"), "warn")
              and E.turn_text(None)[1] == "warn")
    set_language("en")
    check("the turn_left step says how far: about 45 degrees, the left shoulder",
          "45°" in t("enroll.calib.turn_left") and "left shoulder" in t("enroll.calib.turn_left"))
    set_language("ru")
    check("ru: ... 'примерно на 45°', 'левое плечо'", "45°" in t("enroll.calib.turn_left")
          and "левое плечо" in t("enroll.calib.turn_left"))
    set_language("en")
    q = t("enroll.calib.offer")
    check("F2-06: the calibration question says what it is, how long, and what No means",
          "sign-in screen" in q and "10 seconds" in q and "No = the default direction" in q
          and "Calibrate head turn" in q, q)
    for key in ("enroll.calib.too_small", "enroll.calib.no_face", "enroll.calib.failed"):
        check(f"a failed calibration says what to do ({key})", "Calibrate head turn" in t(key), t(key))


def test_focus_and_throttling():
    print("[F2-03] focus from Setup, background throttling, the preview's pace")
    import ctypes
    from ctypes import wintypes
    import tkinter as tk
    import presence_monitor.enroll_gui as E
    from presence_monitor.ui import bring_to_front
    root = tk.Tk()
    try:
        root.geometry("200x100+50+50")
        root.update()
        bring_to_front(root)
        root.update()
        check("bring_to_front: the topmost is taken back (no lasting topmost)",
              not bool(root.attributes("-topmost")))
    finally:
        root.destroy()

    class _State(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

    def read_state():
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.GetProcessInformation.restype = wintypes.BOOL
        st = _State(1, 0, 0)
        ok = k32.GetProcessInformation(k32.GetCurrentProcess(), 4, ctypes.byref(st), ctypes.sizeof(st))
        return bool(ok), st.ControlMask, st.StateMask
    off = E.set_power_throttling(False)
    got_off = read_state()
    on = E.set_power_throttling(True)
    got_on = read_state()
    check("camera on: EXECUTION_SPEED throttling switched off (ControlMask 1, StateMask 0)",
          off is True and (not got_off[0] or (got_off[1] & 1 and got_off[2] == 0)), (off, got_off))
    check("camera off: handed back to Windows (ControlMask 0)",
          on is True and (not got_on[0] or got_on[1] == 0), (on, got_on))
    import inspect
    src = inspect.getsource(E.EnrollWindow)
    check("the window switches it off on camera_opened and back on close / failure / camera_closed",
          src.count("self._throttle(False)") == 1 and src.count("self._throttle(True)") >= 3)
    check("the window brings itself to the front at start (bring_to_front)",
          "bring_to_front, self.root" in inspect.getsource(E.EnrollWindow.__init__))
    from presence_monitor import password_gui as PG
    check("the password window too", "bring_to_front(self.root)" in inspect.getsource(PG.PasswordWindow.__init__))
    iss = Path(__file__).resolve().parents[1].joinpath("installer", "installer.iss").read_text(encoding="utf-8-sig")
    runs = [ln for ln in iss.split("[Run]", 1)[1].split("[", 1)[0].splitlines() if ln.startswith("  Check")]
    check("installer: every Finish-page entry grants the foreground first (BeforeInstall: AllowForeground)",
          len(runs) == 4 and all("BeforeInstall: AllowForeground" in ln for ln in runs)
          and "AllowSetForegroundWindow($FFFFFFFF)" in iss, runs)
    # the preview's pace
    check("detect_due: a frame goes to the detector only when it has none in hand",
          E.detect_due(1.2, 1.0, False) and not E.detect_due(1.2, 1.0, True))
    check("detect_due: ... and at most every DETECT_MIN_INTERVAL_S",
          not E.detect_due(1.05, 1.0, False) and E.detect_due(1.1, 1.0, False))

    class _SlowDet:
        unavailable = False
        n = 0

        def _ensure(self, w, h):
            return self

        def detect(self, frame):
            _SlowDet.n += 1
            time.sleep(0.25)                 # a slow CPU (the 9e VM measured ~0.3 s per frame)
            return None, None

    class _Cap:
        def read(self):
            time.sleep(0.01)
            return True, np.full((480, 640, 3), 110, np.uint8)

        def release(self):
            pass
    try:
        import importlib
        importlib.import_module("insightface.utils.face_align")   # the session warms it too
    except Exception:
        pass
    saved = (E._open_capture, E.FaceDetector)
    E._open_capture = lambda cfg, name: (_Cap(), None)
    E.FaceDetector = _SlowDet
    stamps = []
    try:
        from face_service.config import Config
        s = E.Session(Config(), "")
        q: queue.Queue = queue.Queue()
        th = threading.Thread(target=E.camera_worker, args=(s, q, None, None), daemon=True)
        th.start()
        t_end = time.monotonic() + 3.0
        while time.monotonic() < t_end:
            try:
                m = q.get(timeout=0.05)
            except queue.Empty:
                continue
            if m[0] == "frame":
                stamps.append(time.monotonic())
        s.stop.set()
        th.join(5)
    finally:
        E._open_capture, E.FaceDetector = saved
    steady = [x for x in stamps if x >= (stamps[0] + 0.5 if stamps else 0)]
    fps = (len(steady) - 1) / (steady[-1] - steady[0]) if len(steady) > 2 else 0.0
    gaps = [b - a for a, b in zip(steady, steady[1:])]
    check(f"a 250 ms detector: the preview keeps the camera loop's pace ({fps:.1f} frames/s drawn, "
          f"{_SlowDet.n} detections; the old in-line loop drew under 6)", fps >= 12.0 and _SlowDet.n >= 4,
          (round(fps, 1), _SlowDet.n))
    check("... with no detector-sized stall between two drawn frames",
          gaps and max(gaps) < 0.2, round(max(gaps), 3) if gaps else None)
    check("the detector thread ends with the session", not th.is_alive()
          and not any(t.name == "enroll-detect" and t.is_alive() for t in threading.enumerate()))


def test_logs():
    print("[F2-07] log noise")
    from face_service import recognizer as R
    rec = _Rec()
    R.log.addHandler(rec)
    R.log.setLevel(logging.DEBUG)
    try:
        class _Ort:
            def __init__(self, dev):
                self.dev = dev

            def get_available_providers(self):
                return ["AzureExecutionProvider", "CPUExecutionProvider"]

            def get_device(self):
                return self.dev
        rec.recs.clear()
        prov, ctx = R._select_providers(_Ort("CPU"))
        check("CPU build: 'CUDAExecutionProvider not available' at INFO, no WARNING",
              prov == ["CPUExecutionProvider"] and ctx == -1
              and any(lv == logging.INFO and "not available" in m for lv, m in rec.recs)
              and not any(lv >= logging.WARNING for lv, m in rec.recs), rec.recs)
        rec.recs.clear()
        R._select_providers(_Ort("GPU"))
        check("GPU build without CUDA: still a WARNING",
              any(lv == logging.WARNING and "not available" in m for lv, m in rec.recs), rec.recs)
    finally:
        R.log.removeHandler(rec)
    from presence_monitor import monitor as M
    rec = _Rec()
    M.log.addHandler(rec)
    M.log.setLevel(logging.DEBUG)
    try:
        clock = {"t": 0.0}
        answers = [(None, "no-pipe"), (None, "no-pipe"), (None, "reply-timeout"), ({"ok": True, "pong": True}, None)]
        calls = []

        def ex(req, timeout):
            calls.append((req.get("cmd"), timeout))
            clock["t"] += 1.0
            return answers.pop(0)

        class _Stop(threading.Event):
            def wait(self, timeout=None):
                clock["t"] += float(timeout or 0)
                return self.is_set()
        waited = M.wait_for_service(_Stop(), _exchange=ex, _clock=lambda: clock["t"])
        check("the tray waits for the service: no-pipe, no-pipe, reply-timeout, then pong -> answered",
              waited is not None and len(calls) == 4 and all(c[0] == "ping" for c in calls), (waited, calls))
        check("... quietly: nothing above DEBUG while waiting",
              not any(lv >= logging.INFO for lv, m in rec.recs), rec.recs)
        clock["t"] = 0.0
        calls.clear()
        never = lambda req, timeout: (clock.__setitem__("t", clock["t"] + 1.0), (None, "no-pipe"))[1]
        check("a service that never answers: gives up after ~30 s (then the first tick warns as before)",
              M.wait_for_service(_Stop(), _exchange=never, _clock=lambda: clock["t"]) is None
              and 30.0 <= clock["t"] <= 33.0, clock["t"])
        stop = _Stop()
        stop.set()
        check("a stop during the wait ends it at once", M.wait_for_service(stop, _exchange=never) is None)
        import inspect
        check("the presence loop waits before its first tick",
              inspect.getsource(M.PresenceMonitor.run).index("wait_for_service(self._stop)")
              < inspect.getsource(M.PresenceMonitor.run).index("self._tick()"))
        check("the wait is ~30 s with pauses", M.STARTUP_PIPE_WAIT_S == 30.0 and M.STARTUP_PIPE_RETRY_S == 2.0)
    finally:
        M.log.removeHandler(rec)


def main() -> int:
    set_language("en")
    test_speed()
    test_service_status()
    test_final_line()
    test_focus_and_throttling()
    test_logs()
    print()
    if FAILS:
        print(f"LIVE-9E SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("LIVE-9E SELFTEST OK: honest calibration end state, speed estimate and warning, focus and "
          "throttling, preview pace, quiet logs at start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
