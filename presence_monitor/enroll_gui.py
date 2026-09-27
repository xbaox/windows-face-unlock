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
* **The camera lease while the wizard holds the device** (R10; 9d, A-4): the lease is taken
  BEFORE the camera is opened -- no lease, no camera -- and held for as long as the wizard holds
  the device: preview, capture, build, calibration. The end state closes the camera and gives the
  lease back; "Turn the camera on again" takes both again. A camera that delivers no usable frame
  within 5 s is reported as failed; Retry takes a fresh lease and opens it again.
* **No pipe call on the Tk thread** (9d, V-42): the lease, the service calls and the close run on
  workers and report through the queue.
* **9d-r2.** The lease is counted per holder (a camera session, a build) and asked for only once
  the service has answered; one camera session at a time ("switching camera"); every camera
  message carries its session's number, so a stale frame or failure is dropped; the first-frame
  limit is kept on the Tk side; Build works with the camera off; failures are shown in words;
  the preview is sized so the whole window fits the work area, and the window is kept inside it.
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
                                 WATCHDOG_PAUSE_PATH, Config, camera_index_explicit)
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
FIRST_FRAME_S = 5.0          # 9d (A-4, V-30): no usable frame this long after the open -> failed
CALIB_STALL_S = 30.0         # 9d (V-31): a calibration without progress this long is abandoned
LEASE_CALL_S = 5.0           # one lease request / hand-back over the pipe
CAMERA_JOIN_S = 5.0          # 9d-r2 (W-18): the end state waits this long for the camera thread
PREVIEW_MAX_FRAC = 0.5       # 9d-r2 (W-10): the preview takes at most this share of the work-area height
PREVIEW_MIN_K = 0.3          # ...and is never scaled below this share of its 96-dpi size
READY_WRAP_PX = 300          # 9d-r2 (W-10): the readiness panel's text width (96-dpi pixels)

# 9d-r2 (W-10): the readiness texts the layout is measured with (the longest one of the language)
_READY_TEXT_KEYS = ("enroll.ready.password.none", "enroll.ready.password.unreadable",
                    "enroll.ready.password.rejected", "enroll.ready.custody.bad",
                    "enroll.ready.camera.bad", "enroll.ready.service.bad", "enroll.ready.enrollment.bad")
# 9d-r2 (W-20): the one optional button slot of the button row
_EXTRA_MODES = {"retry": "enroll.btn.retry", "camera_on": "enroll.btn.camera_on",
                "cancel_calib": "enroll.btn.cancel_calib"}
# 9d-r2 (W-12): camera-session messages carry the session's gen last; a stale one is dropped
_CAMERA_MSGS = {"camera_switching", "camera_opened", "camera_failed", "camera_closed", "first_frame",
                "coach", "captured", "done_capture", "calib_shots"}

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


def fit_preview(work_w: int, work_h: int, chrome_w: int, chrome_h: int,
                scale: float) -> "tuple[int, int]":
    """9d-r2 (W-10): the preview size, in physical pixels, that keeps the whole wizard inside a
    work area of ``work_w`` x ``work_h``. ``chrome_w`` / ``chrome_h`` is the rest of the window
    (its full size -- title bar, borders, readiness panel, buttons -- minus the preview) at this
    ``scale``. The 96-dpi preview is scaled to ``scale``, kept to PREVIEW_MAX_FRAC of the work-area
    height and to what the chrome leaves free, never below PREVIEW_MIN_K; 4:3 is kept. Pure."""
    pw, ph = PREVIEW_W * scale, PREVIEW_H * scale
    k = min(1.0, work_h * PREVIEW_MAX_FRAC / ph, (work_h - chrome_h) / ph, (work_w - chrome_w) / pw)
    k = max(PREVIEW_MIN_K, k)
    return int(pw * k), int(ph * k)


def build_failure_text(raw: "str | None") -> str:
    """9d-r2 (W-19): a failed build in words (the raw reason goes to the log only)."""
    raw = (raw or "").strip()
    if raw in ("", "timeout"):
        return t("enroll.build.fail.timeout")
    if raw == "lease":
        return t("enroll.error.lease")
    key = f"why.{raw}"
    if t(key) != key:
        return t("enroll.build.fail.refusing", why=t(key))
    return t("enroll.build.fail.generic")


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


def built_message(resp: dict) -> "tuple[str, str]":
    """The wizard's line after a SUCCESSFUL build: (text, level). 9d (V-20): photos dropped as
    another person's face are reported, not silently absorbed."""
    n = int(resp.get("count", 0) or 0)
    try:
        others = max(0, int(resp.get("other_person", 0) or 0))
    except (TypeError, ValueError):
        others = 0
    pose = resp.get("pose") or {}
    if n > 0 and pose and pose_warning(float(pose.get("pitch", 0.0)), float(pose.get("yaw", 0.0))):
        text, level = t("enroll.guide.pose_warn", n=n), "warn"
    else:
        text, level = t("enroll.guide.done", n=n), "ok"
    if others:
        text, level = text + " " + t("enroll.guide.other_person", k=others), "warn"
    return text, level


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


