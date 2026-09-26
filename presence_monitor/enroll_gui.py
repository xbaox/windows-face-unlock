"""The face setup wizard -- camera preview, capture, build, turn calibration, readiness check.

Runs as its OWN process (``python -m presence_monitor.enroll_gui`` in a checkout, the tray exe with
``--enroll`` when installed; started by the tray and by the installer's Finish page). Owning a
process is what makes a wedged camera read harmless: closing the window ends the process, and the
OS takes the device back whatever state the native call is in (KNOWN_ISSUES #1).

Stage 9 (act 9b R12) rebuilt it around four rules:

* **Threads never touch Tk.** The camera, the service calls and the readiness check run as plain
  functions on worker threads that hold only a queue and plain data (``Session``). They post
  messages; the Tk thread drains the queue with ``after`` and is the only one that touches a
  widget. Only the newest preview frame is drawn.
* **Honest flow.** The first text says what to do ("press Start"); Start is enabled only when the
  service answers AND the camera has delivered a usable frame; a service that does not answer or a
  camera that does not open says so, with a Retry button, and the wait goes on in the background
  (F-177, F-178, F-179). The shot counter and the pose instruction have their own row (F-181).
* **The camera lease only while it is needed** (R10): the wizard asks the service for the camera
  when capture starts, keeps it through the build and the calibration, and gives it back right
  after -- not for as long as the window is open (F-180).
* **A real end state.** After the build the wizard offers the head-turn calibration (R6, F-117),
  then checks what face sign-in needs -- a readable stored password the lock screen has not
  rejected, a secure data folder, the service seeing the new face profile, the camera given back
  -- and shows each with a button to fix it (F-180).

Also: pick the camera by device name (R10, F-141); Replace / Add is a dialog with those words on
its buttons (F-202); an unbuilt Replace session asks before it is thrown away, and a stale one is
removed at start (F-182); Close waits for a running build (F-185); Delete is off while capturing
(F-186); after a build the next Start asks the mode again (F-187); the build timeout follows the
number of shots (F-190); one wizard at a time -- a second start brings this window to the front
(F-189).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import NamedTuple

import cv2
import numpy as np
from PIL import Image, ImageTk

from face_service import imio
from face_service.config import (CALIBRATION_DIR, EMBED_PATH, ENROLL_DIR, ENROLL_PENDING_DIR,
                                 WATCHDOG_PAUSE_PATH, Config)
from face_service.detector import FaceDetector
from face_service.enroll_qc import frame_quality, qc_reasons
from face_service.i18n import set_language, t

from .monitor import pipe_call
from .widgets import attach_tooltip

# Explicit name, NOT __name__: under `python -m presence_monitor.enroll_gui` __name__ is "__main__".
log = logging.getLogger("presence_monitor.enroll_gui")

ENROLL_MUTEX = "Local\\FaceUnlockEnroll"
PREVIEW_W = 480
PREVIEW_H = 360
CAPTURE_COOLDOWN_S = 1.0    # min gap between captures
FACE_STABLE_FRAMES = 3      # a face must be seen this many frames in a row before a capture
DETECT_EVERY_N_FRAMES = 2   # YuNet is fast, but every other frame is enough to feel live
CAMERA_LEASE_S = 120        # the lease asked of the service; renewed while it is needed
LEASE_RENEW_S = 45
SERVICE_WAIT_S = 60.0       # after this the wizard SAYS the service is not answering (it keeps trying)
SERVICE_PING_S = 3.0
CAMERA_READ_TIMEOUT_MS = 1000
COUNT_MIN, COUNT_MAX, COUNT_DEFAULT = 5, 40, 15
BLACK_LUMA = 8.0            # a first frame at or below this is not "usable" yet
CALIB_SHOTS = 3
CALIB_TURN_WAIT_S = 2.5

# ---- framing coach (wizard UX only -- not recognition, liveness or QC numbers) ----
COACH_AREA_MIN_FRAC = 0.06
COACH_AREA_MAX_FRAC = 0.38
COACH_OFFSET_MAX_FRAC = 0.18
COACH_FACE_ASPECT = 0.8

# ---- pose advice after a build (Stage 8b D-17): a WARNING, never a gate ----
POSE_PITCH_WARN_DEG = -15.0
POSE_YAW_WARN_DEG = 15.0


def pose_warning(pitch: float, yaw: float) -> bool:
    return pitch < POSE_PITCH_WARN_DEG or abs(yaw) > POSE_YAW_WARN_DEG


_GUIDE_AREA_PX = (COACH_AREA_MIN_FRAC + COACH_AREA_MAX_FRAC) / 2 * PREVIEW_W * PREVIEW_H
_GUIDE_AXIS_Y = int((_GUIDE_AREA_PX / COACH_FACE_ASPECT) ** 0.5) // 2
_GUIDE_AXIS_X = int(_GUIDE_AXIS_Y * COACH_FACE_ASPECT)
_COACH_BGR = {"ok": (60, 190, 90), "warn": (40, 170, 235), "err": (60, 60, 210)}
_COACH_FG = {"ok": "#1e7a3c", "warn": "#8a5a00", "err": "#b3261e", "info": "#222222"}

_REASON_KEY_BY_TOKEN = {
    "det": "enroll.reason.det", "blur": "enroll.reason.blur", "dark": "enroll.reason.dark",
    "bright": "enroll.reason.bright", "no-face": "enroll.reason.no_face",
    "unreadable": "enroll.reason.unreadable", "crop-failed": "enroll.reason.crop_failed",
}
_COACH_KEY_BY_TOKEN = {"dark": "enroll.coach.dark", "bright": "enroll.coach.bright",
                       "blur": "enroll.coach.blur"}


def count_images(directory=None) -> int:
    try:
        return sum(1 for p in (directory or ENROLL_DIR).iterdir()
                   if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    except FileNotFoundError:
        return 0


def clamp_count(raw) -> int:
    """F-184: the shot count is 5..40 however it was typed."""
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return COUNT_DEFAULT
    return max(COUNT_MIN, min(COUNT_MAX, v))


def build_timeout_s(n_images: int) -> float:
    """F-190: the first build may load the engine on a CPU; allow for it and for every shot."""
    return float(min(600, 90 + 6 * max(0, n_images)))


def _pause_watchdog(ttl_s: float) -> "float | None":
    """Stage 8b (F-30): the build holds the sequential server; the watchdog would read that as a
    hang. A standard self-expiring pause marker covers the build; its creation time identifies it."""
    try:
        from face_service.watchdog import write_pause
        now = time.time()
        write_pause(WATCHDOG_PAUSE_PATH, now, ttl_s)
        return now
    except Exception:
        log.exception("could not pause the watchdog for the build")
        return None


def _resume_watchdog(created: "float | None") -> None:
    if created is None:
        return
    try:
        import json
        data = json.loads(WATCHDOG_PAUSE_PATH.read_text(encoding="utf-8"))
        if float(data.get("created", -1)) == created:
            from face_service.watchdog import clear_pause
            clear_pause(WATCHDOG_PAUSE_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("could not clear the build's watchdog pause (it self-expires)")


def _token_head(tok: str) -> str:
    for sep in ("<", ">"):
        i = tok.find(sep)
        if i > 0:
            return tok[:i]
    return tok


def humanize_reason(reason: str) -> "str | None":
    """Localise the service's QC rejection summary ("... Dropped: blur<80 x2, no-face x1. ..."),
    or None when a token is unknown (the caller then shows the raw text)."""
    marker = "Dropped:"
    i = reason.find(marker)
    if i < 0:
        return None
    tail = reason[i + len(marker):].strip()
    end = tail.find(". ")
    if end >= 0:
        tail = tail[:end]
    parts: list[str] = []
    for item in tail.split(","):
        item = item.strip()
        if not item:
            continue
        tok, _, count = item.partition(" x")
        key = _REASON_KEY_BY_TOKEN.get(_token_head(tok.strip()))
        if key is None:
            return None
        count = count.strip()
        parts.append(f"{t(key)} ×{count}" if count else t(key))
    return ", ".join(parts) or None


class CoachState(NamedTuple):
    key: str      # i18n key for the guidance line
    ok: bool      # the frame passes the live capture gate
    level: str    # "ok" | "warn" | "err"


class _YuNetFace:
    """A YuNet row in the InsightFace shape ``enroll_qc`` reads (.bbox x1y1x2y2, .kps, .det_score)."""
    __slots__ = ("bbox", "kps", "det_score")

    def __init__(self, row):
        x, y, w, h = (float(v) for v in row[0:4])
        self.bbox = np.array([x, y, x + w, y + h], dtype=np.float32)
        pts = np.asarray(row[4:14], dtype=np.float32).reshape(5, 2)
        eyes = pts[0:2][np.argsort(pts[0:2, 0])]
        mouth = pts[3:5][np.argsort(pts[3:5, 0])]
        self.kps = np.stack([eyes[0], eyes[1], pts[2], mouth[0], mouth[1]])
        self.det_score = float(row[14])


def evaluate_coach(frame, rows, cfg, detector_unavailable: bool = False) -> CoachState:
    """Grade one frame: no face -> framing -> quality -> ready. Pure except for enroll_qc."""
    if not rows:
        if detector_unavailable:
            return CoachState("enroll.coach.detector_unavailable", False, "err")
        return CoachState("enroll.status.waiting", False, "err")
    row = max(rows, key=lambda r: float(r[2]) * float(r[3]))
    fh, fw = frame.shape[:2]
    x, y, w, h = (float(v) for v in row[0:4])
    area = (w * h) / float(fw * fh)
    if area < COACH_AREA_MIN_FRAC:
        return CoachState("enroll.coach.closer", False, "warn")
    if area > COACH_AREA_MAX_FRAC:
        return CoachState("enroll.coach.farther", False, "warn")
    if (abs((x + w / 2) - fw / 2) / fw > COACH_OFFSET_MAX_FRAC
            or abs((y + h / 2) - fh / 2) / fh > COACH_OFFSET_MAX_FRAC):
        return CoachState("enroll.coach.center", False, "warn")
    try:
        q = frame_quality(frame, _YuNetFace(row))
    except Exception as e:
        log.debug("live quality failed: %s", e)
        q = None
    if q is None:
        return CoachState("enroll.status.waiting", False, "err")
    heads = {_token_head(r) for r in qc_reasons(q, cfg)} - {"det"}
    for tok in ("dark", "bright", "blur"):
        if tok in heads:
            return CoachState(_COACH_KEY_BY_TOKEN[tok], False, "warn")
    return CoachState("enroll.status.ready", True, "ok")


def annotate(bgr, faces, level: str):
    colour = _COACH_BGR.get(level, _COACH_BGR["warn"])
    img = bgr.copy()
    for row in faces:
        x, y, w, h = (int(v) for v in row[0:4])
        cv2.rectangle(img, (x, y), (x + w, y + h), colour, 3)
    img = cv2.flip(img, 1)
    h0, w0 = img.shape[:2]
    scale = min(PREVIEW_W / w0, PREVIEW_H / h0)
    nw, nh = int(w0 * scale), int(h0 * scale)
    img = cv2.resize(img, (nw, nh))
    canvas = np.zeros((PREVIEW_H, PREVIEW_W, 3), dtype=img.dtype)
    ox, oy = (PREVIEW_W - nw) // 2, (PREVIEW_H - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = img
    cv2.ellipse(canvas, (PREVIEW_W // 2, PREVIEW_H // 2), (_GUIDE_AXIS_X, _GUIDE_AXIS_Y),
                0, 0, 360, colour, 2)
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


# ---------------------------------------------------------------------------------------------
# Worker side: plain data + a queue. Nothing below touches Tk.
# ---------------------------------------------------------------------------------------------

class Session:
    """State shared between the Tk thread and the camera worker. Plain Python, one lock."""

    def __init__(self, cfg: Config, camera_name: str):
        self.cfg = cfg
        self.camera_name = camera_name
        self.stop = threading.Event()
        self.armed = threading.Event()
        self.lock = threading.Lock()
        self.capture_dir = ENROLL_DIR
        self.target = COUNT_DEFAULT
        self.captured = 0
        self.calib: "tuple[str, int] | None" = None     # ("frontal"|"left", shots still wanted)
        self.calib_names: dict = {"frontal": [], "left": []}


def _open_capture(cfg: Config, camera_name: str):
    """Open the configured camera: by NAME on DSHOW only (R10); an empty name is the legacy index
    path. Returns (cap, None) or (None, reason) with reason "not-found" | "open-failed"."""
    index, backends = cfg.camera_index, (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
    name = (camera_name or "").strip()
    if name:
        from face_service.camera_devices import resolve_index
        index = resolve_index(name)
        if index is None:
            log.warning("enroll camera %r is not connected", name)
            return None, "not-found"
        backends = (cv2.CAP_DSHOW,)
    for backend in backends:
        cap = cv2.VideoCapture(index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            fcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            log.info("enroll camera open: device=%s index=%s backend=%s %dx%d fps=%.1f fourcc=%s",
                     repr(name) if name else "(by index)", index, backend,
                     int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                     float(cap.get(cv2.CAP_PROP_FPS)),
                     "".join(chr((fcc >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00") or "?")
        except Exception:
            log.debug("enroll camera format query failed", exc_info=True)
        for prop_name in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
            prop = getattr(cv2, prop_name, None)
            if prop is not None:
                cap.set(prop, CAMERA_READ_TIMEOUT_MS)
        return cap, None
    return None, "open-failed"


def camera_worker(s: Session, q: "queue.Queue") -> None:
    """Preview + capture + calibration shots. Posts: ("camera", "failed", reason),
    ("frame", rgb), ("first_frame",), ("coach", CoachState), ("captured", n),
    ("done_capture", n), ("calib_shots", phase, names)."""
    cap, why = _open_capture(s.cfg, s.camera_name)
    if cap is None:
        q.put(("camera_failed", why))
        return
    detector = FaceDetector()
    try:
        import importlib
        importlib.import_module("insightface.utils.face_align")   # warm the one-off import
    except Exception:
        pass
    frame_idx = 0
    faces: list = []
    coach = CoachState("enroll.status.waiting", False, "err")
    last_coach = None
    streak = 0
    last_capture = 0.0
    first = False
    try:
        while not s.stop.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            frame_idx += 1
            if not first and float(frame.mean()) > BLACK_LUMA:
                first = True
                q.put(("first_frame",))
            measured = frame_idx % DETECT_EVERY_N_FRAMES == 0
            if measured:
                try:
                    h, w = frame.shape[:2]
                    det = detector._ensure(w, h)  # type: ignore[attr-defined]
                    _, res = det.detect(frame) if det is not None else (None, None)
                    faces = list(res) if res is not None else []
                except Exception as e:
                    log.debug("detect failed: %s", e)
                    faces = []
                coach = evaluate_coach(frame, faces, s.cfg, bool(getattr(detector, "unavailable", False)))
                streak = streak + 1 if faces else 0
            if coach.key != last_coach:
                last_coach = coach.key
                q.put(("coach", coach))
            now = time.time()
            if measured and coach.ok and streak >= FACE_STABLE_FRAMES and now - last_capture >= CAPTURE_COOLDOWN_S:
                with s.lock:
                    calib = s.calib
                if calib is not None:
                    phase, left = calib
                    name = f"{phase}_{int(now * 1000)}.png"
                    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
                    if imio.imwrite(CALIBRATION_DIR / name, frame):
                        last_capture = now
                        with s.lock:
                            s.calib_names[phase].append(name)
                            s.calib = (phase, left - 1) if left > 1 else None
                        if left <= 1:
                            q.put(("calib_shots", phase, list(s.calib_names[phase])))
                elif s.armed.is_set():
                    with s.lock:
                        target, d = s.target, s.capture_dir
                    try:
                        d.mkdir(parents=True, exist_ok=True)
                        path = d / f"enroll_{int(now * 1000)}.jpg"
                        if imio.imwrite(path, frame):      # R8: checked, Unicode-safe
                            last_capture = now
                            with s.lock:
                                s.captured += 1
                                n = s.captured
                                done = n >= target
                            log.info("enroll: saved %s (%d/%d)", path.name, n, target)
                            q.put(("captured", n))
                            if done:
                                s.armed.clear()
                                q.put(("done_capture", n))
                        else:
                            log.error("enroll: could not save %s -- not counted", path.name)
                    except Exception:
                        log.exception("failed to save an enrollment shot")
            q.put(("frame", annotate(frame, faces, coach.level)))
            time.sleep(0.03)
    finally:
        try:
            cap.release()
        except Exception:
            log.exception("enroll camera release failed")


def service_wait_worker(stop: threading.Event, q: "queue.Queue") -> None:
    """Ping until the service answers; says "late" once after SERVICE_WAIT_S and keeps trying."""
    t0 = time.monotonic()
    late = False
    while not stop.is_set():
        resp = pipe_call({"cmd": "ping"}, timeout_s=SERVICE_PING_S)
        if resp and resp.get("ok"):
            q.put(("service", "ready", resp.get("state"), resp.get("why")))
            return
        if not late and time.monotonic() - t0 >= SERVICE_WAIT_S:
            late = True
            q.put(("service", "late", None, None))
        stop.wait(2.0)


class LeaseKeeper:
    """The camera lease, held only while capture / build / calibration need it (R10)."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self.held = False

    def acquire(self) -> bool:
        resp = pipe_call({"cmd": "pause_camera", "seconds": CAMERA_LEASE_S}, timeout_s=5.0)
        if not (resp and resp.get("ok")):
            log.warning("camera lease refused: %s", resp)
            return False
        self.held = True
        self._stop.clear()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._renew, name="enroll-lease", daemon=True)
            self._thread.start()
        return True

    def _renew(self) -> None:
        while not self._stop.wait(LEASE_RENEW_S):
            resp = pipe_call({"cmd": "pause_camera", "seconds": CAMERA_LEASE_S}, timeout_s=5.0)
            if not (resp and resp.get("ok")):
                log.warning("camera lease renewal failed: %s", resp)

    def release(self) -> None:
        self._stop.set()
        if self.held:
            self.held = False
            for _ in range(3):              # F-185: a busy server may need a second try
                resp = pipe_call({"cmd": "resume_camera"}, timeout_s=5.0)
                if resp and resp.get("ok"):
                    return
                time.sleep(1.0)
            log.warning("resume_camera was not confirmed; the lease lapses by itself")


