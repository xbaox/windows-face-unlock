"""tools/wizard_selftest.py -- the setup wizard against a fake pipe and a synthetic camera (9d, 9d-r2).

No service, no real camera, no face: the camera is a generator of plain grey frames, the pipe a
recorder with canned answers. What it pins:
  [1] A-4 / V-30: the camera lease is taken BEFORE the camera is opened -- a refused lease opens
      nothing; the lease goes back with the device; no usable frame within 5 s -> camera_failed;
      Retry takes a fresh lease and opens again.
  [2] V-42: no lease or pipe call in the window's own (Tk-thread) methods.
  [3] V-34: a new install picks the camera at DirectShow index 0 BY NAME and saves it after the
      first successful build; an explicit camera_index keeps the "(older setting)" entry.
  [4] V-41: Enter in the Replace / Add / Cancel dialog is Add (on the wizard's own root).
  [5] V-31: calibration has Cancel; 30 s (here 1.5 s) without progress abandons it with a clear
      text, closes the camera and gives the lease back; Close during a calibration closes.
  [6] V-43: Start has the focus, Enter presses the focused button, the preview follows the DPI.
  9d-r2:
  [W-10] the preview is sized so the whole window fits the work area at 1920x1080 100/125/150/
         175 % and 1366x768 100 % (pure geometry over the window's measured chrome); the window
         is moved back into the work area after a layout change.
  [W-11] the lease is counted per holder (a session can only give back its own share); a new
         camera session does not start while the old camera thread lives ("switching").
  [W-12] a preview frame queued before camera_closed is not drawn.
  [W-13] the lease is asked for only after the service answered; a lease request without an
         answer is followed by resume_camera; Retry then works (the installer's Finish-page
         wizard while the service is still warming up).
  [W-14] with the camera off, Build takes its own lease for the build; Calibrate says to turn the
         camera on first.
  [W-15] an exception in the camera session (the calibration shots too) -> camera_failed; the 5 s
         first-frame limit is kept on the Tk side (a read() that blocks cannot hide it).
  [W-18] the readiness check says "camera handed back" only after the camera thread has ended.
  [W-19] a failed build shows words, the raw reason goes to the log only.
  [W-20] dialog wrap width follows the DPI; the window width does not change when the optional
         button appears; no "(older setting)" when there is no camera at index 0.
  9e-0:
  [X-01] two camera changes while the first camera thread hangs in read(): the newest worker
         waits for EVERY earlier camera thread still alive ("switching"), never two devices open.

Run:  python -m tools.wizard_selftest [--shots DIR [--lang en|ru]]
With --shots the window is captured (PNG) at each state -- the frames are synthetic, no face.
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tools import testhome  # noqa: E402  (isolation before any product import)
testhome.isolate("faceunlock_wizard_")

import argparse  # noqa: E402
import inspect  # noqa: E402
import logging  # noqa: E402
import queue  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import tkinter as tk  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from face_service import camera_devices as CD  # noqa: E402
from face_service.config import CONFIG_PATH, ENROLL_DIR, Config  # noqa: E402
from face_service.i18n import set_language, t  # noqa: E402
import presence_monitor.enroll_gui as E  # noqa: E402

FAILS: list = []
CALLS: list = []          # ("pipe", cmd) / ("open", name) / ("release",) in the order they happened
ERRORS: list = []         # messagebox.showerror texts
LOGS: list = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f"  (got={got!r})"))
    if not cond:
        FAILS.append(name)


class FakePipe:
    def __init__(self):
        self.replies = {"ping": {"ok": True, "pong": True, "state": "serving"},
                        "pause_camera": {"ok": True}, "resume_camera": {"ok": True},
                        "status": {"ok": True, "state": "serving", "data_dir_secure": True,
                                   "enrollment": True},
                        "build_enrollment": {"ok": True, "count": 12, "other_person": 2},
                        "reload_config": {"ok": True}}
        self.script: dict = {}            # cmd -> list of one-shot replies (None = no answer)
        self.threads: list = []
        self.lock = threading.Lock()

    def __call__(self, req, timeout_s=30.0):
        cmd = req.get("cmd")
        with self.lock:
            CALLS.append(("pipe", cmd))
            self.threads.append(threading.current_thread().name)
            queued = self.script.get(cmd)
            if queued:
                return queued.pop(0)
        if cmd == "calibrate_turn":
            time.sleep(60)                    # never answers in time: the stall watch decides
        return self.replies.get(cmd, {"ok": True})


BLOCK = threading.Event()
RELEASE_DELAY = {"s": 0.0}


class FakeCap:
    """A camera that sends plain grey frames (or black ones), no face; "raise" fails its read;
    "block" hangs in read() until BLOCK is set."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.n = 0

    def read(self):
        if self.mode == "block":
            BLOCK.wait(30)
            return False, None
        time.sleep(0.03)
        self.n += 1
        if self.mode == "raise":
            raise RuntimeError("synthetic camera fault")
        if self.mode == "black":
            return True, np.zeros((480, 640, 3), np.uint8)
        f = np.full((480, 640, 3), 110, np.uint8)
        f[:, :, 0] = np.linspace(80, 140, 640, dtype=np.uint8)[None, :]
        return True, f

    def release(self):
        if RELEASE_DELAY["s"]:
            time.sleep(RELEASE_DELAY["s"])
        with E.pipe_call.lock:
            CALLS.append(("release",))