def annotate(bgr, faces, level: str, size: "tuple[int, int] | None" = None):
    """The preview image. ``size`` is the preview in physical pixels (9d, V-43: scaled with the
    window's DPI); the default is the 96-dpi size."""
    pw, ph = size or (PREVIEW_W, PREVIEW_H)
    colour = _COACH_BGR.get(level, _COACH_BGR["warn"])
    img = bgr.copy()
    for row in faces:
        x, y, w, h = (int(v) for v in row[0:4])
        cv2.rectangle(img, (x, y), (x + w, y + h), colour, 3)
    img = cv2.flip(img, 1)
    h0, w0 = img.shape[:2]
    scale = min(pw / w0, ph / h0)
    nw, nh = int(w0 * scale), int(h0 * scale)
    img = cv2.resize(img, (nw, nh))
    canvas = np.zeros((ph, pw, 3), dtype=img.dtype)
    ox, oy = (pw - nw) // 2, (ph - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = img
    k = pw / PREVIEW_W
    cv2.ellipse(canvas, (pw // 2, ph // 2), (int(_GUIDE_AXIS_X * k), int(_GUIDE_AXIS_Y * k)),
                0, 0, 360, colour, 2)
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


# ---------------------------------------------------------------------------------------------
# Worker side: plain data + a queue. Nothing below touches Tk.
# ---------------------------------------------------------------------------------------------

class Session:
    """State shared between the Tk thread and the camera worker. Plain Python, one lock."""

    def __init__(self, cfg: Config, camera_name: str, gen: int = 0,
                 service_up: "threading.Event | None" = None):
        self.cfg = cfg
        self.camera_name = camera_name
        self.gen = gen                                  # 9d-r2 (W-12): tags its messages
        self.service_up = service_up                    # 9d-r2 (W-13): lease only after this
        self.stop = threading.Event()
        self.armed = threading.Event()
        self.lock = threading.Lock()
        self.capture_dir = ENROLL_DIR
        self.target = COUNT_DEFAULT
        self.captured = 0
        self.calib: "tuple[str, int] | None" = None     # ("frontal"|"left", shots still wanted)
        self.calib_names: dict = {"frontal": [], "left": []}
        self.preview_size = (PREVIEW_W, PREVIEW_H)     # 9d (V-43): set from the window's DPI


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


def _live_threads(threads) -> "list[threading.Thread]":
    """9e-0 (X-01): the camera threads of ``threads`` (one thread, a sequence, or None) still alive."""
    if threads is None:
        return []
    if isinstance(threads, threading.Thread):
        threads = [threads]
    return [t for t in threads if t is not None and t.is_alive()]


def camera_worker(s: Session, q: "queue.Queue", lease: "LeaseKeeper | None" = None,
                  prev: "threading.Thread | list[threading.Thread] | None" = None) -> None:
    """Preview + capture + calibration shots. Every message ends with the session's ``gen``:
    ("camera_switching", gen), ("camera_opened", gen), ("camera_failed", reason, gen),
    ("frame", rgb, gen), ("first_frame", gen), ("coach", CoachState, gen), ("captured", n, gen),
    ("done_capture", n, gen), ("calib_shots", phase, names, gen).

    9d (A-4, V-30): the camera lease is taken FIRST -- refused -> ("camera_failed", "lease") and the
    device is never opened -- and given back when this worker lets the device go.
    9d-r2: W-11 -- the worker does not start while the previous camera thread (``prev``) lives
    (it says "camera_switching" and waits), and it gives back only ITS OWN lease token;
    W-13 -- the lease is asked for only after the service has answered (``s.service_up``);
    W-15 -- any exception in the session, the calibration shots included, is "camera_failed".
    9e-0: X-01 -- ``prev`` is EVERY earlier camera thread still alive, not only the last one: after
    two quick changes the middle worker ends at once (its session was stopped) while the first may
    still hang in a native read() holding the device."""
    alive = _live_threads(prev)
    if alive:
        q.put(("camera_switching", s.gen))
        log.info("enroll camera: waiting for %d earlier camera session(s) to let the device go",
                 len(alive))
        while alive:
            if s.stop.is_set():
                return
            alive[0].join(0.25)
            alive = _live_threads(alive)
    if s.service_up is not None:
        while not s.service_up.wait(0.25):
            if s.stop.is_set():
                return
    if s.stop.is_set():
        return
    token = None
    if lease is not None:
        token = lease.acquire("camera")
        if token is None:
            q.put(("camera_failed", "lease", s.gen))
            return
    try:
        _camera_session(s, q)
    except Exception:
        log.exception("enroll camera session failed")
        q.put(("camera_failed", "error", s.gen))
    finally:
        if lease is not None:
            lease.release(token)              # the device is gone: this session's share only


def _camera_session(s: Session, q: "queue.Queue") -> None:
    gen = s.gen
    cap, why = _open_capture(s.cfg, s.camera_name)
    if cap is None:
        q.put(("camera_failed", why, gen))
        return
    try:
        q.put(("camera_opened", gen))          # W-15: the Tk side times the first frame from here
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
        first_deadline = time.monotonic() + FIRST_FRAME_S
        while not s.stop.is_set():
            if not first and time.monotonic() >= first_deadline:
                log.warning("enroll camera: no usable frame within %.0fs", FIRST_FRAME_S)
                q.put(("camera_failed", "no-frame", gen))
                return
            ok, frame = cap.read()
            if s.stop.is_set():
                return
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            frame_idx += 1
            if not first and float(frame.mean()) > BLACK_LUMA:
                first = True
                q.put(("first_frame", gen))
            if not first:
                continue                      # black frames of a camera still starting up
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
                q.put(("coach", coach, gen))
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
                            q.put(("calib_shots", phase, list(s.calib_names[phase]), gen))
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
                            q.put(("captured", n, gen))
                            if done:
                                s.armed.clear()
                                q.put(("done_capture", n, gen))
                        else:
                            log.error("enroll: could not save %s -- not counted", path.name)
                    except Exception:
                        log.exception("failed to save an enrollment shot")
            q.put(("frame", annotate(frame, faces, coach.level, s.preview_size), gen))
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
    """The camera lease (R10), counted per holder (9d-r2, W-11).

    Every holder -- a camera session, a build -- gets its own token from ``acquire`` and gives
    back exactly that token with ``release``. The service's lease (pause_camera) is asked for by
    the first holder and handed back (resume_camera) with the last one, so the ``finally`` of an
    old camera session can never hand back the lease a newer session or a build still needs.
    Acquire and release are serialised with their pipe calls. W-13: a lease request that gets no
    answer in time is followed by resume_camera -- the service may still act on it late -- and
    counts as refused."""

    def __init__(self):
        self._op = threading.Lock()
        self._tokens: set = set()
        self._renew_stop: "threading.Event | None" = None

    @property
    def held(self) -> bool:
        return bool(self._tokens)

    def acquire(self, holder: str = "camera") -> "object | None":
        with self._op:
            if not self._tokens:
                resp = pipe_call({"cmd": "pause_camera", "seconds": CAMERA_LEASE_S},
                                 timeout_s=LEASE_CALL_S)
                if not (resp and resp.get("ok")):
                    if resp is None:
                        log.warning("camera lease request (%s) got no answer -- taking it back "
                                    "with resume_camera", holder)
                        pipe_call({"cmd": "resume_camera"}, timeout_s=LEASE_CALL_S)
                    else:
                        log.warning("camera lease refused (%s): %s", holder, resp)
                    return None
                stop = threading.Event()
                self._renew_stop = stop
                threading.Thread(target=self._renew, args=(stop,), name="enroll-lease",
                                 daemon=True).start()
            token = (holder, object())
            self._tokens.add(token)
            log.info("camera lease: %s holds it (%d holder(s))", holder, len(self._tokens))
            return token

    def _renew(self, stop: threading.Event) -> None:
        while not stop.wait(LEASE_RENEW_S):
            resp = pipe_call({"cmd": "pause_camera", "seconds": CAMERA_LEASE_S}, timeout_s=LEASE_CALL_S)
            if not (resp and resp.get("ok")):
                log.warning("camera lease renewal failed: %s", resp)

    def release(self, token) -> bool:
        """Give back ``token``. False (and nothing else happens) for a token that is not held --
        already given back, or never given out."""
        with self._op:
            if token is None or token not in self._tokens:
                return False
            self._tokens.discard(token)
            log.info("camera lease: %s gave it back (%d holder(s) left)", token[0], len(self._tokens))
            if not self._tokens:
                self._hand_back()
            return True

    def release_all(self) -> None:
        """Close: every share goes, and the lease with them."""
        with self._op:
            had = bool(self._tokens)
            self._tokens.clear()
            if had:
                self._hand_back()

    def _hand_back(self) -> None:
        if self._renew_stop is not None:
            self._renew_stop.set()
            self._renew_stop = None
        for _ in range(3):              # F-185: a busy server may need a second try
            resp = pipe_call({"cmd": "resume_camera"}, timeout_s=LEASE_CALL_S)
            if resp and resp.get("ok"):
                return
            time.sleep(1.0)
        log.warning("resume_camera was not confirmed; the lease lapses by itself")


def build_worker(q: "queue.Queue", req: dict, timeout_s: float, pause_ttl: float,
                 lease: "LeaseKeeper | None" = None) -> None:
    """9d-r2 (W-14): the build holds its own share of the camera lease -- with the camera off it
    takes the lease itself, for the build only."""
    token = None
    if lease is not None:
        token = lease.acquire("build")
        if token is None:
            q.put(("built", {"ok": False, "reason": "lease"}))
            return
    paused = _pause_watchdog(pause_ttl)
    try:
        resp = pipe_call(req, timeout_s=timeout_s)
    finally:
        _resume_watchdog(paused)
        if lease is not None:
            lease.release(token)
    q.put(("built", resp))


def calibrate_worker(q: "queue.Queue", frontal: list, left: list) -> None:
    q.put(("calibrated", pipe_call({"cmd": "calibrate_turn", "frontal": frontal, "left": left},
                                   timeout_s=30.0)))


def readiness_worker(q: "queue.Queue", camera_released: bool) -> None:
    """F-180: what face sign-in needs, checked for real. 9d-r2 (W-18): ``camera_released`` is true
    only when the camera thread was seen to end and no lease share is held."""
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
        "camera": bool(camera_released),
    }
    q.put(("readiness", checks, pwd, bool(status.get("password_rejected"))))