def build_worker(q: "queue.Queue", req: dict, timeout_s: float, pause_ttl: float) -> None:
    paused = _pause_watchdog(pause_ttl)
    try:
        resp = pipe_call(req, timeout_s=timeout_s)
    finally:
        _resume_watchdog(paused)
    q.put(("built", resp))


def calibrate_worker(q: "queue.Queue", frontal: list, left: list) -> None:
    q.put(("calibrated", pipe_call({"cmd": "calibrate_turn", "frontal": frontal, "left": left},
                                   timeout_s=30.0)))


def readiness_worker(q: "queue.Queue", lease_released: bool) -> None:
    """F-180: what face sign-in needs, checked for real."""
    from face_service.credentials import password_state
    status = pipe_call({"cmd": "status"}, timeout_s=5.0) or {}
    try:
        pwd, _info = password_state()
    except Exception:
        pwd = "unreadable"
    ok = bool(status.get("ok"))
    checks = {
        "service": ok and status.get("state") == "serving",
        "custody": ok and bool(status.get("data_dir_secure")),
        "enrollment": ok and bool(status.get("enrollment")),
        "password": pwd == "ok" and not status.get("password_rejected"),
        "camera": lease_released,
    }
    q.put(("readiness", checks, pwd, bool(status.get("password_rejected"))))