OPEN_MODE = {"mode": "ok"}


def fake_open(cfg, name):
    with E.pipe_call.lock:
        CALLS.append(("open", name))
    if OPEN_MODE["mode"] == "not-found":
        return None, "not-found"
    return FakeCap(OPEN_MODE["mode"]), None


class _NoDetector:
    unavailable = False

    def _ensure(self, w, h):
        return None


def _idx(entry, start=0):
    for i in range(start, len(CALLS)):
        if CALLS[i] == entry or (isinstance(entry, str) and CALLS[i][0] == entry):
            return i
    return -1


class _LogSpy(logging.Handler):
    def emit(self, rec):
        LOGS.append(rec.getMessage())


def install_fakes(pipe: FakePipe) -> None:
    E.pipe_call = pipe
    E._open_capture = fake_open
    E.FaceDetector = _NoDetector
    CD.list_video_devices = lambda: [CD.CameraDevice(0, "Synthetic Camera", ""),
                                     CD.CameraDevice(1, "Other Camera", "")]
    E.messagebox.askyesno = lambda *a, **k: False       # no calibration offer, no dialogs
    E.messagebox.showinfo = lambda *a, **k: None
    E.messagebox.showerror = lambda *a, **k: ERRORS.append(a[1] if len(a) > 1 else k.get("message"))
    E.messagebox.showwarning = lambda *a, **k: None
    E.log.addHandler(_LogSpy(level=logging.DEBUG))
    E.log.setLevel(logging.DEBUG)


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_worker() -> None:
    print("[1] A-4 / V-30: lease before the camera, back with the device, 5 s first frame, retry")
    q: queue.Queue = queue.Queue()
    pipe = E.pipe_call
    # refused lease -> nothing is opened
    pipe.replies["pause_camera"] = {"ok": False, "reason": "busy"}
    CALLS.clear()
    s = E.Session(Config(), "Synthetic Camera")
    lease = E.LeaseKeeper()
    E.camera_worker(s, q, lease)
    check("a refused lease -> camera_failed 'lease', the camera is never opened",
          q.get_nowait() == ("camera_failed", "lease", 0) and _idx("open") == -1, CALLS)
    pipe.replies["pause_camera"] = {"ok": True}
    # only black frames -> no-frame after FIRST_FRAME_S, the lease is given back
    saved = E.FIRST_FRAME_S
    E.FIRST_FRAME_S = 0.4
    try:
        OPEN_MODE["mode"] = "black"
        CALLS.clear()
        s = E.Session(Config(), "Synthetic Camera")
        E.camera_worker(s, q, lease)
        msgs = _drain(q)
        check("no usable frame within the first-frame limit -> camera_failed 'no-frame'",
              ("camera_failed", "no-frame", 0) in msgs and ("first_frame", 0) not in msgs, msgs)
        a, o, rel, res = (_idx(("pipe", "pause_camera")), _idx("open"), _idx(("release",)),
                          _idx(("pipe", "resume_camera")))
        check("order: pause_camera -> open -> device released -> resume_camera",
              0 <= a < o < rel < res, CALLS)
        check("the lease is not held after the device is gone", not lease.held)
        # retry: a fresh lease and a fresh open
        OPEN_MODE["mode"] = "not-found"
        CALLS.clear()
        E.camera_worker(E.Session(Config(), "Synthetic Camera"), q, lease)
        check("Retry takes a fresh lease before opening (and gives it back on a failed open)",
              _idx(("pipe", "pause_camera")) < _idx("open") < _idx(("pipe", "resume_camera")), CALLS)
        check("a camera that is not connected -> camera_failed 'not-found'",
              q.get_nowait() == ("camera_failed", "not-found", 0))
    finally:
        E.FIRST_FRAME_S = saved
        OPEN_MODE["mode"] = "ok"

    print("[2] V-42: the window's methods make no lease or pipe call")
    src = inspect.getsource(E.EnrollWindow)
    check("EnrollWindow: no lease.acquire(), lease.release() or pipe_call( on the Tk thread",
          "lease.acquire(" not in src and "lease.release(" not in src and "pipe_call(" not in src)

    print("[3] V-34: the default camera by name")
    devs = CD.list_video_devices()
    check("new install -> the DirectShow index 0 camera, by name",
          E.default_camera_name(devs, explicit_index=False) == "Synthetic Camera")
    check("an explicit camera_index keeps the old setting (no default name)",
          E.default_camera_name(devs, explicit_index=True) == "")
    tmp = CONFIG_PATH.with_name("cfg_probe.toml")
    tmp.write_text("camera_index = 1\n", encoding="utf-8")
    ok1 = E.camera_index_explicit(tmp)
    tmp.write_text('language = "en"\n', encoding="utf-8")
    ok2 = E.camera_index_explicit(tmp)
    tmp.unlink()
    check("camera_index_explicit reads config.toml (present / absent)", ok1 is True and ok2 is False)
    from face_service import config as C
    check("W-16: camera_index_explicit lives in face_service.config (the wizard re-uses it)",
          E.camera_index_explicit is C.camera_index_explicit)