def release_and_check_worker(q: "queue.Queue", s: "Session | None",
                             cam: "threading.Thread | list[threading.Thread] | None",
                             lease: LeaseKeeper) -> None:
    """9d (A-4): close the camera (the worker gives its lease share back with it), then run the
    readiness check. 9d-r2 (W-18): "camera handed back" only when the camera thread was seen to
    END (join confirmed) -- a thread still in a native read keeps its share, and the check says
    the camera is still busy."""
    gen = s.gen if s is not None else None
    if s is not None:
        s.stop.set()
        s.armed.clear()
    deadline = time.monotonic() + CAMERA_JOIN_S
    for th in _live_threads(cam):                 # X-01: every camera thread, not only the last
        th.join(timeout=max(0.0, deadline - time.monotonic()))
    joined = not _live_threads(cam)
    if not joined:
        log.warning("enroll camera thread still holds the device after %.0fs", CAMERA_JOIN_S)
    q.put(("camera_closed", gen))
    readiness_worker(q, joined and not lease.held)


def close_worker(q: "queue.Queue", s: "Session | None",
                 cam: "threading.Thread | list[threading.Thread] | None", lease: LeaseKeeper) -> None:
    """9d (V-42): Close -- stop the camera and give the lease back off the Tk thread, then let the
    Tk thread destroy the window."""
    if s is not None:
        s.stop.set()
        s.armed.clear()
    deadline = time.monotonic() + 3.0
    for th in _live_threads(cam):                 # X-01: every camera thread still alive
        th.join(timeout=max(0.0, deadline - time.monotonic()))
    if _live_threads(cam):
        log.warning("enroll camera thread did not exit within 3s")
    lease.release_all()
    q.put(("closed",))