def release_and_check_worker(q: "queue.Queue", lease: LeaseKeeper) -> None:
    """Give the camera back, then run the readiness check (worker: the lease and the queue only)."""
    lease.release()
    readiness_worker(q, not lease.held)


def camera_worker_after(prev: "threading.Thread | None", s: Session, q: "queue.Queue") -> None:
    """Start the preview only once the previous camera thread has let the device go."""
    if prev is not None and prev.is_alive():
        prev.join(timeout=5.0)
    camera_worker(s, q)


def wipe_worker(q: "queue.Queue") -> None:
    q.put(("wiped", pipe_call({"cmd": "clear_enrollment"}, timeout_s=30.0)))


def save_camera_worker(q: "queue.Queue", name: str) -> None:
    try:
        cfg = Config.load()
        cfg.camera_name = name
        cfg.validate()
        cfg.save(keys=["camera_name"])
        pipe_call({"cmd": "reload_config"}, timeout_s=5.0)
    except Exception:
        log.exception("saving the camera choice failed")


# ---------------------------------------------------------------------------------------------
# Tk side
# ---------------------------------------------------------------------------------------------

def ask_mode(parent) -> "str | None":
    """F-202: Replace / Add / Cancel with those words on the buttons (never Yes/No/Cancel)."""
    result: dict = {"v": None}
    top = tk.Toplevel(parent)
    top.title(t("enroll.confirm.mode.title"))
    top.transient(parent)
    top.resizable(False, False)
    frm = ttk.Frame(top, padding=14)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=t("enroll.confirm.mode.body"), wraplength=420, justify="left").pack(
        anchor="w", pady=(0, 12))
    btns = ttk.Frame(frm)
    btns.pack(anchor="e")

    def choose(v):
        result["v"] = v
        top.destroy()
    b = ttk.Button(btns, text=t("enroll.mode.replace"), command=lambda: choose("replace"))
    b.pack(side="left", padx=4)
    ttk.Button(btns, text=t("enroll.mode.add"), command=lambda: choose("add")).pack(side="left", padx=4)
    ttk.Button(btns, text=t("enroll.btn.cancel"), command=lambda: choose(None)).pack(side="left", padx=4)
    top.bind("<Escape>", lambda _e: choose(None))
    top.bind("<Return>", lambda _e: choose("replace"))
    b.focus_set()
    top.grab_set()
    parent.wait_window(top)
    return result["v"]