def _max_open(calls) -> int:
    """The most camera devices open at once in a CALLS trace (a fake open always succeeds here)."""
    n = peak = 0
    for c in calls:
        if c[0] == "open":
            n += 1
            peak = max(peak, n)
        elif c == ("release",):
            n -= 1
    return peak


def test_x01(lease) -> None:
    print("[X-01] two camera changes while the first camera thread hangs in read(): one device open")

    class _Win:                                   # just what EnrollWindow._start_camera touches
        def __init__(self):
            self.cfg = Config()
            self.cfg.camera_name = "Synthetic Camera"
            self.q: queue.Queue = queue.Queue()
            self.lease = lease
            self.service_up = threading.Event()
            self.service_up.set()
            self.preview_size = (E.PREVIEW_W, E.PREVIEW_H)
            self.session = None
            self.cam_thread = None
            self.cam_threads = []
            self._gen = 10
            self.cam_gen = 10
            self.camera_ok = False

        def _set_extra(self, *_a):
            pass

        def _refresh_buttons(self):
            pass

    w = _Win()
    start = E.EnrollWindow._start_camera
    CALLS.clear()
    BLOCK.clear()
    OPEN_MODE["mode"] = "block"                   # the first device hangs in its native read()
    try:
        start(w)
        t1 = w.cam_thread
        deadline = time.monotonic() + 3
        while ("open", "Synthetic Camera") not in CALLS and time.monotonic() < deadline:
            time.sleep(0.02)
        OPEN_MODE["mode"] = "ok"
        w.cfg.camera_name = "Other Camera"        # change 1: its worker waits for t1
        start(w)
        time.sleep(0.4)
        w.cfg.camera_name = "Synthetic Camera"    # change 2: must wait for t1 too, not only for t2
        start(w)
        kept = t1 in w.cam_threads and w.cam_thread in w.cam_threads
        time.sleep(1.0)
        msgs = _drain(w.q)
        check("X-01: while the hung first camera thread lives, no second device is opened",
              t1.is_alive() and CALLS.count(("open", "Synthetic Camera")) == 1
              and ("open", "Other Camera") not in CALLS, CALLS)
        check("X-01: the newest session says 'switching camera' meanwhile",
              ("camera_switching", w.cam_gen) in msgs, msgs)
        BLOCK.set()                               # the hung read() returns; the first device goes
        deadline = time.monotonic() + 5
        while CALLS.count(("open", "Synthetic Camera")) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        first = _idx(("open", "Synthetic Camera"))
        check("X-01: the newest camera opens only after the first device was released",
              0 <= first < _idx(("release",)) < _idx(("open", "Synthetic Camera"), first + 1), CALLS)
        check("X-01: never more than one device open at a time", _max_open(CALLS) <= 1, CALLS)
        check("X-01: the window keeps every camera thread still alive, the hung one included "
              "(Close and the readiness check wait for all)", kept, w.cam_threads)
    finally:
        OPEN_MODE["mode"] = "ok"
        if w.session is not None:
            w.session.stop.set()
        for t in list(getattr(w, "cam_threads", []) or []) + [w.cam_thread]:
            if t is not None:
                t.join(3)
        BLOCK.clear()
    check("X-01: every camera thread ended and the lease is back", not lease.held
          and not any(t.is_alive() for t in w.cam_threads + [w.cam_thread] if t is not None))


