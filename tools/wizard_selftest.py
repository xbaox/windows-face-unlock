"""tools/wizard_selftest.py -- the setup wizard against a fake pipe and a synthetic camera (9d).

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
        self.threads: list = []

    def __call__(self, req, timeout_s=30.0):
        cmd = req.get("cmd")
        CALLS.append(("pipe", cmd))
        self.threads.append(threading.current_thread().name)
        if cmd == "calibrate_turn":
            time.sleep(60)                    # never answers in time: the stall watch decides
        return self.replies.get(cmd, {"ok": True})


class FakeCap:
    """A camera that sends plain grey frames (or black ones), no face."""

    def __init__(self, black=False):
        self.black = black
        self.n = 0

    def read(self):
        time.sleep(0.03)
        self.n += 1
        if self.black:
            return True, np.zeros((480, 640, 3), np.uint8)
        f = np.full((480, 640, 3), 110, np.uint8)
        f[:, :, 0] = np.linspace(80, 140, 640, dtype=np.uint8)[None, :]
        return True, f

    def release(self):
        CALLS.append(("release",))


OPEN_MODE = {"mode": "ok"}


def fake_open(cfg, name):
    CALLS.append(("open", name))
    if OPEN_MODE["mode"] == "not-found":
        return None, "not-found"
    return FakeCap(black=OPEN_MODE["mode"] == "black"), None


class _NoDetector:
    unavailable = False

    def _ensure(self, w, h):
        return None


def _idx(entry, start=0):
    for i in range(start, len(CALLS)):
        if CALLS[i] == entry or (isinstance(entry, str) and CALLS[i][0] == entry):
            return i
    return -1


def install_fakes(pipe: FakePipe) -> None:
    E.pipe_call = pipe
    E._open_capture = fake_open
    E.FaceDetector = _NoDetector
    CD.list_video_devices = lambda: [CD.CameraDevice(0, "Synthetic Camera", ""),
                                     CD.CameraDevice(1, "Other Camera", "")]
    E.messagebox.askyesno = lambda *a, **k: False       # no calibration offer, no dialogs
    E.messagebox.showinfo = lambda *a, **k: None
    E.messagebox.showerror = lambda *a, **k: None
    E.messagebox.showwarning = lambda *a, **k: None


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
          q.get_nowait() == ("camera_failed", "lease") and _idx("open") == -1, CALLS)
    pipe.replies["pause_camera"] = {"ok": True}
    # only black frames -> no-frame after FIRST_FRAME_S, the lease is given back
    saved = E.FIRST_FRAME_S
    E.FIRST_FRAME_S = 0.4
    try:
        OPEN_MODE["mode"] = "black"
        CALLS.clear()
        s = E.Session(Config(), "Synthetic Camera")
        E.camera_worker(s, q, lease)
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
        check("no usable frame within the first-frame limit -> camera_failed 'no-frame'",
              ("camera_failed", "no-frame") in msgs and ("first_frame",) not in msgs, msgs)
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
              q.get_nowait() == ("camera_failed", "not-found"))
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


def shot(root, shots: "Path | None", name: str) -> None:
    if shots is None:
        return
    try:
        from PIL import ImageGrab
        root.attributes("-topmost", True)
        root.update()
        time.sleep(0.3)
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(shots / f"{name}.png")
        root.attributes("-topmost", False)
    except Exception as e:
        print(f"  (screenshot {name} failed: {e!r})")


def test_window(shots: "Path | None") -> None:
    # a clean home: no photos, no profile (a runner may reuse the folder of an earlier run)
    import shutil
    from face_service.config import EMBED_PATH
    shutil.rmtree(ENROLL_DIR, ignore_errors=True)
    EMBED_PATH.unlink(missing_ok=True)

    print("[5]/[6] the window: lease with the device, calibration Cancel / stall, focus, close")
    CALLS.clear()
    E.pipe_call.threads.clear()      # [1] called the worker directly on this thread on purpose
    saved = E.CALIB_STALL_S
    E.CALIB_STALL_S = 1.5
    w = E.EnrollWindow()
    res: dict = {}
    steps: list = []

    def at(delay_ms, fn):
        steps.append((delay_ms, fn))

    def s_ready():
        res["ready"] = (w.camera_ok, w.lease.held, _idx(("pipe", "pause_camera")) < _idx("open"))
        res["focus"] = w.root.focus_lastfor() is w.start_btn
        from presence_monitor.ui import px
        res["preview"] = w.preview_size == (px(w.root, E.PREVIEW_W), px(w.root, E.PREVIEW_H))
        res["camera_name"] = w.cfg.camera_name
        shot(w.root, shots, "wizard-idle")
        w._on_enter()                              # Enter -> Start (focused / default)
        res["armed"] = bool(w.session and w.session.armed.is_set())
        shot(w.root, shots, "wizard-capturing")
        w._on_start_stop()                         # Stop again

    def s_calib():
        w._on_calibrate()
        w.root.update()
        res["cancel_shown"] = bool(w.calib_cancel_btn.winfo_ismapped())
        shot(w.root, shots, "wizard-calibrating")

    def s_stalled():
        res["stalled"] = (w.calibrating, w.line.cget("text") == t("enroll.calib.stalled"))

    def s_closed():
        res["closed"] = (w.lease.held, str(w.retry_btn.cget("text")) == t("enroll.btn.camera_on"),
                         ("pipe", "resume_camera") in CALLS, w.ready_frm.winfo_ismapped())
        shot(w.root, shots, "wizard-ready")
        res["calls_before_retry"] = len(CALLS)
        w._on_retry()

    def s_again():
        later = CALLS[res["calls_before_retry"]:]
        res["retry"] = (w.camera_ok, w.lease.held, ("pipe", "pause_camera") in later and
                        later.index(("pipe", "pause_camera")) < later.index(next(c for c in later
                                                                                  if c[0] == "open")))
        w._on_calibrate()
        w.calib_cancel_btn.invoke()
        res["cancelled"] = (w.calibrating, w.line.cget("text") == t("enroll.calib.cancelled"))

    def s_build():
        w._on_retry()

    def s_build2():
        ENROLL_DIR.mkdir(parents=True, exist_ok=True)
        (ENROLL_DIR / "enroll_1.jpg").write_bytes(b"synthetic")
        w._on_build()

    def s_built():
        res["built_text"] = w.line.cget("text")
        shot(w.root, shots, "wizard-built")

    def s_close():
        # a calibration in progress does not keep the window open (V-31)
        w._on_retry()

    def s_close2():
        w._on_calibrate()
        res["resume_before_close"] = CALLS.count(("pipe", "resume_camera"))
        w._on_close()

    def s_mode():
        # [4] V-41, on the wizard's own root (one Tk per process, as in the product)
        def press():
            for wdg in w.root.winfo_children():
                if isinstance(wdg, tk.Toplevel):
                    if shots is not None:
                        wdg.update()
                        shot(wdg, shots, "wizard-mode-dialog")
                    wdg.event_generate("<Return>")
        w.root.after(400, press)
        res["mode"] = E.ask_mode(w.root)

    at(2500, s_mode)
    at(300, s_ready)
    at(800, s_calib)
    at(3200, s_stalled)
    at(2500, s_closed)
    at(3000, s_again)
    at(2500, s_build)
    at(2500, s_build2)
    at(3000, s_built)
    at(2500, s_close)
    at(2500, s_close2)

    def run_steps(i=0):
        if i >= len(steps):
            return
        delay, fn = steps[i]

        def go():
            try:
                fn()
            except Exception as e:
                res.setdefault("errors", []).append(f"{fn.__name__}: {e!r}")
            run_steps(i + 1)
        w.root.after(delay, go)
    run_steps()
    watchdog = threading.Timer(90.0, lambda: w.root.after(0, w.root.destroy))
    watchdog.start()
    w.run()
    watchdog.cancel()
    E.CALIB_STALL_S = saved
    check("no step raised", not res.get("errors"), res.get("errors"))
    check("V-41: Enter in Replace / Add / Cancel -> 'add' (the non-destructive choice)",
          res.get("mode") == "add", res.get("mode"))
    check("A-4: camera on, lease held, the lease came before the open", res.get("ready") == (True, True, True),
          res.get("ready"))
    check("V-34: new install -> the camera at index 0 chosen by name", res.get("camera_name") == "Synthetic Camera",
          res.get("camera_name"))
    check("V-43: Start has the focus when the window opens", res.get("focus") is True)
    check("V-43: the preview is sized for the window's DPI", res.get("preview") is True)
    check("V-43: Enter pressed Start (capture armed)", res.get("armed") is True)
    check("V-31: calibration shows its Cancel button", res.get("cancel_shown") is True)
    check("V-31: no progress for the stall limit -> abandoned with the stall text",
          res.get("stalled") == (False, True), res.get("stalled"))
    check("V-31 / A-4: after it the camera is closed, the lease given back, 'Turn the camera on "
          "again' offered, the readiness panel shown", res.get("closed") == (False, True, True, True),
          res.get("closed"))
    check("A-4: 'Turn the camera on again' takes a fresh lease before the open",
          res.get("retry") == (True, True, True), res.get("retry"))
    check("V-31: Cancel ends the calibration with its own text", res.get("cancelled") == (False, True),
          res.get("cancelled"))
    check("V-20 in the window: the build line names the photos of another person",
          "different person" in res.get("built_text", "") or "другим человеком" in res.get("built_text", ""),
          res.get("built_text"))
    cfg_text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    check("V-34: the default camera name is saved after the successful build",
          'camera_name = "Synthetic Camera"' in cfg_text, cfg_text)
    check("V-31 / V-42: Close during a calibration closes; the lease is released by a worker",
          CALLS.count(("pipe", "resume_camera")) > res.get("resume_before_close", 10**6)
          and not w.lease.held and (w.cam_thread is None or not w.cam_thread.is_alive()))
    tk_thread = threading.main_thread().name
    check("V-42: every pipe call of the run was made off the Tk thread",
          tk_thread not in E.pipe_call.threads, [n for n in E.pipe_call.threads if n == tk_thread])


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
    test_window(shots)
    print()
    if FAILS:
        print(f"WIZARD SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("WIZARD SELFTEST OK: the lease comes before the camera and goes with it; 5 s to a first "
          "frame; Retry re-leases; no pipe call on the Tk thread; the default camera by name; Enter "
          "= Add; calibration can be cancelled and stalls out; Close always closes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
