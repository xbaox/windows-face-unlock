"""Enrollment wizard — live camera preview, auto-capture, build embeddings.

Replaces the old ``python -m tools.enroll capture`` console flow. Runs as
its OWN process (``python -m presence_monitor.enroll_gui``), spawned by the
tray; see ``main()`` at the bottom for why that matters.

Flow
----
1. ``pause_camera`` on the FaceService so we own the webcam, re-armed
   periodically for as long as the window lives.
2. Open the camera in a background thread and publish BGR frames.
3. Render each frame with a green box around detected faces (YuNet).
4. When capture is armed and a face has been visible long enough, save
   the frame as JPG into ``ENROLL_DIR`` and increment the counter.
5. When the user hits "Build", call ``build_enrollment`` on the service
   which runs the ONNX/InsightFace engine (buffalo_l, ArcFace embeddings)
   and writes ``embeddings.npz``.
6. On close, always ``resume_camera`` so probes come back on.
"""
from __future__ import annotations
import logging
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Callable, NamedTuple

import cv2
from PIL import Image, ImageTk

from face_service.config import ENROLL_DIR, EMBED_PATH, Config
from face_service.detector import FaceDetector
from face_service.enroll_qc import frame_quality, qc_reasons
from face_service.i18n import set_language, t

from .monitor import pipe_call
from .widgets import InfoButton, Tooltip, attach_tooltip

# Explicit name, NOT __name__: this module is also the process entry point, and under
# `python -m presence_monitor.enroll_gui` __name__ is "__main__" -- which would label every line
# in enroll.log as "__main__" and make it ungreppable against the tray's own logs. Pinning the
# canonical dotted name keeps records identical whether the module is imported or run.
log = logging.getLogger("presence_monitor.enroll_gui")

PREVIEW_W = 480
PREVIEW_H = 360
CAPTURE_COOLDOWN_S = 1.0    # min gap between auto captures
FACE_STABLE_FRAMES = 3      # face must be seen this many frames before arming capture
DETECT_EVERY_N_FRAMES = 2   # YuNet is fast but skipping halves CPU
CAMERA_LEASE_S = 120        # ask the service to hand the camera over for this long. SHORT on
                            # purpose, and only workable because of the renewal below: the lease
                            # is the sole thing keeping the service off the webcam, and nothing
                            # cancels it if this wizard dies without sending resume_camera. So it
                            # doubles as the worst-case hostage window -- a dead wizard gives the
                            # camera back within CAMERA_LEASE_S, while a live one just keeps
                            # re-arming. The old 300 was five minutes of blindness for that case.
LEASE_RENEW_S = 45          # re-arm interval. Comfortably under CAMERA_LEASE_S so two renewals
                            # in a row can fail before the lease actually lapses.
CAMERA_READ_TIMEOUT_MS = 1000  # open/read timeout hint; only MSMF honors it, and NOT on
                               # this hardware -- kept as cross-HW insurance only
                               # (wizard-local; NOT one of the service camera_* knobs)

# ---- Framing coach (wizard UX only -- NOT recognition or QC numbers) ----
# These shape the on-screen advice and the FRAMING half of the capture gate.
# The QUALITY half (sharpness / exposure) is delegated to face_service.enroll_qc
# so the wizard and the build step judge a frame with ONE set of thresholds;
# nothing below is a matching, liveness or QC constant.
COACH_AREA_MIN_FRAC = 0.06     # face box smaller than this share of the frame -> "closer"
COACH_AREA_MAX_FRAC = 0.38     # larger -> "move back"
COACH_OFFSET_MAX_FRAC = 0.18   # box centre may sit this far off the frame centre
COACH_FACE_ASPECT = 0.8        # nominal face w/h, used only to draw the guide oval

# On-screen guide oval, in PREVIEW pixels. Sized to the midpoint of the accepted
# area band so "fill the oval" and the area gate agree by construction.
_GUIDE_AREA_PX = (COACH_AREA_MIN_FRAC + COACH_AREA_MAX_FRAC) / 2 * PREVIEW_W * PREVIEW_H
_GUIDE_AXIS_Y = int((_GUIDE_AREA_PX / COACH_FACE_ASPECT) ** 0.5) // 2
_GUIDE_AXIS_X = int(_GUIDE_AXIS_Y * COACH_FACE_ASPECT)

# Coach level -> named ttk style (see _init_styles) and preview box colour (BGR).
_COACH_STYLE = {
    "ok": "Coach.Ok.TLabel",
    "warn": "Coach.Warn.TLabel",
    "err": "Coach.Err.TLabel",
}
_COACH_BGR = {"ok": (60, 190, 90), "warn": (40, 170, 235), "err": (60, 60, 210)}