def test_r2_workers() -> None:
    print("[W-11] the lease is counted per holder; one camera session at a time")
    pipe = E.pipe_call
    CALLS.clear()
    lease = E.LeaseKeeper()
    a = lease.acquire("camera")
    b = lease.acquire("build")
    check("W-11: the first holder asks the service, the second does not",
          a is not None and b is not None and CALLS.count(("pipe", "pause_camera")) == 1, CALLS)
    check("W-11: the old session gives back its share -> the lease stays (no resume_camera)",
          lease.release(a) is True and lease.held and ("pipe", "resume_camera") not in CALLS)
    check("W-11: ... and cannot give it back twice (the new holder's share is untouched)",
          lease.release(a) is False and lease.held and ("pipe", "resume_camera") not in CALLS)
    check("W-11: the last share back -> resume_camera", lease.release(b) is True and not lease.held
          and CALLS.count(("pipe", "resume_camera")) == 1, CALLS)

    q: queue.Queue = queue.Queue()
    up = threading.Event()
    up.set()
    CALLS.clear()
    RELEASE_DELAY["s"] = 0.6                        # the old device takes its time to let go
    try:
        s1 = E.Session(Config(), "Synthetic Camera", gen=1, service_up=up)
        t1 = threading.Thread(target=E.camera_worker, args=(s1, q, lease), daemon=True)
        t1.start()
        time.sleep(0.5)
        s2 = E.Session(Config(), "Other Camera", gen=2, service_up=up)
        s1.stop.set()
        t2 = threading.Thread(target=E.camera_worker, args=(s2, q, lease, t1), daemon=True)
        t2.start()
        time.sleep(0.3)
        msgs = _drain(q)
        check("W-11: while the old camera thread lives, the new session says 'switching' and "
              "opens nothing", ("camera_switching", 2) in msgs
              and ("open", "Other Camera") not in CALLS, (msgs[-3:], CALLS))
        time.sleep(1.2)
        rel = _idx(("release",))
        check("W-11: the new camera opens only after the old device was released",
              0 <= rel < _idx(("open", "Other Camera")), CALLS)
        check("W-11: the new session holds the lease", lease.held)
        s2.stop.set()
        t2.join(3)
        check("W-11: ... and gives it back when it ends", not lease.held and not t2.is_alive())
    finally:
        RELEASE_DELAY["s"] = 0.0

    test_x01(lease)

    print("[W-13] the lease only after the service answered; a lease request without answer")
    CALLS.clear()
    down = threading.Event()
    s = E.Session(Config(), "Synthetic Camera", gen=3, service_up=down)
    th13 = threading.Thread(target=E.camera_worker, args=(s, q, lease), daemon=True)
    th13.start()
    time.sleep(0.6)
    check("W-13: no lease request (and no open) before the service answered",
          ("pipe", "pause_camera") not in CALLS and _idx("open") == -1, CALLS)
    pipe.script["pause_camera"] = [None]            # the request times out
    down.set()
    th13.join(3)
    msgs = _drain(q)
    p_i, r_i = _idx(("pipe", "pause_camera")), _idx(("pipe", "resume_camera"))
    check("W-13: a lease request without an answer is followed by resume_camera",
          0 <= p_i < r_i and _idx("open") == -1, CALLS)
    check("W-13: ... and reported as camera_failed 'lease'", ("camera_failed", "lease", 3) in msgs, msgs)
    check("W-13: ... nothing held", not lease.held)

    print("[W-15] a fault in the camera session is camera_failed")
    CALLS.clear()
    OPEN_MODE["mode"] = "raise"
    try:
        s = E.Session(Config(), "Synthetic Camera", gen=4)
        E.camera_worker(s, q, lease)
        msgs = _drain(q)
        check("W-15: a read() that raises -> camera_failed 'error', the lease back",
              ("camera_failed", "error", 4) in msgs and not lease.held
              and _idx(("release",)) >= 0, (msgs, CALLS))
    finally:
        OPEN_MODE["mode"] = "ok"

    class _FaceDet:
        unavailable = False

        def _ensure(self, w, h):
            class _D:
                def detect(self, frame):
                    return None, [np.array([200, 120, 240, 240] + [0.0] * 10 + [0.99], np.float32)]
            return _D()
    saved = (E.FaceDetector, E.evaluate_coach, E.imio.imwrite)
    E.FaceDetector = _FaceDet
    E.evaluate_coach = lambda *a, **k: E.CoachState("enroll.status.ready", True, "ok")

    def _boom(*_a, **_k):
        raise OSError("synthetic disk fault")
    E.imio.imwrite = _boom
    try:
        s = E.Session(Config(), "Synthetic Camera", gen=5)
        s.calib = ("frontal", 3)
        th = threading.Thread(target=E.camera_worker, args=(s, q, lease), daemon=True)
        th.start()
        th.join(5)
        msgs = _drain(q)
        check("W-15: a fault while saving a CALIBRATION shot -> camera_failed 'error'",
              ("camera_failed", "error", 5) in msgs and not th.is_alive() and not lease.held,
              [m for m in msgs if m[0] != "frame"])
    finally:
        E.FaceDetector, E.evaluate_coach, E.imio.imwrite = saved

    print("[W-18] 'camera handed back' only after the camera thread ended")
    saved_join = E.CAMERA_JOIN_S
    E.CAMERA_JOIN_S = 0.3
    try:
        stuck = threading.Event()
        cam = threading.Thread(target=stuck.wait, args=(5,), daemon=True)
        cam.start()
        E.release_and_check_worker(q, E.Session(Config(), "x", gen=6), cam, E.LeaseKeeper())
        msgs = _drain(q)
        ready = [m for m in msgs if m[0] == "readiness"]
        check("W-18: a camera thread that has not ended -> the camera check is NOT ok",
              ("camera_closed", 6) in msgs and ready and ready[0][1]["camera"] is False, msgs)
        stuck.set()
        cam.join(2)
        E.release_and_check_worker(q, E.Session(Config(), "x", gen=7), cam, E.LeaseKeeper())
        ready = [m for m in _drain(q) if m[0] == "readiness"]
        check("W-18: an ended camera thread and no lease share -> 'handed back'",
              ready and ready[0][1]["camera"] is True, ready)
    finally:
        E.CAMERA_JOIN_S = saved_join

    print("[W-19] a failed build in words")
    check("W-19: no answer -> the timeout text", E.build_failure_text("timeout") == t("enroll.build.fail.timeout")
          and E.build_failure_text(None) == t("enroll.build.fail.timeout"))
    check("W-19: a refusal reason -> its words", E.build_failure_text("custody")
          == t("enroll.build.fail.refusing", why=t("why.custody")))
    raw = r"[Errno 13] Permission denied: 'C:\Users\someone\.face-unlock\embeddings.npz'"
    txt = E.build_failure_text(raw)
    check("W-19: anything else -> the generic text, never the raw code",
          txt == t("enroll.build.fail.generic") and "Errno" not in txt and "Users" not in txt, txt)