def ask_unbuilt(parent, n: int) -> "str | None":
    """F-182: an unbuilt Replace session on Close -- Build / Discard / Cancel."""
    result: dict = {"v": None}
    top = tk.Toplevel(parent)
    top.title(t("enroll.unbuilt.title"))
    top.transient(parent)
    frm = ttk.Frame(top, padding=14)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=t("enroll.unbuilt.body", n=n), wraplength=420, justify="left").pack(
        anchor="w", pady=(0, 12))
    btns = ttk.Frame(frm)
    btns.pack(anchor="e")

    def choose(v):
        result["v"] = v
        top.destroy()
    b = ttk.Button(btns, text=t("enroll.btn.build"), command=lambda: choose("build"))
    b.pack(side="left", padx=4)
    ttk.Button(btns, text=t("enroll.unbuilt.discard"), command=lambda: choose("discard")).pack(side="left", padx=4)
    ttk.Button(btns, text=t("enroll.btn.cancel"), command=lambda: choose(None)).pack(side="left", padx=4)
    top.bind("<Escape>", lambda _e: choose(None))
    b.focus_set()
    top.grab_set()
    parent.wait_window(top)
    return result["v"]


class EnrollWindow:
    def __init__(self):
        from .ui import apply_scaling
        self.root = tk.Tk()
        self.root.title(t("enroll.title"))
        apply_scaling(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.q: "queue.Queue" = queue.Queue()
        self.cfg = Config.load()
        self.session: "Session | None" = None
        self.cam_thread: "threading.Thread | None" = None
        self.lease = LeaseKeeper()
        self.stop_all = threading.Event()
        self.service_ready = False
        self.refusing: "str | None" = None
        self.camera_ok = False
        self.building = False
        self.calibrating = False
        self.mode: "str | None" = None
        self.devices: list = []
        self._tk_image = None

        # A stale pending session (a tray Quit killed an earlier wizard mid-Replace) holds face
        # images with no purpose -- removed before anything else (F-182).
        try:
            from face_service.datadir import remove_tree_no_follow
            if ENROLL_PENDING_DIR.exists():
                remove_tree_no_follow(ENROLL_PENDING_DIR)
                log.info("removed a stale pending enrollment session")
        except Exception:
            log.exception("could not remove the stale pending session")

        self._build_ui()
        self._set_line(t("enroll.status.connecting"), "info")
        self._refresh_buttons()
        threading.Thread(target=service_wait_worker, args=(self.stop_all, self.q),
                         name="enroll-service-wait", daemon=True).start()
        self._load_devices()
        self._start_camera()
        self.root.after(33, self._drain)

    # ---- UI ----
    def _build_ui(self) -> None:
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        cam_row = ttk.Frame(frm)
        cam_row.pack(fill="x", pady=(0, 6))
        ttk.Label(cam_row, text=t("enroll.camera") + ":").pack(side="left")
        self.cam_var = tk.StringVar(master=self.root)
        self.cam_combo = ttk.Combobox(cam_row, textvariable=self.cam_var, state="readonly", width=40)
        self.cam_combo.pack(side="left", padx=6)
        self.cam_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_camera_chosen())

        self.preview = tk.Label(frm, background="#222", width=PREVIEW_W, height=PREVIEW_H)
        self.preview.pack(pady=(0, 8))
        blank = Image.new("RGB", (PREVIEW_W, PREVIEW_H), (24, 24, 24))
        self._tk_image = ImageTk.PhotoImage(blank, master=self.root)
        self.preview.configure(image=self._tk_image)

        self.line = ttk.Label(frm, text="", wraplength=PREVIEW_W, justify="center",
                              font=("", 11, "bold"))
        self.line.pack(fill="x", pady=(0, 4))
        # F-181: the shot counter and the pose instruction live in their own rows.
        prog = ttk.Frame(frm)
        prog.pack(fill="x", pady=(0, 2))
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=COUNT_DEFAULT)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.count_lbl = ttk.Label(prog, text="", font=("", 10, "bold"))
        self.count_lbl.pack(side="right")
        attach_tooltip(self.progress, "enroll.progress.tip")
        ttk.Label(frm, text=t("enroll.pose_hint"), foreground="#555", wraplength=PREVIEW_W,
                  justify="left").pack(anchor="w", pady=(0, 6))

        row = ttk.Frame(frm)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=t("enroll.count") + ":").pack(side="left")
        self.count_var = tk.StringVar(master=self.root, value=str(COUNT_DEFAULT))
        self.count_spin = ttk.Spinbox(row, from_=COUNT_MIN, to=COUNT_MAX, textvariable=self.count_var,
                                      width=6)
        self.count_spin.pack(side="left", padx=6)
        self.existing = ttk.Label(frm, text="", foreground="#555")
        self.existing.pack(anchor="w", pady=(2, 6))

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))
        self.start_btn = ttk.Button(btns, text=t("enroll.btn.start"), command=self._on_start_stop)
        self.start_btn.pack(side="left", padx=3)
        self.build_btn = ttk.Button(btns, text=t("enroll.btn.build"), command=self._on_build)
        self.build_btn.pack(side="left", padx=3)
        self.wipe_btn = ttk.Button(btns, text=t("enroll.btn.wipe"), command=self._on_wipe)
        self.wipe_btn.pack(side="left", padx=3)
        self.retry_btn = ttk.Button(btns, text=t("enroll.btn.retry"), command=self._on_retry)
        ttk.Button(btns, text=t("enroll.btn.close"), command=self._on_close).pack(side="right", padx=3)

        # The end-state panel (F-180), shown after a build.
        self.ready_frm = ttk.LabelFrame(frm, text=t("enroll.ready.title"), padding=8)
        self.ready_lines: dict = {}
        for i, key in enumerate(("service", "custody", "enrollment", "password", "camera")):
            lbl = ttk.Label(self.ready_frm, text="", wraplength=PREVIEW_W - 40, justify="left")
            lbl.grid(row=i, column=0, sticky="w")
            self.ready_lines[key] = lbl
        rb = ttk.Frame(self.ready_frm)
        rb.grid(row=10, column=0, sticky="w", pady=(6, 0))
        self.calib_btn = ttk.Button(rb, text=t("enroll.btn.calibrate"), command=self._on_calibrate)
        self.calib_btn.pack(side="left", padx=3)
        self.pwd_btn = ttk.Button(rb, text=t("enroll.btn.set_password"), command=self._on_set_password)
        self.pwd_btn.pack(side="left", padx=3)
        ttk.Button(rb, text=t("enroll.btn.check_again"), command=self._check_ready).pack(side="left", padx=3)

        self.root.bind("<Escape>", lambda _e: self._on_close())
        self._refresh_existing()

    def _set_line(self, text: str, level: str = "info") -> None:
        self.line.configure(text=text, foreground=_COACH_FG.get(level, "#222"))

    def _refresh_existing(self) -> None:
        n = count_images()
        key = "enroll.existing.yes" if EMBED_PATH.exists() else "enroll.existing.no"
        self.existing.configure(text=t(key, n=n))

    def _refresh_buttons(self) -> None:
        armed = bool(self.session and self.session.armed.is_set())
        can_start = (self.service_ready and not self.refusing and self.camera_ok
                     and not self.building and not self.calibrating)
        self.start_btn.configure(text=t("enroll.btn.stop") if armed else t("enroll.btn.start"),
                                 state="normal" if (can_start or armed) else "disabled")
        n = count_images(self._capture_dir())
        self.build_btn.configure(state="normal" if (self.service_ready and not self.refusing and n
                                                    and not self.building and not armed
                                                    and not self.calibrating) else "disabled")
        self.wipe_btn.configure(state="normal" if (self.service_ready and not armed and not self.building
                                                   and not self.calibrating) else "disabled")
        self.count_spin.configure(state="disabled" if armed else "normal")
        self.cam_combo.configure(state="disabled" if (armed or self.building or self.calibrating)
                                 else "readonly")

    def _capture_dir(self):
        return ENROLL_PENDING_DIR if self.mode == "replace" else ENROLL_DIR

    # ---- camera ----
    def _load_devices(self) -> None:
        from face_service.camera_devices import list_video_devices
        try:
            self.devices = [d.name for d in list_video_devices() if d.name]
        except Exception:
            self.devices = []
        values = list(self.devices)
        cur = self.cfg.camera_name
        if not cur:
            label = t("settings.camera.by_index", i=self.cfg.camera_index)
            values = [label] + values
            self.cam_var.set(label)
        else:
            if cur not in values:
                values.append(cur)
            self.cam_var.set(cur)
        self.cam_combo.configure(values=values)

    def _start_camera(self) -> None:
        prev = self.cam_thread
        if self.session is not None:
            self.session.stop.set()
        self.camera_ok = False
        self.session = Session(self.cfg, self.cfg.camera_name)
        self.cam_thread = threading.Thread(target=camera_worker_after, args=(prev, self.session, self.q),
                                           name="enroll-camera", daemon=True)
        self.cam_thread.start()
        self.retry_btn.pack_forget()
        self._refresh_buttons()

    def _on_camera_chosen(self) -> None:
        name = self.cam_var.get()
        if name not in self.devices:
            name = ""                              # the by-index entry
        if name == self.cfg.camera_name:
            return
        self.cfg.camera_name = name
        threading.Thread(target=save_camera_worker, args=(self.q, name), daemon=True).start()
        log.info("camera chosen: %r", name)
        self._start_camera()

    def _on_retry(self) -> None:
        self.retry_btn.pack_forget()
        self._set_line(t("enroll.status.retrying"), "info")
        self.root.after(1500, self._start_camera)          # F-179: a beat after the release

    # ---- the queue ----
    def _drain(self) -> None:
        frame = None
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "frame":
                    frame = msg[1]                   # only the newest one is drawn
                else:
                    self._handle(msg)
        except queue.Empty:
            pass
        if frame is not None:
            try:
                self._tk_image = ImageTk.PhotoImage(Image.fromarray(frame), master=self.root)
                self.preview.configure(image=self._tk_image)
            except tk.TclError:
                pass
        try:
            self.root.after(33, self._drain)
        except tk.TclError:
            pass

    def _handle(self, msg) -> None:
        kind = msg[0]
        if kind == "service":
            _k, what, state, why = msg
            if what == "ready":
                self.service_ready = True
                self.refusing = why if state == "refusing" else None
                if self.refusing:
                    self._set_line(t("enroll.error.refusing", why=t(f"why.{self.refusing}")), "err")
                elif self.camera_ok:
                    self._set_line(t("enroll.guide.idle"), "info")
            else:
                self._set_line(t("enroll.error.service_down"), "err")
            self._refresh_buttons()
        elif kind == "camera_failed":
            self.camera_ok = False
            key = "enroll.error.camera_missing" if msg[1] == "not-found" else "enroll.error.camera"
            self._set_line(t(key, name=self.cfg.camera_name or "?"), "err")
            self.retry_btn.pack(side="left", padx=3)
            self._refresh_buttons()
        elif kind == "first_frame":
            self.camera_ok = True
            if self.service_ready and not self.refusing:
                self._set_line(t("enroll.guide.idle"), "info")
            self._refresh_buttons()
        elif kind == "coach":
            coach = msg[1]
            if self.building or not self.camera_ok or not self.service_ready or self.refusing:
                return
            if self.calibrating:
                return                               # the calibration prompt owns the line
            armed = bool(self.session and self.session.armed.is_set())
            if coach.ok and not armed:
                self._set_line(t("enroll.status.ready_idle"), "ok")      # F-177
            else:
                self._set_line(t(coach.key), coach.level)
        elif kind == "captured":
            self._show_count(msg[1])
        elif kind == "done_capture":
            self._show_count(msg[1])
            self._set_line(t("enroll.guide.done_capture"), "ok")
            self._refresh_buttons()
        elif kind == "built":
            self._on_built(msg[1])
        elif kind == "calib_shots":
            self._on_calib_shots(msg[1], msg[2])
        elif kind == "calibrated":
            self._on_calibrated(msg[1])
        elif kind == "readiness":
            self._show_ready(msg[1], msg[2], msg[3])
        elif kind == "wiped":
            self._on_wiped(msg[1])

    def _show_count(self, n: int) -> None:
        target = self.session.target if self.session else COUNT_DEFAULT
        self.progress.configure(maximum=max(target, n))
        self.progress["value"] = n
        self.count_lbl.configure(text=t("enroll.shots", n=n, target=target))

    # ---- capture ----
    def _on_start_stop(self) -> None:
        s = self.session
        if s is None:
            return
        if s.armed.is_set():
            s.armed.clear()
            self._set_line(t("enroll.guide.idle"), "info")
            self._refresh_buttons()
            return
        new_session = self.mode is None
        if self.mode is None:
            if count_images() == 0 and not EMBED_PATH.exists():
                self.mode = "add"
            else:
                self.mode = ask_mode(self.root)
                if self.mode is None:
                    return
                if self.mode == "replace":
                    from face_service.datadir import remove_tree_no_follow
                    remove_tree_no_follow(ENROLL_PENDING_DIR)
            log.info("enroll session mode: %s", self.mode)
        target = clamp_count(self.count_var.get())
        self.count_var.set(str(target))
        if not self.lease.held and not self.lease.acquire():
            self._set_line(t("enroll.error.lease"), "err")
            return
        with s.lock:
            s.capture_dir = self._capture_dir()
            s.target = target
            if new_session:
                s.captured = 0            # F-184: Stop / Start within a session keeps its count
            n = s.captured
        self._show_count(n)
        if n >= target:
            self._set_line(t("enroll.guide.done_capture"), "ok")
            self._refresh_buttons()
            return
        s.armed.set()
        self._set_line(t("enroll.guide.capturing"), "ok")
        self._refresh_buttons()

    # ---- build ----
    def _on_build(self) -> None:
        if self.building:
            return
        n = count_images(self._capture_dir())
        if n == 0:
            messagebox.showwarning(t("enroll.title"), t("enroll.guide.build_empty"), parent=self.root)
            return
        if self.session:
            self.session.armed.clear()
        if not self.lease.held and not self.lease.acquire():
            self._set_line(t("enroll.error.lease"), "err")
            return
        self.building = True
        self._refresh_buttons()
        self._set_line(t("enroll.guide.building"), "info")
        req = {"cmd": "build_enrollment"}
        if self.mode == "replace":
            req["replace"] = True
        threading.Thread(target=build_worker,
                         args=(self.q, req, build_timeout_s(n), self.cfg.watchdog_pause_ttl_s),
                         name="enroll-build", daemon=True).start()

    def _on_built(self, resp) -> None:
        self.building = False
        self._refresh_existing()
        if resp and resp.get("ok"):
            n = int(resp.get("count", 0))
            self.mode = None                        # F-187: the next Start asks again
            pose = resp.get("pose") or {}
            if n > 0 and pose and pose_warning(float(pose.get("pitch", 0.0)), float(pose.get("yaw", 0.0))):
                self._set_line(t("enroll.guide.pose_warn", n=n), "warn")
            else:
                self._set_line(t("enroll.guide.done", n=n), "ok")
            self._show_ready_panel()
            self._offer_calibration()
        else:
            raw = (resp or {}).get("reason") or ("timeout" if resp is None else "")
            why = humanize_reason(raw)
            if why:
                self._set_line(t("enroll.guide.build_rejected", why=why), "err")
                messagebox.showerror(t("enroll.title"), t("enroll.build.failed", why=why), parent=self.root)
            else:
                self._set_line(t("enroll.guide.build_failed"), "err")
                messagebox.showerror(t("enroll.title"),
                                     t("enroll.build.failed_raw", reason=raw or "?"), parent=self.root)
            self.lease.release()
        self._refresh_buttons()

    # ---- calibration (R6 / F-117) ----
    def _offer_calibration(self) -> None:
        if messagebox.askyesno(t("enroll.calib.title"), t("enroll.calib.offer"), parent=self.root):
            self._on_calibrate()
        else:
            self._release_and_check()

    def _on_calibrate(self) -> None:
        if self.session is None or not self.camera_ok:
            return
        if not self.lease.held and not self.lease.acquire():
            self._set_line(t("enroll.error.lease"), "err")
            return
        self.calibrating = True
        self._refresh_buttons()
        with self.session.lock:
            self.session.calib_names = {"frontal": [], "left": []}
            self.session.calib = ("frontal", CALIB_SHOTS)
        self._set_line(t("enroll.calib.look_straight"), "info")

    def _on_calib_shots(self, phase: str, names: list) -> None:
        if phase == "frontal":
            self._set_line(t("enroll.calib.turn_left"), "info")

            def arm_left():
                if self.session is not None:
                    with self.session.lock:
                        self.session.calib = ("left", CALIB_SHOTS)
            self.root.after(int(CALIB_TURN_WAIT_S * 1000), arm_left)
        else:
            self._set_line(t("enroll.calib.measuring"), "info")
            s = self.session
            threading.Thread(target=calibrate_worker,
                             args=(self.q, list(s.calib_names["frontal"]), list(s.calib_names["left"])),
                             daemon=True).start()

    def _on_calibrated(self, resp) -> None:
        self.calibrating = False
        if resp and resp.get("ok"):
            self._set_line(t("enroll.calib.done"), "ok")
        else:
            reason = (resp or {}).get("reason") or "?"
            key = {"turn-too-small": "enroll.calib.too_small",
                   "no-face": "enroll.calib.no_face"}.get(reason, "enroll.calib.failed")
            self._set_line(t(key, reason=reason), "warn")
        self._release_and_check()

    # ---- end state (F-180) ----
    def _release_and_check(self) -> None:
        threading.Thread(target=release_and_check_worker, args=(self.q, self.lease),
                         name="enroll-ready", daemon=True).start()
        self._refresh_buttons()

    def _check_ready(self) -> None:
        threading.Thread(target=readiness_worker, args=(self.q, not self.lease.held),
                         daemon=True).start()

    def _show_ready_panel(self) -> None:
        if not self.ready_frm.winfo_ismapped():
            self.ready_frm.pack(fill="x", pady=(8, 0))

    def _show_ready(self, checks: dict, pwd: str, rejected: bool) -> None:
        self._show_ready_panel()
        for key, ok in checks.items():
            text = t(f"enroll.ready.{key}.{'ok' if ok else 'bad'}")
            if key == "password" and not ok:
                text = t("enroll.ready.password.rejected" if rejected
                         else f"enroll.ready.password.{pwd}")
            self.ready_lines[key].configure(text=("✓ " if ok else "✗ ") + text,
                                            foreground=_COACH_FG["ok" if ok else "err"])
        if all(checks.values()):
            self._set_line(t("enroll.ready.all_ok"), "ok")
        self._refresh_buttons()

    def _on_set_password(self) -> None:
        import subprocess
        import sys
        frozen = bool(getattr(sys, "frozen", False))
        argv = [sys.executable, "--set-password"] if frozen else \
            [sys.executable, "-m", "presence_monitor.password_gui"]
        try:
            subprocess.Popen(argv, creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not open the password dialog")

    # ---- delete ----
    def _on_wipe(self) -> None:
        if not messagebox.askyesno(t("enroll.confirm.wipe.title"), t("enroll.confirm.wipe.body"),
                                   parent=self.root, icon="warning", default="no"):
            return
        if self.session:
            self.session.armed.clear()               # F-186: nothing is written during the wipe
        self.wipe_btn.configure(state="disabled")
        threading.Thread(target=wipe_worker, args=(self.q,), daemon=True).start()

    def _on_wiped(self, resp) -> None:
        if not (resp and resp.get("ok")):
            log.warning("clear_enrollment failed: %s", resp)
            messagebox.showerror(t("enroll.title"), t("enroll.error.wipe_failed"), parent=self.root)
        else:
            self.mode = None
            if self.session is not None:
                with self.session.lock:
                    self.session.captured = 0
            self._show_count(0)
            self._set_line(t("enroll.guide.idle"), "info")
        self._refresh_existing()
        self._refresh_buttons()

    # ---- close ----
    def _on_close(self) -> None:
        if self.building or self.calibrating:
            messagebox.showinfo(t("enroll.title"), t("enroll.busy_close"), parent=self.root)
            return                                  # F-185
        if self.mode == "replace":
            n = count_images(ENROLL_PENDING_DIR)
            if n:
                choice = ask_unbuilt(self.root, n)
                if choice is None:
                    return
                if choice == "build":
                    self._on_build()
                    return
        self.stop_all.set()
        if self.session is not None:
            self.session.stop.set()
            self.session.armed.clear()
        th = self.cam_thread
        if th is not None and th.is_alive():
            th.join(timeout=3.0)
            if th.is_alive():
                log.warning("enroll camera thread did not exit within 3s")
        try:
            self.root.destroy()
        except Exception:
            pass
        self.lease.release()
        if self.mode == "replace":
            try:
                from face_service.datadir import remove_tree_no_follow
                remove_tree_no_follow(ENROLL_PENDING_DIR)
            except Exception:
                log.exception("discarding the pending session failed")

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    """Process entry point (``python -m presence_monitor.enroll_gui`` / tray exe ``--enroll``)."""
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    from presence_monitor.instance import first_instance, raise_by_title
    from presence_monitor.ui import enable_dpi_awareness
    setup_logging(LOG_PATH.with_name("enroll.log"))
    try:
        cfg = Config.load()
        set_language(cfg.language)
        if not first_instance(ENROLL_MUTEX):              # F-189: in this module, both layouts
            raise_by_title(t("enroll.title"))
            return 0
        enable_dpi_awareness()
        from face_service.ort_privacy import disable_ort_telemetry
        disable_ort_telemetry()          # the wizard imports insightface -> onnxruntime (§2.11)
        log.info("enroll wizard starting: pid=%s lang=%s camera_name=%r camera_index=%s",
                 os.getpid(), cfg.language, cfg.camera_name, cfg.camera_index)
        EnrollWindow().run()
    except Exception:
        log.exception("enroll wizard crashed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