# enroll_qc token -> i18n key. Tokens embed the live threshold (``blur<80``), so
# only the head before the comparison operator is matched.
_REASON_KEY_BY_TOKEN = {
    "det": "enroll.reason.det",
    "blur": "enroll.reason.blur",
    "dark": "enroll.reason.dark",
    "bright": "enroll.reason.bright",
    "no-face": "enroll.reason.no_face",
    "unreadable": "enroll.reason.unreadable",
    "crop-failed": "enroll.reason.crop_failed",
}
# Quality advice, in the order the coach reports it (exposure before focus).
_COACH_KEY_BY_TOKEN = {
    "dark": "enroll.coach.dark",
    "bright": "enroll.coach.bright",
    "blur": "enroll.coach.blur",
}


def _count_enroll_images() -> int:
    try:
        return sum(
            1 for p in ENROLL_DIR.iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        )
    except FileNotFoundError:
        return 0


def _has_embeddings() -> bool:
    return EMBED_PATH.exists()


class CoachState(NamedTuple):
    """One frame's verdict: what to tell the user, and whether it may be saved."""
    key: str      # i18n key for the guidance line
    ok: bool      # True == this frame passes the live capture gate
    level: str    # "ok" | "warn" | "err" -> style + preview box colour


class _YuNetFace:
    """Adapt a YuNet row to the InsightFace shape ``enroll_qc`` expects.

    ``enroll_qc.frame_quality`` reads ``.bbox``, ``.kps`` and ``.det_score`` off
    an InsightFace face. YuNet hands back a flat 15-float row whose box is
    (x, y, w, h) where InsightFace uses (x1, y1, x2, y2), so the conversion
    lives here -- enroll_qc itself is used strictly read-only.
    """
    __slots__ = ("bbox", "kps", "det_score")

    def __init__(self, row):
        import numpy as np
        x, y, w, h = (float(v) for v in row[0:4])
        self.bbox = np.array([x, y, x + w, y + h], dtype=np.float32)
        pts = np.asarray(row[4:14], dtype=np.float32).reshape(5, 2)
        # norm_crop's reference landmarks put the IMAGE-left eye first, then the
        # image-right one (same for the mouth corners). YuNet names its pair from
        # the SUBJECT's point of view, which is the mirror of that, so sort each
        # pair by x rather than trusting the column order -- a swapped pair
        # silently mirrors the aligned crop and skews the numbers we compare
        # against the build step.
        eyes = pts[0:2][np.argsort(pts[0:2, 0])]
        mouth = pts[3:5][np.argsort(pts[3:5, 0])]
        self.kps = np.stack([eyes[0], eyes[1], pts[2], mouth[0], mouth[1]])
        self.det_score = float(row[14])


def _token_head(tok: str) -> str:
    """``blur<80`` -> ``blur``; ``no-face`` -> ``no-face``."""
    for sep in ("<", ">"):
        i = tok.find(sep)
        if i > 0:
            return tok[:i]
    return tok


def _humanize_reason(reason: str) -> str | None:
    """Localise the service's QC rejection summary, or None if unrecognised.

    The service's ``reason`` format is frozen, so it is parsed here rather than
    changed there. It reads:

        "enrollment rejected: only 1 of 15 image(s) passed quality control
         (need >= 3). Dropped: blur<80 x2, no-face x1. Re-capture with ..."

    Returning None (rather than a partial translation) on ANY unknown token lets
    the caller fall back to the raw string, so a reason we cannot parse is still
    shown to the user instead of being swallowed.
    """
    marker = "Dropped:"
    i = reason.find(marker)
    if i < 0:
        return None
    tail = reason[i + len(marker):].strip()
    end = tail.find(". ")          # summarize_rejections output ends here
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
            return None            # unknown token -> caller shows the raw text
        count = count.strip()
        parts.append(f"{t(key)} ×{count}" if count else t(key))
    return ", ".join(parts) or None


class EnrollWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(t("enroll.title"))
        self.root.geometry(f"{PREVIEW_W + 60}x{PREVIEW_H + 320}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.detector = FaceDetector()
        # Live QC thresholds, read once. The wizard only READS these; the gate
        # numbers themselves stay owned by Config/enroll_qc.
        self._cfg = Config.load()
        self._stop = threading.Event()
        self._capture_armed = threading.Event()
        self._cam_thread: threading.Thread | None = None
        self._cap: cv2.VideoCapture | None = None
        self._latest_tk_image: ImageTk.PhotoImage | None = None
        self._face_streak = 0
        self._last_capture_ts = 0.0
        self._captured = 0
        self._target = 15
        self._building = False
        self._coach = CoachState("enroll.status.waiting", False, "err")
        self._last_coach_key: str | None = None
        self._guide_pinned = False

        self._lease_ok = self._acquire_camera_lease()

        self._build_ui()
        self._refresh_existing_stats()

        if self._lease_ok:
            self._set_guide("enroll.guide.idle")
            # Start the preview immediately so the user sees themselves.
            self._cam_thread = threading.Thread(
                target=self._camera_loop, name="enroll-camera", daemon=True
            )
            self._cam_thread.start()
        else:
            # Lease refused (camera owned by the service / another process): do
            # NOT open a competing capture. Keep the blank preview frame and
            # tell the user; the window still closes cleanly (no cam thread).
            self._set_guide("enroll.error.service_busy")

    # ---------------- UI ----------------

    def _init_styles(self) -> None:
        """Define NAMED ttk styles for this window only.

        ``ttk.Style(self.root)`` is bound to OUR interpreter for the same reason
        every Variable above carries ``master=``: several Tk roots live in this
        process (tray Status/Settings/Help), and an unmastered Style would attach
        to whichever root came up first. Every name is derived
        ("Enroll.*"/"Coach.*") -- configuring a BARE class such as "TButton", or
        calling ``theme_use``, would restyle the settings window too.
        """
        style = ttk.Style(self.root)
        style.configure("Enroll.TButton", padding=(10, 5))
        style.configure("Enroll.Guide.TLabel", font=("", 11, "bold"))
        style.configure("Enroll.Hint.TLabel", foreground="#555555")
        style.configure("Enroll.Count.TLabel", font=("", 10, "bold"))
        # Coach line: one size up from the body text, colour carries the state.
        for name, colour in (("Coach.Ok.TLabel", "#1e7a3c"),
                             ("Coach.Warn.TLabel", "#8a5a00"),
                             ("Coach.Err.TLabel", "#b3261e")):
            style.configure(name, foreground=colour, font=("", 11, "bold"))

    def _build_ui(self) -> None:
        self._init_styles()
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)

        # Camera preview canvas
        self.preview = tk.Label(frm, background="#222",
                                width=PREVIEW_W, height=PREVIEW_H)
        self.preview.pack(pady=(0, 8))

        # Big guidance line.
        # master= on every Variable below: several Tk roots live in this
        # process (tray Status/Settings/Help each open their own), and a
        # master-less Variable binds to the FIRST live root instead of ours --
        # the empty-widget bug fixed for the tray windows in block1 (c27a79f).
        self.guide_var = tk.StringVar(master=self.root, value="")
        self.guide = ttk.Label(frm, textvariable=self.guide_var,
                               wraplength=PREVIEW_W, justify="center",
                               style="Coach.Warn.TLabel")
        self.guide.pack(fill="x", pady=(0, 6))

        # Progress row -- counts frames that PASSED the live quality gate.
        prog_row = ttk.Frame(frm)
        prog_row.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(prog_row, mode="determinate",
                                        maximum=self._target)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.progress_var = tk.StringVar(master=self.root, value="0/15")
        ttk.Label(prog_row, textvariable=self.progress_var, width=8,
                  anchor="e", style="Enroll.Count.TLabel").pack(side="right")
        attach_tooltip(self.progress, "enroll.progress.tip")

        # Count spinbox
        count_row = ttk.Frame(frm)
        count_row.pack(fill="x", pady=4)
        ttk.Label(count_row, text=t("enroll.count") + ":",
                  width=22, anchor="w").pack(side="left")
        self.count_var = tk.IntVar(master=self.root, value=self._target)
        self.count_spin = ttk.Spinbox(
            count_row, from_=5, to=40, textvariable=self.count_var,
            width=6, command=self._on_count_changed,
        )
        self.count_spin.pack(side="left")
        attach_tooltip(self.count_spin, "enroll.count")

        # Existing data label
        self.existing_var = tk.StringVar(master=self.root)
        ttk.Label(frm, textvariable=self.existing_var,
                  style="Enroll.Hint.TLabel").pack(anchor="w", pady=(2, 6))

        # Buttons row
        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=(4, 0))
        self.start_btn = ttk.Button(btns, text=t("enroll.btn.start"),
                                    style="Enroll.TButton",
                                    command=self._on_start_stop)
        self.start_btn.pack(side="left", padx=3)
        self.build_btn = ttk.Button(btns, text=t("enroll.btn.build"),
                                    style="Enroll.TButton",
                                    command=self._on_build)
        self.build_btn.pack(side="left", padx=3)
        self.wipe_btn = ttk.Button(btns, text=t("enroll.btn.wipe"),
                                   style="Enroll.TButton",
                                   command=self._on_wipe)
        self.wipe_btn.pack(side="left", padx=3)
        ttk.Button(btns, text=t("enroll.btn.close"), style="Enroll.TButton",
                   command=self._on_close).pack(side="right", padx=3)

        # Render a first black frame so the layout doesn't collapse.
        blank = Image.new("RGB", (PREVIEW_W, PREVIEW_H), (24, 24, 24))
        self._latest_tk_image = ImageTk.PhotoImage(blank)
        self.preview.configure(image=self._latest_tk_image)

    # ---------------- helpers ----------------

    def _set_guide(self, key: str, **kwargs) -> None:
        """Set a NON-coach message (build progress, run finished, errors).

        Drops the state colour back to neutral and clears the coach's
        last-key memo, so the next genuine change of advice re-posts even if it
        happens to repeat whatever was on screen before this message.
        """
        self._last_coach_key = None
        try:
            self.guide_var.set(t(key, **kwargs))
            self.guide.configure(style="Enroll.Guide.TLabel")
        except (tk.TclError, AttributeError, RuntimeError):
            pass

    def _refresh_existing_stats(self) -> None:
        n = _count_enroll_images()
        has = t("enroll.has.yes") if _has_embeddings() else t("enroll.has.no")
        self.existing_var.set(t("enroll.existing", n=n, has=has))

    def _on_count_changed(self) -> None:
        try:
            v = int(self.count_var.get())
        except (ValueError, tk.TclError):
            return
        self._target = max(1, v)
        self.progress.configure(maximum=self._target)
        self.progress_var.set(f"{self._captured}/{self._target}")

    def _update_progress(self) -> None:
        self.progress["value"] = self._captured
        self.progress_var.set(f"{self._captured}/{self._target}")

    def _acquire_camera_lease(self) -> bool:
        resp = pipe_call(
            {"cmd": "pause_camera", "seconds": CAMERA_LEASE_S},
            timeout_s=3.0,
        )
        if not (resp and resp.get("ok")):
            log.warning("could not pause face_service camera: %s", resp)
            messagebox.showwarning(
                t("enroll.title"),
                t("enroll.error.service_busy"),
                parent=self.root,
            )
            return False
        return True

    def _renew_camera_lease(self) -> None:
        """Re-arm the lease, so it never outlives this wizard by more than CAMERA_LEASE_S.

        Driven from the camera loop rather than a timer thread, and that is the design: the loop
        is already what has to keep running for the preview to be alive, so a wedged loop simply
        stops renewing -- which is exactly when we WANT the lease to lapse and the device to go
        back to the service.

        A failed renewal is logged and otherwise ignored; ``_lease_ok`` is deliberately NOT
        cleared. The expected cause is the service restarting mid-enrollment, and the next renewal
        then takes the lease again from scratch. That also repairs the restart case, which used to
        have no cure: a restarted service grabs the webcam in its warmup with no memory of our
        lease (the lease lives only in its RAM), and the next re-arm takes it back through the
        normal pause_camera handler. Worst-case contention is one renewal interval.

        Honest cost: pipe_call blocks THIS loop for up to its timeout when the service is down, so
        the preview can hitch for up to 3s once every LEASE_RENEW_S. Accepted deliberately -- the
        alternative is a second thread racing the same pipe for the same lease.
        """
        resp = pipe_call({"cmd": "pause_camera", "seconds": CAMERA_LEASE_S}, timeout_s=3.0)
        if not (resp and resp.get("ok")):
            # Once per LEASE_RENEW_S at worst, so this cannot spam the log.
            log.warning("camera lease renewal failed (service down or busy?): %s", resp)

    def _release_camera_lease(self) -> None:
        pipe_call({"cmd": "resume_camera"}, timeout_s=3.0)

    # ---------------- camera thread ----------------

    def _open_capture(self) -> bool:
        """Open the webcam with a bounded read-timeout, storing it on
        ``self._cap``. Returns True on success.

        DSHOW is tried FIRST: it is the backend this hardware has always used.
        MSMF was promoted to first in block5-A because it is the only backend
        that honors ``CAP_PROP_*_TIMEOUT_MSEC``, but on the target webcam the
        timeout is NOT applied -- read() still blocks forever (measured: 2
        hangs on MSMF vs 2 on DSHOW, i.e. no improvement). The timeout props
        below are kept as harmless cross-hardware insurance; the wedged-read
        leak itself is contained by running this wizard in its own process
        (see ``main()``), so a stuck thread dies with it.

        The device comes from ``cfg.camera_index``, the same knob the service
        opens with -- a hardcoded 0 here would fight the service for a
        different camera on a multi-cam machine.
        """
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
            cap = cv2.VideoCapture(self._cfg.camera_index, backend)
            if not cap.isOpened():
                cap.release()   # never keep a candidate we didn't accept
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Open/read timeout hint. getattr(): these props only exist on newer
            # OpenCV; set() may be rejected by a backend -- both are non-fatal.
            # DSHOW ignores them outright; kept for hardware where MSMF wins.
            for prop_name in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
                prop = getattr(cv2, prop_name, None)
                if prop is not None and not cap.set(prop, CAMERA_READ_TIMEOUT_MS):
                    log.debug("%s not accepted by backend %s", prop_name, backend)
            self._cap = cap
            return True
        return False

    def _release_capture(self) -> None:
        """Release the capture on every exit path (idempotent)."""
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                log.exception("enroll camera release failed")

    def _camera_loop(self) -> None:
        if not self._open_capture():
            self.root.after(0, lambda: messagebox.showerror(
                t("enroll.title"), t("enroll.error.camera"),
                parent=self.root,
            ))
            return

        cap = self._cap
        try:
            self._warm_quality()
            frame_idx = 0
            faces: list = []
            # Monotonic, not wall-clock: a clock adjustment mid-enrollment must not skip or stall
            # the renewal. _lease_ok is checked even though the thread only starts when the lease
            # was taken -- an unpaired pause_camera would hand us a device we never asked for.
            next_renew = time.monotonic() + LEASE_RENEW_S
            while not self._stop.is_set():
                if self._lease_ok and time.monotonic() >= next_renew:
                    self._renew_camera_lease()
                    # Re-read the clock: the call above can burn up to its timeout, and scheduling
                    # from BEFORE it would make a slow/failing renewal fire again immediately.
                    next_renew = time.monotonic() + LEASE_RENEW_S
                ok, frame = cap.read()
                if not ok or frame is None:
                    # Timed-out / dropped read: NOT fatal. Sleep a beat (in case
                    # a backend ignored the timeout and returns instantly) and
                    # loop -- the point is to re-check _stop, not to die.
                    time.sleep(0.05)
                    continue
                frame_idx += 1

                # Detect every few frames to save CPU but still feel live. The
                # coach verdict is recomputed with the detection, since it needs
                # that frame's landmarks; in between, the last verdict stands.
                # ``measured`` says whether the verdict describes THIS frame --
                # only such a frame may be written to disk.
                measured = frame_idx % DETECT_EVERY_N_FRAMES == 0
                if measured:
                    faces = self._detect_faces(frame)
                    self._coach = self._evaluate_coach(frame, faces)

                self._process_capture(frame, faces, self._coach, measured)

                annotated = self._annotate(frame, faces, self._coach)
                self._post_preview(annotated)
                time.sleep(0.03)  # ~30 fps cap
        finally:
            self._release_capture()

    def _warm_quality(self) -> None:
        """Pay the one-off insightface import before the preview starts.

        ``enroll_qc.aligned_crop`` imports ``insightface.utils.face_align`` on
        first use (~0.5 s). Left lazy that would land on the camera thread a
        frame or two into the preview and read as a freeze while we hold the
        webcam -- exactly the symptom of the wedged-read debt. Failure here is
        harmless: aligned_crop falls back to a resized bbox crop on its own.
        """
        try:
            from insightface.utils import face_align  # noqa: F401
        except Exception as e:
            log.debug("face_align warmup skipped: %s", e)

    def _detect_faces(self, bgr) -> list:
        """Return the FULL YuNet rows for this frame.

        YuNet yields (N, 15) float32: cols 0-3 the bbox (x, y, w, h), cols 4-13
        the five landmarks as x,y pairs, col 14 the confidence. The wizard used
        to keep only the bbox; the landmarks are what enroll_qc needs to build
        the same 112x112 aligned crop the build step measures, so the whole row
        is carried up now.
        """
        try:
            h, w = bgr.shape[:2]
            det = self.detector._ensure(w, h)  # type: ignore[attr-defined]
            _, res = det.detect(bgr)
            if res is None:
                return []
            return list(res)
        except Exception as e:
            log.debug("detect failed: %s", e)
            return []

    @staticmethod
    def _largest_row(rows):
        """The biggest box in the frame -- the one being enrolled."""
        return max(rows, key=lambda r: float(r[2]) * float(r[3]))

    def _evaluate_coach(self, frame, rows) -> CoachState:
        """Grade this frame and pick the single most useful thing to say.

        Ladder, most blocking first: no face -> framing -> quality -> ready.
        Everything here works in RAW frame coordinates (pre-mirror); only the
        drawing in _annotate crosses into preview space.
        """
        if not rows:
            return CoachState("enroll.status.waiting", False, "err")

        row = self._largest_row(rows)
        fh, fw = frame.shape[:2]
        x, y, w, h = (float(v) for v in row[0:4])

        area_frac = (w * h) / float(fw * fh)
        if area_frac < COACH_AREA_MIN_FRAC:
            return CoachState("enroll.coach.closer", False, "warn")
        if area_frac > COACH_AREA_MAX_FRAC:
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

        # Same gate function the build step uses, minus the det token: build-time
        # det comes from InsightFace/SCRFD while this score is YuNet's, and the
        # two are not on a comparable scale. Exposure and focus ARE comparable --
        # both are measured on the identical 112x112 aligned crop.
        heads = {_token_head(r) for r in qc_reasons(q, self._cfg)} - {"det"}
        for tok in ("dark", "bright", "blur"):
            if tok in heads:
                return CoachState(_COACH_KEY_BY_TOKEN[tok], False, "warn")

        return CoachState("enroll.status.ready", True, "ok")

    def _annotate(self, bgr, faces, coach: CoachState | None = None):
        import numpy as np
        level = coach.level if coach is not None else "warn"
        colour = _COACH_BGR[level]
        img = bgr.copy()
        # Face boxes are drawn in RAW coordinates on purpose: they ride through
        # the mirror below and land on the face.
        for row in faces:
            x, y, w, h = (int(v) for v in row[0:4])
            cv2.rectangle(img, (x, y), (x + w, y + h), colour, 3)
        # Flip horizontally so the preview feels like a mirror.
        img = cv2.flip(img, 1)
        # Resize to the preview dims, keep aspect.
        h0, w0 = img.shape[:2]
        scale = min(PREVIEW_W / w0, PREVIEW_H / h0)
        new_w, new_h = int(w0 * scale), int(h0 * scale)
        img = cv2.resize(img, (new_w, new_h))
        # Center-pad to (PREVIEW_W, PREVIEW_H)
        canvas = np.zeros((PREVIEW_H, PREVIEW_W, 3), dtype=img.dtype)
        ox = (PREVIEW_W - new_w) // 2
        oy = (PREVIEW_H - new_h) // 2
        canvas[oy:oy + new_h, ox:ox + new_w] = img
        # The framing guide is a fixed on-screen target, so it is drawn HERE --
        # after the mirror/scale/pad -- in PREVIEW coordinates. Drawing it with
        # the boxes above would push it through the flip a second time.
        cv2.ellipse(canvas, (PREVIEW_W // 2, PREVIEW_H // 2),
                    (_GUIDE_AXIS_X, _GUIDE_AXIS_Y), 0, 0, 360, colour, 2)
        return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)

    def _post_preview(self, rgb) -> None:
        # The camera thread prepares ONLY a PIL image and touches NO Tk object.
        # ImageTk.PhotoImage binds to a Tcl interpreter, so constructing it here
        # meant a foreign thread reaching into this window's interpreter -- the
        # same cross-thread hazard that aborted the tray with Tcl_AsyncDelete
        # (block1 fix2, 7932eb3). It is now built inside apply(), on the
        # window's own thread.
        try:
            pil = Image.fromarray(rgb)
        except Exception:
            return

        def apply():
            # Runs on the window's thread, so building the Tk image is safe.
            if self._stop.is_set():
                return   # closing: don't hand new Tk state to a dying window
            try:
                img = ImageTk.PhotoImage(pil)
                # Hold the reference; ImageTk requires it.
                self._latest_tk_image = img
                self.preview.configure(image=img)
            except (tk.TclError, AttributeError, RuntimeError):
                # Window torn down mid-flight (widgets already nulled/destroyed).
                pass

        try:
            self.root.after(0, apply)
        except RuntimeError:
            pass

    def _process_capture(self, frame, rows, coach: CoachState,
                         measured: bool = False) -> None:
        if self._building:
            return

        if rows:
            self._face_streak += 1
        else:
            self._face_streak = 0

        # The coach owns the guidance line in both modes -- it already states the
        # most blocking thing about this frame. The exception is a TERMINAL
        # message ("all N captured", a build result): those pin the line, or the
        # coach would overwrite them on the very next frame, ~66 ms later.
        if not self._guide_pinned:
            self._queue_coach(coach)

        if not self._capture_armed.is_set():
            return

        now = time.time()
        if self._captured >= self._target:
            self._capture_armed.clear()
            self._guide_pinned = True
            self._queue_guide("enroll.guide.done_capture", n=self._target)
            self._queue_refresh_buttons()
            return
        # The gate: framing + live QC (coach.ok) on top of the existing stability
        # and cooldown rules. Only frames that would survive the build step's
        # quality control reach the disk, so the bar counts ACCEPTED shots.
        if not coach.ok or self._face_streak < FACE_STABLE_FRAMES:
            return
        if not measured:
            # The verdict was computed on the PREVIOUS frame; THIS one never went
            # through frame_quality. Writing it would put an unmeasured JPG on
            # disk and count it as accepted -- exactly the dishonesty the gate
            # exists to remove. Wait one frame; the cooldown is ~30 frames long,
            # so nothing is lost.
            return
        if now - self._last_capture_ts < CAPTURE_COOLDOWN_S:
            # Between shots. The line still reads "ready" (set above) -- it must
            # not flicker to a warning just because the cooldown is running.
            return

        try:
            ENROLL_DIR.mkdir(parents=True, exist_ok=True)
            path = ENROLL_DIR / f"enroll_{int(now * 1000)}.jpg"
            cv2.imwrite(str(path), frame)
            self._captured += 1
            self._last_capture_ts = now
            log.info("enroll: saved %s (%d/%d)", path.name, self._captured, self._target)
        except Exception:
            log.exception("failed to save enroll frame")
            return

        self._queue_update_progress()

    def _queue_guide(self, key: str, **kwargs) -> None:
        try:
            self.root.after(0, lambda: self._set_guide(key, **kwargs))
        except RuntimeError:
            pass

    def _queue_coach(self, coach: CoachState) -> None:
        # Only when the advice actually changes: the loop runs ~30x/s and
        # re-posting an identical line would just flood the after() queue.
        if coach.key == self._last_coach_key:
            return
        self._last_coach_key = coach.key
        try:
            self.root.after(0, lambda: self._apply_coach(coach))
        except RuntimeError:
            pass

    def _apply_coach(self, coach: CoachState) -> None:
        # Runs on the window's thread. Same swallow-list as _post_preview: the
        # window may have been torn down (refs nulled) while this was queued.
        try:
            self.guide_var.set(t(coach.key))
            self.guide.configure(style=_COACH_STYLE[coach.level])
        except (tk.TclError, AttributeError, RuntimeError):
            pass

    def _queue_update_progress(self) -> None:
        try:
            self.root.after(0, self._update_progress)
        except RuntimeError:
            pass

    def _queue_refresh_buttons(self) -> None:
        try:
            self.root.after(0, lambda: self.start_btn.configure(text=t("enroll.btn.start")))
        except RuntimeError:
            pass

    # ---------------- button actions ----------------

    def _on_start_stop(self) -> None:
        if self._capture_armed.is_set():
            self._capture_armed.clear()
            self.start_btn.configure(text=t("enroll.btn.start"))
            self._set_guide("enroll.guide.idle")
            return

        # (re)start a session: reset counters
        self._captured = 0
        self._last_capture_ts = 0.0
        self._guide_pinned = False
        try:
            self._target = max(1, int(self.count_var.get()))
        except Exception:
            self._target = 15
        self.progress.configure(maximum=self._target)
        self._update_progress()
        self._capture_armed.set()
        self.start_btn.configure(text=t("enroll.btn.stop"))
        self._set_guide("enroll.guide.capturing", i=0, n=self._target)

    def _on_build(self) -> None:
        if self._building:
            return
        if _count_enroll_images() == 0:
            messagebox.showwarning(
                t("enroll.title"),
                t("enroll.guide.build_empty"),
                parent=self.root,
            )
            return

        self._building = True
        self._capture_armed.clear()
        self.start_btn.configure(state="disabled")
        self.build_btn.configure(state="disabled")
        self.wipe_btn.configure(state="disabled")
        self._set_guide("enroll.guide.building")

        def worker():
            # Call the service — it already has the ONNX engine loaded and warm.
            resp = pipe_call({"cmd": "build_enrollment"}, timeout_s=120.0)
            ok = bool(resp and resp.get("ok"))
            n = int(resp.get("count", 0)) if ok else 0
            try:
                # Toast fires even if this window was closed mid-build.
                from .tray import notify_event  # lazy: avoids an import cycle
                if ok and n > 0:
                    notify_event("notify_enroll", t("notify.enroll_ok", n=n))
                else:
                    # Report what actually failed. Defaulting to "no-face" used
                    # to blame the wrong cause for every blur/exposure drop.
                    raw = (resp or {}).get("reason") or ""
                    reason = _humanize_reason(raw) or raw or t("enroll.reason.unknown")
                    notify_event("notify_enroll", t("notify.enroll_fail", reason=reason))
            except Exception:
                log.exception("enroll notify failed")
            def done():
                # Pin BEFORE clearing _building: the moment _building goes False
                # the camera thread may queue a coach update, and that callback
                # would land after this one and wipe the outcome off the line.
                self._guide_pinned = True
                self._building = False
                self.start_btn.configure(state="normal")
                self.build_btn.configure(state="normal")
                self.wipe_btn.configure(state="normal")
                self._refresh_existing_stats()
                # Every branch below is a terminal message; the pin above keeps
                # the coach from overwriting it on the next frame.
                if resp and resp.get("ok"):
                    n = int(resp.get("count", 0))
                    if n > 0:
                        self._set_guide("enroll.guide.done", n=n)
                    else:
                        self._set_guide("enroll.guide.build_empty")
                else:
                    # Show the REAL reason the service returned. build_empty is
                    # no longer hardcoded here: it claims "no face in any shot",
                    # which is simply false when the drops were blur or exposure.
                    raw = (resp or {}).get("reason") or ""
                    why = _humanize_reason(raw)
                    if why:
                        self._set_guide("enroll.guide.build_rejected", why=why)
                        body = t("enroll.build.failed", why=why)
                    else:
                        # Unparsed reason: surface it verbatim rather than
                        # swallowing it behind a guessed message.
                        self._set_guide("enroll.guide.build_failed")
                        body = f"build_enrollment: {raw or '?'}"
                    messagebox.showerror(
                        t("enroll.title"), body, parent=self.root,
                    )
            try:
                self.root.after(0, done)
            except RuntimeError:
                pass

        threading.Thread(target=worker, name="enroll-build", daemon=True).start()

    def _on_wipe(self) -> None:
        if not messagebox.askyesno(
            t("enroll.confirm.wipe.title"),
            t("enroll.confirm.wipe.body"),
            parent=self.root,
        ):
            return

        try:
            if ENROLL_DIR.exists():
                for p in ENROLL_DIR.iterdir():
                    if p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                        try:
                            p.unlink()
                        except Exception:
                            log.exception("delete %s", p)
            if EMBED_PATH.exists():
                EMBED_PATH.unlink()
        except Exception:
            log.exception("wipe failed")

        self._captured = 0
        self._guide_pinned = False
        self._update_progress()
        self._refresh_existing_stats()
        self._set_guide("enroll.guide.idle")

    def _on_close(self) -> None:
        # Signal the camera thread first and WAIT for it to release the
        # webcam before we tear down Tk. Skipping the join would let the
        # daemon thread get cut mid-read(), which on Windows leaks the camera
        # handle and black-frames the service until a full restart (hit in
        # production 2026-04-21). read() is still not reliably interruptible
        # on this hardware, so the join below can and does time out -- but
        # that is no longer terminal: this window owns its process, so
        # returning from main() ends it and the OS releases the camera handle
        # unconditionally, wedged thread or not. That is what actually closes
        # KNOWN_ISSUES #1; the polite path below just makes the common case
        # clean instead of relying on process teardown every time.
        self._stop.set()
        self._capture_armed.clear()
        t_cam = self._cam_thread
        if t_cam is not None and t_cam.is_alive():
            # Budget for the loop to notice _stop between frames and run its
            # finally (cap.release()). On a healthy device a read returns in
            # ~30 ms, so 3 s is generous; on a wedged one it will time out.
            t_cam.join(timeout=3.0)
            if t_cam.is_alive():
                # Do NOT cross-thread release here: that escalation waits on a
                # confirmed process topology (block5 follow-up). Leave the log.
                log.warning("enroll camera thread did not exit within 3s")
        # Separate try blocks: a failure while dropping references must never
        # cost us the destroy() that actually tears the interpreter down.
        try:
            self._teardown_tk_objects()
        except Exception:
            log.exception("enroll tk teardown failed")
        try:
            self.root.destroy()
        except Exception:
            pass
        # Only resume if we actually took the lease -- an unpaired resume would
        # clear a lease we never set.
        if self._lease_ok:
            self._release_camera_lease()

    def _teardown_tk_objects(self) -> None:
        """Drop every Tk reference we hold, BEFORE root.destroy().

        Same rationale as the tray windows (block1 fix2, 7932eb3): with several
        Tk roots alive in this process, a Variable or PhotoImage finalized later
        by ANOTHER thread's GC talks to a dead interpreter -- that raises
        "main thread is not in main loop" and can abort the process with
        Tcl_AsyncDelete. Releasing them here, on this window's own thread while
        its interpreter is still alive, keeps finalization deterministic.

        Deliberately independent of the camera thread: this only touches Tk
        state, so it still runs when that thread is wedged in read() (the
        leaked-handle debt deferred to Stage 7).
        """
        self.guide_var = None
        self.progress_var = None
        self.count_var = None
        self.existing_var = None
        self._latest_tk_image = None
        for name in ("preview", "guide", "progress", "count_spin",
                     "start_btn", "build_btn", "wipe_btn"):
            setattr(self, name, None)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    """Process entry point: ``python -m presence_monitor.enroll_gui``.

    The wizard used to run on a daemon thread inside the tray process. It does not any more, and
    the reason is the whole point of this module's isolation: ``cv2.VideoCapture.read()`` is not
    reliably interruptible on this hardware, so a wedged camera thread never ran its
    ``finally: cap.release()`` and kept the physical device inside the TRAY's address space --
    every later verify then read black frames, and with ``persistent_camera=True`` that black
    capture got cached (KNOWN_ISSUES #1). Releasing it from another thread was not an option
    either: ``VideoCapture`` is not thread-safe, and a native crash would take presence
    monitoring -- i.e. walk-away locking -- down with it.

    Owning a process solves it by construction. Closing the window returns from here, the process
    exits, and the OS reclaims the camera handle no matter what state the native call is stuck in.
    It also removes the crash hole the old in-thread launcher had: a window that died before
    setting ``_stop`` left its camera thread looping inside the tray forever.

    Tk lives on the MAIN thread here (the camera stays a worker), which is what Tk wants anyway.

    Logging goes to its own ``enroll.log`` beside ``presence.log`` -- same level, format and
    handlers as the presence process, but a separate file, because two processes appending to one
    log file interleave badly on Windows.
    """
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    setup_logging(LOG_PATH.with_name("enroll.log"))
    try:
        # i18n state is per-process: the tray's set_language() never ran here, and the module
        # default is English, so without this the wizard would ignore the user's saved language.
        # EnrollWindow loads its own Config for the QC/camera knobs; this second read is the
        # cheap price of not reshaping its constructor.
        cfg = Config.load()
        set_language(cfg.language)
        # One startup line per run. enroll.log is APPENDED to (and rotated at 5 MB), not truncated,
        # so this is what separates one wizard run from the last and tells you which process and
        # which settings produced everything below it.
        log.info("enroll wizard starting: pid=%s lang=%s camera_index=%s",
                 os.getpid(), cfg.language, cfg.camera_index)
        EnrollWindow().run()
    except Exception:
        log.exception("enroll wizard crashed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