def test_geometry(chrome_logical: "tuple[float, float] | None") -> None:
    print("[W-10] the wizard fits the work area (pure geometry)")
    from presence_monitor.ui import clamp_window
    # the taskbar is 48 logical px; the chrome (everything but the preview) scales with the DPI
    configs = [("1920x1080 @100%", 1920, 1080, 1.00), ("1920x1080 @125%", 1920, 1080, 1.25),
               ("1920x1080 @150%", 1920, 1080, 1.50), ("1920x1080 @175%", 1920, 1080, 1.75),
               ("1366x768 @100%", 1366, 768, 1.00)]
    models = [("a conservative model chrome 700x400", (700.0, 400.0))]
    if chrome_logical is not None:
        models.append((f"the chrome measured here ({chrome_logical[0]:.0f}x{chrome_logical[1]:.0f})",
                       chrome_logical))
    for label, (cw, ch) in models:
        for name, sw, sh, s in configs:
            work_w, work_h = sw, sh - int(48 * s)
            chrome_w, chrome_h = int(cw * s), int(ch * s)
            pw, ph = E.fit_preview(work_w, work_h, chrome_w, chrome_h, s)
            fits = chrome_h + ph <= work_h and chrome_w + pw <= work_w
            check(f"W-10: {name}, {label}: preview {pw}x{ph} -> window "
                  f"{chrome_w + pw}x{chrome_h + ph} in {work_w}x{work_h}",
                  fits and ph <= work_h * E.PREVIEW_MAX_FRAC + 1 and abs(pw * 3 - ph * 4) <= 4
                  and ph >= E.PREVIEW_H * s * 0.4, (pw, ph))
    check("W-10: a roomy screen keeps the full 96-dpi preview at 100 %",
          E.fit_preview(2560, 1392, 600, 400, 1.0) == (E.PREVIEW_W, E.PREVIEW_H))
    check("W-10: clamp_window pulls a window below the taskbar back up and a left-off one right",
          clamp_window(100, 900, 800, 400, (0, 0, 1920, 1032)) == (100, 632)
          and clamp_window(-300, 10, 800, 400, (0, 0, 1920, 1032)) == (0, 10))
    check("W-10: a window larger than the work area keeps its top-left edge visible",
          clamp_window(500, 500, 3000, 2000, (0, 0, 1920, 1032)) == (0, 0))