def wipe_worker(q: "queue.Queue") -> None:
    q.put(("wiped", pipe_call({"cmd": "clear_enrollment"}, timeout_s=30.0)))


def default_camera_name(devices, explicit_index: bool) -> str:
    """9d (V-34): for a new install (no camera_name, no camera_index of its own) the camera that is
    DirectShow index 0 right now -- chosen BY NAME, so it stays the same camera when another one is
    plugged in later. "" when there is none (or the old index setting is kept)."""
    if explicit_index:
        return ""
    for d in devices or []:
        if getattr(d, "index", None) == 0 and getattr(d, "name", ""):
            return d.name
    return ""


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
    from .ui import px
    ttk.Label(frm, text=t("enroll.confirm.mode.body"), wraplength=px(parent, 420),  # W-20: DPI
              justify="left").pack(
        anchor="w", pady=(0, 12))
    btns = ttk.Frame(frm)
    btns.pack(anchor="e")

    def choose(v):
        result["v"] = v
        top.destroy()
    ttk.Button(btns, text=t("enroll.mode.replace"), command=lambda: choose("replace")).pack(
        side="left", padx=4)
    b = ttk.Button(btns, text=t("enroll.mode.add"), command=lambda: choose("add"), default="active")
    b.pack(side="left", padx=4)
    ttk.Button(btns, text=t("enroll.btn.cancel"), command=lambda: choose(None)).pack(side="left", padx=4)
    top.bind("<Escape>", lambda _e: choose(None))
    # 9d (V-41): Enter is the NON-destructive choice (Add), and the focus starts there.
    top.bind("<Return>", lambda _e: choose("add"))
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
    from .ui import px
    ttk.Label(frm, text=t("enroll.unbuilt.body", n=n), wraplength=px(parent, 420),  # W-20: DPI
              justify="left").pack(
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
        from .ui import apply_scaling, set_app_icon
        self.root = tk.Tk()
        self.root.title(t("enroll.title"))
        self.scale = apply_scaling(self.root)
        set_app_icon(self.root)                      # 9d (V-48)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.q: "queue.Queue" = queue.Queue()
        self.cfg = Config.load()
        self.session: "Session | None" = None
        self.cam_thread: "threading.Thread | None" = None
        self.cam_threads: "list[threading.Thread]" = []    # 9e-0 (X-01): every one still alive
        # 9d-r2 (W-12): the camera session whose frames may be drawn; None while the camera is off
        self.cam_gen: "int | None" = None
        self._gen = 0
        self.lease = LeaseKeeper()
        self.stop_all = threading.Event()
        self.service_up = threading.Event()          # 9d-r2 (W-13): the service has answered
        self.service_ready = False
        self.refusing: "str | None" = None
        self.camera_ok = False
        self.building = False
        self.calibrating = False
        self.mode: "str | None" = None
        self.devices: list = []
        self._tk_image = None
        self._preview_blank = True
        self._extra_mode: "str | None" = None
        self._relayout_pending = False
        self._pending_camera_save = ""       # 9d (V-34): the default camera, saved after a build
        self._calib_progress_at = 0.0        # 9d (V-31)
        self._calib_gen = 0
        self.closing = False

        # A stale pending session (a tray Quit killed an earlier wizard mid-Replace) holds face
        # images with no purpose -- removed before anything else (F-182).
        try:
            from face_service.datadir import remove_tree_no_follow
            if ENROLL_PENDING_DIR.exists():
                remove_tree_no_follow(ENROLL_PENDING_DIR)
                log.info("removed a stale pending enrollment session")
        except Exception:
            log.exception("could not remove the stale pending session")

        from .ui import px
        self._px = lambda n: px(self.root, n)
        self.preview_size = (self._px(PREVIEW_W), self._px(PREVIEW_H))   # 9d (V-43)
        self._build_ui()
        self._fit_layout()                                                # 9d-r2 (W-10)
        self._set_line(t("enroll.status.connecting"), "info")
        self._refresh_buttons()
        threading.Thread(target=service_wait_worker, args=(self.stop_all, self.q),
                         name="enroll-service-wait", daemon=True).start()
        self._load_devices()
        self._start_camera()
        self.root.after(33, self._drain)
        self.root.after(250, self._keep_in_work_area)

    # ---- UI ----
    def _build_ui(self) -> None:
        from .ui import derived_font
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        self.outer = outer
        frm = ttk.Frame(outer)
        frm.grid(row=0, column=0, sticky="n")
        self.left = frm
        cam_row = ttk.Frame(frm)
        cam_row.pack(fill="x", pady=(0, 6))
        ttk.Label(cam_row, text=t("enroll.camera") + ":").pack(side="left")
        self.cam_var = tk.StringVar(master=self.root)
        self.cam_combo = ttk.Combobox(cam_row, textvariable=self.cam_var, state="readonly", width=40)
        self.cam_combo.pack(side="left", padx=6)
        self.cam_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_camera_chosen())

        pw, ph = self.preview_size
        self.preview = tk.Label(frm, background="#222", width=pw, height=ph)
        self.preview.pack(pady=(0, 8))
        self._blank_preview()

        # 9d-r2 (W-21): named fonts derived from TkDefaultFont
        self.line = ttk.Label(frm, text="", wraplength=pw, justify="center",
                              font=derived_font(self.root, "FuWizardLine", delta=2))
        self.line.pack(fill="x", pady=(0, 4))
        # F-181: the shot counter and the pose instruction live in their own rows.
        prog = ttk.Frame(frm)
        prog.pack(fill="x", pady=(0, 2))
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=COUNT_DEFAULT)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.count_lbl = ttk.Label(prog, text="", font=derived_font(self.root, "FuWizardCount", delta=1))
        self.count_lbl.pack(side="right")
        attach_tooltip(self.progress, "enroll.progress.tip")
        self.hint = ttk.Label(frm, text=t("enroll.pose_hint"), foreground="#555", wraplength=pw,
                              justify="left")
        self.hint.pack(anchor="w", pady=(0, 6))

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
        self.btns = btns
        self.start_btn = ttk.Button(btns, text=t("enroll.btn.start"), command=self._on_start_stop)
        self.start_btn.pack(side="left", padx=3)
        self.build_btn = ttk.Button(btns, text=t("enroll.btn.build"), command=self._on_build)
        self.build_btn.pack(side="left", padx=3)
        self.wipe_btn = ttk.Button(btns, text=t("enroll.btn.wipe"), command=self._on_wipe)
        self.wipe_btn.pack(side="left", padx=3)
        self.close_btn = ttk.Button(btns, text=t("enroll.btn.close"), command=self._on_close)
        self.close_btn.pack(side="right", padx=3)
        # 9d-r2 (W-20): ONE slot for Retry / Turn the camera on again / Cancel calibration (they
        # never show together), and a spacer as wide as the row with its widest text -- the
        # window does not change width when the button appears.
        self.extra_btn = ttk.Button(btns, text="")
        widest = 0
        for mode in _EXTRA_MODES:
            self.extra_btn.configure(text=t(_EXTRA_MODES[mode]))
            self.extra_btn.pack(side="left", padx=3, after=self.wipe_btn)
            btns.update_idletasks()
            widest = max(widest, btns.winfo_reqwidth())
            self.extra_btn.pack_forget()
        ttk.Frame(frm, width=widest, height=1).pack(anchor="w")

        # The end-state panel (F-180), shown after a build -- 9d-r2 (W-10): in its own column
        # beside the preview, its buttons stacked, so it is always on screen with them.
        self.ready_frm = ttk.LabelFrame(outer, text=t("enroll.ready.title"), padding=8)
        self.ready_lines: dict = {}
        wrap = self._px(READY_WRAP_PX)
        for i, key in enumerate(("service", "custody", "enrollment", "password", "camera")):
            lbl = ttk.Label(self.ready_frm, text="", wraplength=wrap, justify="left")
            lbl.grid(row=i, column=0, sticky="w", pady=1)
            self.ready_lines[key] = lbl
        rb = ttk.Frame(self.ready_frm)
        rb.grid(row=10, column=0, sticky="we", pady=(8, 0))
        self.calib_btn = ttk.Button(rb, text=t("enroll.btn.calibrate"), command=self._on_calibrate)
        self.calib_btn.pack(fill="x", pady=2)
        self.pwd_btn = ttk.Button(rb, text=t("enroll.btn.set_password"), command=self._on_set_password)
        self.pwd_btn.pack(fill="x", pady=2)
        ttk.Button(rb, text=t("enroll.btn.check_again"), command=self._check_ready).pack(fill="x", pady=2)

        self.root.bind("<Escape>", lambda _e: self._on_close())
        # 9d (V-43): Enter presses the focused button (Start when nothing else has the focus); Tab
        # follows the visual order: camera, number of photos, Start, Build, Delete, Close.
        self.root.bind("<Return>", self._on_enter)
        self.root.bind("<KP_Enter>", self._on_enter)
        order = [self.cam_combo, self.count_spin, self.start_btn, self.build_btn, self.wipe_btn,
                 self.close_btn]
        for w in order:
            w.lift()
        self.start_btn.focus_set()
        self._refresh_existing()

    def _measure_chrome(self) -> "tuple[int, int]":
        """The window's size minus the preview, in its fullest state: readiness panel shown with
        its longest texts, a four-line status line, title bar and borders."""
        from .ui import frame_extra
        r = self.root
        saved = self.line.cget("text")
        saved_ready = {k: lbl.cget("text") for k, lbl in self.ready_lines.items()}
        shown = bool(self.ready_frm.winfo_manager())
        longest = max((t(k) for k in _READY_TEXT_KEYS), key=len)
        try:
            self.line.configure(text="\n".join(["M"] * 4))
            for lbl in self.ready_lines.values():
                lbl.configure(text="✗ " + longest)
            if not shown:
                self._grid_ready()
            r.update_idletasks()
            dw, dh = frame_extra(r)
            return (r.winfo_reqwidth() + dw - self.preview.winfo_reqwidth(),
                    r.winfo_reqheight() + dh - self.preview.winfo_reqheight())
        finally:
            self.line.configure(text=saved)
            for k, lbl in self.ready_lines.items():
                lbl.configure(text=saved_ready[k])
            if not shown:
                self.ready_frm.grid_remove()

    def _fit_layout(self) -> None:
        """9d-r2 (W-10): size the preview so that the whole window -- readiness panel and its
        buttons, the button row with its optional button, a four-line status line, title bar and
        borders -- fits the work area of its monitor (``fit_preview``). A narrower preview wraps
        the texts under it into more lines, so the fit is repeated until it holds."""
        from .ui import work_area_of
        left, top, right, bottom = work_area_of(self.root)
        for _ in range(4):
            chrome_w, chrome_h = self._measure_chrome()
            size = fit_preview(right - left, bottom - top, chrome_w, chrome_h, self.scale)
            self.chrome = (chrome_w, chrome_h)
            if size == self.preview_size:
                break
            self._set_preview_size(size)
        log.info("wizard layout: work area %dx%d, scale %.2f, chrome %dx%d -> preview %dx%d",
                 right - left, bottom - top, self.scale, self.chrome[0], self.chrome[1],
                 *self.preview_size)

    def _set_preview_size(self, size: "tuple[int, int]") -> None:
        self.preview_size = size
        pw, ph = size
        self.preview.configure(width=pw, height=ph)
        self.line.configure(wraplength=pw)
        self.hint.configure(wraplength=pw)
        if self.session is not None:
            self.session.preview_size = size
        self._blank_preview()

    def _grid_ready(self) -> None:
        self.ready_frm.grid(row=0, column=1, sticky="n", padx=(self._px(12), 0))

    def _relayout(self) -> None:
        """9d-r2 (W-10): after any change of the layout, the window goes back inside the work area."""
        if self._relayout_pending:
            return
        self._relayout_pending = True
        try:
            self.root.after_idle(self._keep_in_work_area)
        except tk.TclError:
            self._relayout_pending = False

    def _keep_in_work_area(self) -> None:
        self._relayout_pending = False
        if self.closing:
            return
        from .ui import keep_in_work_area
        if keep_in_work_area(self.root):
            log.info("wizard moved back inside the work area")

    def _set_extra(self, mode: "str | None") -> None:
        self._extra_mode = mode
        if mode is None:
            self.extra_btn.pack_forget()
        else:
            cmd = (lambda: self._abort_calibration("enroll.calib.cancelled")) if mode == "cancel_calib" \
                else self._on_retry
            self.extra_btn.configure(text=t(_EXTRA_MODES[mode]), command=cmd)
            if not self.extra_btn.winfo_manager():
                self.extra_btn.pack(side="left", padx=3, after=self.wipe_btn)
        self._relayout()

    def _on_enter(self, _e=None) -> None:
        w = self.root.focus_get()
        if isinstance(w, ttk.Button):
            if str(w.cget("state")) != "disabled":
                w.invoke()
        elif str(self.start_btn.cget("state")) != "disabled":
            self.start_btn.invoke()

    def _set_line(self, text: str, level: str = "info") -> None:
        if self.line.cget("text") != text:
            self.line.configure(text=text, foreground=_COACH_FG.get(level, "#222"))
            self._relayout()
        else:
            self.line.configure(foreground=_COACH_FG.get(level, "#222"))

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
        if self._extra_mode in ("retry", "camera_on"):
            self.extra_btn.configure(state="disabled" if self.building else "normal")

    def _capture_dir(self):
        return ENROLL_PENDING_DIR if self.mode == "replace" else ENROLL_DIR

    # ---- camera ----
    def _load_devices(self) -> None:
        from face_service.camera_devices import list_video_devices
        try:
            devs = list(list_video_devices())
        except Exception:
            devs = []
        self.devices = [d.name for d in devs if d.name]
        values = list(self.devices)
        cur = self.cfg.camera_name
        explicit = camera_index_explicit()
        if not cur:
            name = default_camera_name(devs, explicit)
            if name:
                # 9d (V-34): a new install -- the camera at DirectShow 0 now, by name; saved after
                # the first successful build.
                self.cfg.camera_name = cur = name
                self._pending_camera_save = name
                log.info("new install: camera %r chosen by name (DirectShow index 0)", name)
        if not cur:
            # 9d-r2 (W-20): "(older setting)" only for a config.toml that sets camera_index itself;
            # a new install with no camera at DirectShow 0 says so
            label = (t("settings.camera.by_index", i=self.cfg.camera_index) if explicit
                     else t("enroll.camera.not_found"))
            values = [label] + values
            self.cam_var.set(label)
        else:
            if cur not in values:
                values.append(cur)
            self.cam_var.set(cur)
        self.cam_combo.configure(values=values)

    def _start_camera(self) -> None:
        prev = _live_threads(self.cam_threads + [self.cam_thread])     # X-01: all, not the last
        if self.session is not None:
            self.session.stop.set()
        self.camera_ok = False
        self._gen += 1
        self.cam_gen = self._gen
        self.session = Session(self.cfg, self.cfg.camera_name, gen=self._gen, service_up=self.service_up)
        self.session.preview_size = self.preview_size
        # 9d (A-4): the worker takes its lease share before it opens the camera; 9d-r2 (W-11): it
        # starts only once the previous camera thread has ended; 9e-0 (X-01): EVERY earlier camera
        # thread still alive -- the UI keeps "switching camera" meanwhile
        self.cam_thread = threading.Thread(target=camera_worker,
                                           args=(self.session, self.q, self.lease, prev),
                                           name="enroll-camera", daemon=True)
        self.cam_threads = prev + [self.cam_thread]
        self.cam_thread.start()
        self._set_extra(None)
        self._refresh_buttons()

    def _on_camera_chosen(self) -> None:
        name = self.cam_var.get()
        if name not in self.devices:
            name = ""                              # the by-index / not-found entry
        if name == self.cfg.camera_name:
            return
        self.cfg.camera_name = name
        self._pending_camera_save = ""                 # the user chose: saved right now
        threading.Thread(target=save_camera_worker, args=(self.q, name), daemon=True).start()
        log.info("camera chosen: %r", name)
        self._blank_preview()
        self._start_camera()

    def _on_retry(self) -> None:
        # 9d (A-4): a fresh lease and a fresh open (the camera worker takes both)
        self._set_extra(None)
        self._set_line(t("enroll.status.retrying"), "info")
        self.root.after(1500, self._start_camera)          # F-179: a beat after the release

    def _first_frame_due(self, gen: int) -> None:
        """9d-r2 (W-15): the first-frame limit is kept HERE, on the Tk side -- a worker stuck in a
        native read() cannot report its own timeout."""
        if self.closing or self.session is None or self.session.gen != gen:
            return
        if self.cam_gen == gen and not self.camera_ok:
            log.warning("enroll camera: no usable frame within %.0fs (Tk timer)", FIRST_FRAME_S)
            self.session.stop.set()
            self._camera_failed("no-frame")

    # ---- the queue ----
    def _drain(self) -> None:
        frame = None
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "frame":
                    frame = (msg[1], msg[-1])        # only the newest one is drawn
                else:
                    self._handle(msg)
        except queue.Empty:
            pass
        # 9d-r2 (W-12): a frame is drawn only for the camera session that is live NOW -- one that
        # was queued before camera_closed / camera_failed (or a switch) is dropped
        if frame is not None and self.cam_gen is not None and frame[1] == self.cam_gen:
            try:
                self._tk_image = ImageTk.PhotoImage(Image.fromarray(frame[0]), master=self.root)
                self.preview.configure(image=self._tk_image)
                self._preview_blank = False
            except tk.TclError:
                pass
        try:
            self.root.after(33, self._drain)
        except tk.TclError:
            pass

    def _handle(self, msg) -> None:
        kind = msg[0]
        if kind in _CAMERA_MSGS and (self.session is None or msg[-1] != self.session.gen):
            log.debug("stale camera message dropped: %s", kind)
            return
        if kind == "service":
            _k, what, state, why = msg
            if what == "ready":
                self.service_ready = True
                self.service_up.set()                  # 9d-r2 (W-13): the camera may ask now
                self.refusing = why if state == "refusing" else None
                if self.refusing:
                    self._set_line(t("enroll.error.refusing", why=t(f"why.{self.refusing}")), "err")
                elif self.camera_ok:
                    self._set_line(t("enroll.guide.idle"), "info")
            else:
                self._set_line(t("enroll.error.service_down"), "err")
            self._refresh_buttons()
        elif kind == "camera_switching":
            self._set_line(t("enroll.status.switching"), "info")
        elif kind == "camera_opened":
            self.root.after(int(FIRST_FRAME_S * 1000), self._first_frame_due, msg[-1])
        elif kind == "camera_failed":
            self._camera_failed(msg[1])
        elif kind == "camera_closed":
            # 9d (A-4): the end state gave the camera and its lease back
            self.camera_ok = False
            self.cam_gen = None
            self._blank_preview()
            self._set_extra("camera_on")
            self._refresh_buttons()
        elif kind == "closed":
            self._finish_close()
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

    def _camera_failed(self, reason: str) -> None:
        self.camera_ok = False
        self.cam_gen = None
        self._blank_preview()
        if self.calibrating:                         # the calibration cannot go on without it
            self.calibrating = False
            self._calib_gen += 1
            if self.session is not None:
                with self.session.lock:
                    self.session.calib = None
        key = {"not-found": "enroll.error.camera_missing", "lease": "enroll.error.lease",
               "no-frame": "enroll.error.no_frame",
               "error": "enroll.error.camera_error"}.get(reason, "enroll.error.camera")
        self._set_line(t(key, name=self.cfg.camera_name or "?"), "err")
        self._set_extra("retry")
        self._refresh_buttons()

    def _blank_preview(self) -> None:
        """The camera is not in use: an empty preview, not its last frame."""
        try:
            self._tk_image = ImageTk.PhotoImage(Image.new("RGB", self.preview_size, (24, 24, 24)),
                                                master=self.root)
            self.preview.configure(image=self._tk_image)
            self._preview_blank = True
        except tk.TclError:
            pass

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
        if not self.lease.held:                  # 9d (A-4): the camera worker holds it with the device
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
        # 9d-r2 (W-14): the build takes its own share of the lease on its worker -- it works with
        # the camera off too (no lease call on the Tk thread, V-42)
        self.building = True
        self._refresh_buttons()
        self._set_line(t("enroll.guide.building"), "info")
        req = {"cmd": "build_enrollment"}
        if self.mode == "replace":
            req["replace"] = True
        threading.Thread(target=build_worker,
                         args=(self.q, req, build_timeout_s(n), self.cfg.watchdog_pause_ttl_s, self.lease),
                         name="enroll-build", daemon=True).start()

    def _on_built(self, resp) -> None:
        self.building = False
        self._refresh_existing()
        if resp and resp.get("ok"):
            self.mode = None                        # F-187: the next Start asks again
            self._set_line(*built_message(resp))    # 9d (V-20): + photos of another person
            if self._pending_camera_save:           # 9d (V-34): the default camera, by name
                threading.Thread(target=save_camera_worker, args=(self.q, self._pending_camera_save),
                                 daemon=True).start()
                self._pending_camera_save = ""
            self._show_ready_panel()
            self._offer_calibration()
        else:
            raw = (resp or {}).get("reason") or ("timeout" if resp is None else "")
            log.warning("building the face profile failed: reason=%r", raw)   # W-19: the code, here only
            why = humanize_reason(raw)
            if why:
                self._set_line(t("enroll.guide.build_rejected", why=why), "err")
                messagebox.showerror(t("enroll.title"), t("enroll.build.failed", why=why), parent=self.root)
            else:
                text = build_failure_text(raw)
                self._set_line(text, "err")
                messagebox.showerror(t("enroll.title"), text, parent=self.root)
        self._refresh_buttons()

    # ---- calibration (R6 / F-117) ----
    def _offer_calibration(self) -> None:
        if not self.camera_ok:
            self._release_and_check()
            return
        if messagebox.askyesno(t("enroll.calib.title"), t("enroll.calib.offer"), parent=self.root):
            self._on_calibrate()
        else:
            self._release_and_check()

    def _on_calibrate(self) -> None:
        if self.calibrating:
            return
        if self.session is None or not self.camera_ok:
            # 9d-r2 (W-14): say what to do instead of doing nothing
            self._set_line(t("enroll.calib.camera_off"), "warn")
            return
        if not self.lease.held:
            self._set_line(t("enroll.error.lease"), "err")
            return
        self.calibrating = True
        self._calib_gen += 1
        self._calib_progress_at = time.monotonic()
        self._set_extra("cancel_calib")
        self._refresh_buttons()
        with self.session.lock:
            self.session.calib_names = {"frontal": [], "left": []}
            self.session.calib = ("frontal", CALIB_SHOTS)
        self._set_line(t("enroll.calib.look_straight"), "info")
        self.root.after(1000, self._calib_watch, self._calib_gen)

    def _calib_watch(self, gen: int) -> None:
        """9d (V-31): no progress for CALIB_STALL_S -> the calibration is abandoned."""
        if not self.calibrating or gen != self._calib_gen or self.closing:
            return
        if time.monotonic() - self._calib_progress_at >= CALIB_STALL_S:
            self._abort_calibration("enroll.calib.stalled")
            return
        self.root.after(1000, self._calib_watch, gen)

    def _abort_calibration(self, key: str) -> None:
        """9d (V-31): Cancel, or a stall: stop collecting, say so, close the camera and hand the
        lease back (the end state), keep the default turn direction."""
        if not self.calibrating:
            return
        self.calibrating = False
        self._calib_gen += 1                  # a late calibrate_turn reply is ignored
        if self.session is not None:
            with self.session.lock:
                self.session.calib = None
        self._set_extra(None)
        self._set_line(t(key), "warn")
        self._release_and_check()

    def _on_calib_shots(self, phase: str, names: list) -> None:
        if not self.calibrating:
            return
        self._calib_progress_at = time.monotonic()
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
        if not self.calibrating:
            return                                # cancelled or stalled meanwhile (V-31)
        self.calibrating = False
        self._set_extra(None)
        if resp and resp.get("ok"):
            self._set_line(t("enroll.calib.done"), "ok")
        else:
            reason = (resp or {}).get("reason") or ("timeout" if resp is None else "?")
            log.warning("head-turn calibration failed: reason=%r", reason)   # W-19: the code, here only
            key = {"turn-too-small": "enroll.calib.too_small",
                   "no-face": "enroll.calib.no_face"}.get(reason, "enroll.calib.failed")
            self._set_line(t(key), "warn")
        self._release_and_check()

    # ---- end state (F-180) ----
    def _release_and_check(self) -> None:
        # 9d (A-4): the camera closes and its lease share goes back with it, then the readiness check
        threading.Thread(target=release_and_check_worker,
                         args=(self.q, self.session, self._camera_threads(), self.lease),
                         name="enroll-ready", daemon=True).start()
        self._refresh_buttons()

    def _camera_threads(self) -> "list[threading.Thread]":
        """9e-0 (X-01): every camera thread still alive (a hung one included)."""
        return _live_threads(self.cam_threads + [self.cam_thread])

    def _check_ready(self) -> None:
        # 9d-r2 (W-18): the camera counts as handed back only when its thread has ended and no
        # lease share is held (reading them is no pipe call)
        released = not self._camera_threads() and not self.lease.held
        threading.Thread(target=readiness_worker, args=(self.q, released), daemon=True).start()

    def _show_ready_panel(self) -> None:
        if not self.ready_frm.winfo_manager():
            self._grid_ready()
            self._relayout()

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
        self._relayout()

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
        if self.closing:
            return
        if self.building:
            messagebox.showinfo(t("enroll.title"), t("enroll.busy_close"), parent=self.root)
            return                                  # F-185
        if self.calibrating:                        # 9d (V-31): a calibration never traps the window
            self.calibrating = False
            self._calib_gen += 1
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
        self.closing = True
        try:
            self.root.withdraw()
        except Exception:
            pass
        # 9d (V-42): the camera stop and the lease release run on a worker; "closed" comes back
        threading.Thread(target=close_worker, args=(self.q, self.session, self._camera_threads(), self.lease),
                         name="enroll-close", daemon=True).start()

    def _finish_close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
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