def _print_window(root):
    """The window as it renders itself (PrintWindow, PW_RENDERFULLCONTENT) -- works while the
    display is off, when a screen grab fails. The whole frame, title bar included."""
    import ctypes
    import win32gui  # type: ignore
    import win32ui  # type: ignore
    from PIL import Image
    hwnd = int(root.wm_frame(), 16)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bottom - top
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        if not ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2):
            raise OSError("PrintWindow failed")
        img = Image.frombuffer("RGB", (w, h), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    return img


def shot(root, shots: "Path | None", name: str) -> None:
    if shots is None:
        return
    try:
        root.attributes("-topmost", True)
        root.update()
        time.sleep(0.3)
        root.update()
        try:
            from PIL import ImageGrab
            x, y = root.winfo_rootx(), root.winfo_rooty()
            w, h = root.winfo_width(), root.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(shots / f"{name}.png")
        except OSError:
            _print_window(root).save(shots / f"{name}.png")
        root.attributes("-topmost", False)
    except Exception as e:
        print(f"  (screenshot {name} failed: {e!r})")


def test_window(shots: "Path | None") -> "tuple[float, float] | None":
    # a clean home: no photos, no profile (a runner may reuse the folder of an earlier run)
    import shutil
    from face_service.config import EMBED_PATH
    from presence_monitor.ui import px, window_rect, work_area_of
    shutil.rmtree(ENROLL_DIR, ignore_errors=True)
    EMBED_PATH.unlink(missing_ok=True)

    print("[5]/[6] + 9d-r2: the window")
    CALLS.clear()
    ERRORS.clear()
    E.pipe_call.threads.clear()      # [1] called the worker directly on this thread on purpose
    # W-13: the installer's Finish page starts the wizard while the service is still warming up:
    # the first ping gets no answer, the first lease request times out
    E.pipe_call.script = {"ping": [None], "pause_camera": [None]}
    saved = (E.CALIB_STALL_S, E.FIRST_FRAME_S)
    E.CALIB_STALL_S = 1.5
    E.FIRST_FRAME_S = 1.0
    w = E.EnrollWindow()
    res: dict = {}
    steps: list = []

    def at(delay_ms, fn):
        steps.append((delay_ms, fn))

    def s_warm():
        first_ok_ping = [i for i, c in enumerate(CALLS) if c == ("pipe", "ping")]
        p_i, r_i = _idx(("pipe", "pause_camera")), _idx(("pipe", "resume_camera"))
        res["warm"] = (len(first_ok_ping) >= 2 and p_i > first_ok_ping[1] and p_i < r_i,
                       w.line.cget("text") == t("enroll.error.lease"), w._extra_mode == "retry",
                       _idx("open") == -1)
        shot(w.root, shots, "wizard-warmup-lease")
        w._on_retry()

    def s_mode():
        # [4] V-41, on the wizard's own root (one Tk per process, as in the product)
        def press():
            for wdg in w.root.winfo_children():
                if isinstance(wdg, tk.Toplevel):
                    if shots is not None:
                        wdg.update()
                        shot(wdg, shots, "wizard-mode-dialog")
                    stack = list(wdg.winfo_children())
                    while stack:
                        c = stack.pop()
                        stack.extend(c.winfo_children())
                        if c.winfo_class() == "TLabel" and str(c.cget("wraplength")) not in ("", "0"):
                            res["wrap"] = int(str(c.cget("wraplength")))
                    wdg.event_generate("<Return>")
        w.root.after(400, press)
        res["wrap_want"] = px(w.root, 420)
        res["mode"] = E.ask_mode(w.root)

    def s_ready():
        res["ready"] = (w.camera_ok, w.lease.held, _idx(("pipe", "pause_camera")) < _idx("open"))
        res["focus"] = w.root.focus_lastfor() is w.start_btn
        res["preview"] = w.preview_size[1] <= px(w.root, E.PREVIEW_H) and \
            abs(w.preview_size[0] * 3 - w.preview_size[1] * 4) <= 4
        res["camera_name"] = w.cfg.camera_name
        res["left_w0"] = w.left.winfo_width()
        res["chrome"] = (w.chrome[0] / w.scale, w.chrome[1] / w.scale)
        shot(w.root, shots, "wizard-idle")
        w._on_enter()                              # Enter -> Start (focused / default)
        res["armed"] = bool(w.session and w.session.armed.is_set())
        shot(w.root, shots, "wizard-capturing")
        w._on_start_stop()                         # Stop again

    def s_stale():
        # W-12: a frame queued before camera_closed must not be drawn (the worker keeps sending)
        gen = w.session.gen
        ph, pw = w.preview_size[1], w.preview_size[0]
        w.q.put(("frame", np.full((ph, pw, 3), 255, np.uint8), gen))
        w.q.put(("camera_closed", gen))

    def s_stale2():
        res["stale"] = (w._preview_blank, w.cam_gen is None, w._extra_mode == "camera_on")
        w._on_retry()

    def s_calib():
        w._on_calibrate()
        w.root.update()
        res["cancel_shown"] = (w._extra_mode == "cancel_calib" and bool(w.extra_btn.winfo_ismapped())
                               and str(w.extra_btn.cget("text")) == t("enroll.btn.cancel_calib"))
        res["left_w1"] = w.left.winfo_width()
        shot(w.root, shots, "wizard-calibrating")

    def s_stalled():
        res["stalled"] = (w.calibrating, w.line.cget("text") == t("enroll.calib.stalled"))

    def s_closed():
        res["closed"] = (w.lease.held, w._extra_mode == "camera_on",
                         ("pipe", "resume_camera") in CALLS, bool(w.ready_frm.winfo_ismapped()))
        res["cam_ready_line"] = w.ready_lines["camera"].cget("text")
        shot(w.root, shots, "wizard-ready")
        rect, work = window_rect(w.root), work_area_of(w.root)
        res["fits_now"] = (rect, work)
        # W-14: calibrate with the camera off -> a clear text; build with the camera off works
        w._on_calibrate()
        res["calib_off"] = w.line.cget("text") == t("enroll.calib.camera_off")
        shot(w.root, shots, "wizard-calib-camera-off")
        ENROLL_DIR.mkdir(parents=True, exist_ok=True)
        (ENROLL_DIR / "enroll_0.jpg").write_bytes(b"synthetic")
        E.pipe_call.script["build_enrollment"] = [
            {"ok": False, "reason": r"[Errno 13] Permission denied: 'C:\Users\someone\x.npz'"}]
        res["calls_before_build_off"] = len(CALLS)
        w._on_build()

    def s_built_off():
        later = CALLS[res["calls_before_build_off"]:]
        pa, bu = later.index(("pipe", "pause_camera")) if ("pipe", "pause_camera") in later else -1, \
            later.index(("pipe", "build_enrollment")) if ("pipe", "build_enrollment") in later else -1
        re_ = max((i for i, c in enumerate(later) if c == ("pipe", "resume_camera")), default=-1)
        res["build_off"] = (0 <= pa < bu < re_, not w.lease.held, _idx("open", res["calls_before_build_off"]) == -1)
        res["build_err"] = (list(ERRORS), w.line.cget("text"))
        shot(w.root, shots, "wizard-build-failed")
        res["calls_before_retry"] = len(CALLS)
        w._on_retry()

    def s_again():
        later = CALLS[res["calls_before_retry"]:]
        res["retry"] = (w.camera_ok, w.lease.held, ("pipe", "pause_camera") in later and
                        later.index(("pipe", "pause_camera")) < later.index(next(c for c in later
                                                                                  if c[0] == "open")))
        w._on_calibrate()
        w.extra_btn.invoke()
        res["cancelled"] = (w.calibrating, w.line.cget("text") == t("enroll.calib.cancelled"))

    def s_build():
        w._on_retry()

    def s_build2():
        (ENROLL_DIR / "enroll_1.jpg").write_bytes(b"synthetic")
        w._on_build()

    def s_built():
        res["built_text"] = w.line.cget("text")
        res["cfg_text"] = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
        shot(w.root, shots, "wizard-built")
        w._on_retry()

    def s_switch():
        # W-11 in the window: switch the camera while it is live
        RELEASE_DELAY["s"] = 0.8
        res["calls_before_switch"] = len(CALLS)
        w.cam_var.set("Other Camera")
        w._on_camera_chosen()

    def s_switching():
        res["switching_line"] = w.line.cget("text")
        shot(w.root, shots, "wizard-switching")

    def s_switched():
        RELEASE_DELAY["s"] = 0.0
        later = CALLS[res["calls_before_switch"]:]
        rel = later.index(("release",)) if ("release",) in later else -1
        op = later.index(("open", "Other Camera")) if ("open", "Other Camera") in later else -1
        res["switch"] = (0 <= rel < op, w.camera_ok, w.lease.held)
        w.cam_var.set("Synthetic Camera")
        w._on_camera_chosen()

    def s_block():
        # W-15: a read() that blocks -- the 5 s (here 1 s) limit is kept by the Tk side
        OPEN_MODE["mode"] = "block"
        BLOCK.clear()
        w._on_retry()

    def s_blocked():
        res["blocked"] = (w.line.cget("text") == t("enroll.error.no_frame"), w._extra_mode == "retry",
                          w.cam_thread.is_alive())
        shot(w.root, shots, "wizard-no-frame")
        OPEN_MODE["mode"] = "ok"
        BLOCK.set()
        w._on_retry()

    def s_notfound():
        # W-20: no camera at DirectShow index 0 and no camera_index of its own -> "Camera not found"
        saved_list, saved_name = CD.list_video_devices, w.cfg.camera_name
        CD.list_video_devices = lambda: []
        w.cfg.camera_name = ""
        w._load_devices()
        res["notfound"] = w.cam_var.get()
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        CONFIG_PATH.write_text(raw + "camera_index = 1\n", encoding="utf-8")
        w._load_devices()
        res["older"] = w.cam_var.get()
        CONFIG_PATH.write_text(raw, encoding="utf-8")
        CD.list_video_devices = saved_list
        w.cfg.camera_name = saved_name
        w._load_devices()
        w._pending_camera_save = ""
        # W-10: push the window half below the work area; the next layout change brings it back
        work = work_area_of(w.root)
        w.root.geometry(f"+{work[0] + 40}+{work[3] - 120}")
        w.root.update()
        res["pushed"] = window_rect(w.root)
        w._set_line("layout change", "info")

    def s_clamped():
        res["clamped"] = (window_rect(w.root), work_area_of(w.root))

    def s_close():
        w._on_calibrate()
        res["resume_before_close"] = CALLS.count(("pipe", "resume_camera"))
        w._on_close()

    at(3500, s_warm)
    at(2500, s_mode)
    at(300, s_ready)
    at(300, s_stale)
    at(400, s_stale2)
    at(2500, s_calib)
    at(3200, s_stalled)
    at(2500, s_closed)
    at(2500, s_built_off)
    at(3000, s_again)
    at(2500, s_build)
    at(2500, s_build2)
    at(3000, s_built)
    at(2500, s_switch)
    at(300, s_switching)
    at(2500, s_switched)
    at(2500, s_block)
    at(3500, s_blocked)
    at(2500, s_notfound)
    at(600, s_clamped)
    at(300, s_close)

    def run_steps(i=0):
        if i >= len(steps):
            return
        delay, fn = steps[i]

        def go():
            try:
                fn()
            except Exception as e:
                import traceback
                traceback.print_exc()
                res.setdefault("errors", []).append(f"{fn.__name__}: {e!r}")
            run_steps(i + 1)
        w.root.after(delay, go)
    run_steps()
    watchdog = threading.Timer(150.0, lambda: w.root.after(0, w.root.destroy))
    watchdog.start()
    w.run()
    watchdog.cancel()
    BLOCK.set()
    E.CALIB_STALL_S, E.FIRST_FRAME_S = saved
    check("no step raised", not res.get("errors"), res.get("errors"))
    check("W-13: warming service -> lease asked only after the service answered; the timed-out "
          "request followed by resume_camera; the lease text and Retry shown; nothing opened",
          res.get("warm") == (True, True, True, True), res.get("warm"))
    check("W-13: ... and Retry then works (camera on, lease held)", res.get("ready", (0,))[:2] == (True, True),
          res.get("ready"))
    check("V-41: Enter in Replace / Add / Cancel -> 'add' (the non-destructive choice)",
          res.get("mode") == "add", res.get("mode"))
    check("W-20: the dialog's wrap width follows the DPI (420 logical px)",
          res.get("wrap") is not None and res.get("wrap") == res.get("wrap_want"),
          res.get("wrap"))
    check("A-4: camera on, lease held, the lease came before the open", res.get("ready") == (True, True, True),
          res.get("ready"))
    check("V-34: new install -> the camera at index 0 chosen by name", res.get("camera_name") == "Synthetic Camera",
          res.get("camera_name"))
    check("V-43: Start has the focus when the window opens", res.get("focus") is True)
    check("V-43 / W-10: the preview is sized for the DPI and the work area (4:3, at most the 96-dpi size)",
          res.get("preview") is True)
    check("V-43: Enter pressed Start (capture armed)", res.get("armed") is True)
    check("W-12: a frame queued before camera_closed is not drawn (preview blank, camera off)",
          res.get("stale") == (True, True, True), res.get("stale"))
    check("V-31: calibration shows its Cancel button (the one optional slot)", res.get("cancel_shown") is True)
    check("W-20: the button row keeps its width when the optional button appears",
          res.get("left_w0") == res.get("left_w1"), (res.get("left_w0"), res.get("left_w1")))
    check("V-31: no progress for the stall limit -> abandoned with the stall text",
          res.get("stalled") == (False, True), res.get("stalled"))
    check("V-31 / A-4: after it the camera is closed, the lease given back, 'Turn the camera on "
          "again' offered, the readiness panel shown", res.get("closed") == (False, True, True, True),
          res.get("closed"))
    check("W-18: ... the readiness line says the camera is handed back (thread ended)",
          res.get("cam_ready_line", "").startswith("✓"), res.get("cam_ready_line"))
    rect, work = res.get("fits_now", (None, None))
    check("W-10: with the readiness panel shown the whole window is inside the work area",
          rect is not None and rect[0] >= work[0] - 8 and rect[1] >= work[1] and rect[2] <= work[2] + 8
          and rect[3] <= work[3] + 8, (rect, work))
    check("W-14: Calibrate with the camera off says to turn it on first", res.get("calib_off") is True)
    check("W-14: Build with the camera off takes its own lease for the build (pause -> build -> "
          "resume), opens no camera", res.get("build_off") == (True, True, True), res.get("build_off"))
    errs, line = res.get("build_err", ([], ""))
    check("W-19: a failed build shows words in the dialog and the line, never the raw code",
          errs and errs[-1] == t("enroll.build.fail.generic") and line == errs[-1]
          and not any("Errno" in e or "Users" in e for e in errs), res.get("build_err"))
    check("W-19: ... the raw code is in the log", any("Errno 13" in m for m in LOGS))
    check("A-4: 'Turn the camera on again' takes a fresh lease before the open",
          res.get("retry") == (True, True, True), res.get("retry"))
    check("V-31: Cancel ends the calibration with its own text", res.get("cancelled") == (False, True),
          res.get("cancelled"))
    check("V-20 in the window: the build line names the photos of another person",
          "different person" in res.get("built_text", "") or "другим человеком" in res.get("built_text", ""),
          res.get("built_text"))
    check("V-34: the default camera name is saved after the successful build",
          'camera_name = "Synthetic Camera"' in res.get("cfg_text", ""), res.get("cfg_text"))
    check("W-11: switching the camera says so while the old one lets go",
          res.get("switching_line") == t("enroll.status.switching"), res.get("switching_line"))
    check("W-11: ... the new camera opens only after the old device was released; lease held",
          res.get("switch") == (True, True, True), res.get("switch"))
    check("W-15: a read() that blocks -> 'no picture within 5 s' from the Tk side (worker still "
          "blocked), Retry offered", res.get("blocked") == (True, True, True), res.get("blocked"))
    check("W-20: no camera at index 0 on a new install -> 'Camera not found', no '(older setting)'",
          res.get("notfound") == t("enroll.camera.not_found"), res.get("notfound"))
    check("W-20: an explicit camera_index still shows '(older setting)'",
          res.get("older") == t("settings.camera.by_index", i=0), res.get("older"))
    pushed = res.get("pushed")
    rect, work = res.get("clamped", (None, None))
    check("W-10: a window pushed below the work area is moved back inside on the next layout change",
          pushed is not None and pushed[3] > work[3] and rect is not None and rect[3] <= work[3]
          and rect[1] >= work[1], (pushed, rect, work))
    check("V-31 / V-42: Close during a calibration closes; the lease is released by a worker",
          CALLS.count(("pipe", "resume_camera")) > res.get("resume_before_close", 10**6)
          and not w.lease.held and (w.cam_thread is None or not w.cam_thread.is_alive()))
    tk_thread = threading.main_thread().name
    check("V-42: every pipe call of the run was made off the Tk thread",
          tk_thread not in E.pipe_call.threads, [n for n in E.pipe_call.threads if n == tk_thread])
    return res.get("chrome")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None)
    ap.add_argument("--lang", default="en")
    a = ap.parse_args(argv)
    set_language(a.lang)
    Config(language=a.lang).save(keys=["language"])
    shots = Path(a.shots) if a.shots else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    from presence_monitor.ui import enable_dpi_awareness
    enable_dpi_awareness()
    install_fakes(FakePipe())
    test_worker()
    test_r2_workers()
    chrome = test_window(shots)
    test_geometry(chrome)
    print()
    if FAILS:
        print(f"WIZARD SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("WIZARD SELFTEST OK: the lease comes before the camera and goes with it, counted per "
          "holder, asked for only once the service answers; one camera session at a time; stale "
          "frames dropped; 5 s to a first frame kept by the Tk side; Build works with the camera "
          "off; failures in words; the window fits the work area; Close always closes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
