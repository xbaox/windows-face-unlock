"""The webcam (cv2.VideoCapture) with bounded open AND bounded read (Stage 9, act 9b R10).

Stage 9 changes:
  * by NAME: ``Camera(name=...)`` resolves the configured device name to its DirectShow index at
    every open (face_service.camera_devices) and opens that index on CAP_DSHOW only -- the index a
    name maps to is only meaningful for the backend it was enumerated for (F-141). A name that is
    not present fails the open with ``not_found`` set; there is no fallback to index 0. Without a
    name the legacy index path (DSHOW -> MSMF -> ANY) stays, for configs from before the name.
  * bounded READ: every read() runs on a worker and is waited on for at most ``read_cap_s`` (the
    same ceiling as an open attempt, camera_open_attempt_cap_s). A read that does not return is
    abandoned: the capture is dropped (released by the worker once the native call returns) and
    CameraReadTimeout is raised -- the request answers camera-error instead of wedging the
    sequential pipe server (F-144).
  * the negotiated format (width x height, FPS, FOURCC, backend, device) is logged once per open
    (F-151).
"""
from __future__ import annotations
import logging
import threading
import time
import cv2
import numpy as np

log = logging.getLogger(__name__)

# Open/read timeout hint, mirroring the enrollment wizard's CAMERA_READ_TIMEOUT_MS
# (presence_monitor/enroll_gui.py, block5-A0). Only MSMF honors these props, and
# NOT on the target hardware -- kept as cross-hardware insurance so a wedged
# driver read cannot block the pipe server forever on machines where it IS
# honored. Deliberately NOT one of the service's camera_* config knobs.
CAMERA_READ_TIMEOUT_MS = 1000


def _apply_timeout_props(cap, backend) -> None:
    """Best-effort open/read timeout hints. NEVER raises.

    These props only exist on newer OpenCV builds and a backend may reject the
    ``set()`` outright, so both the lookup and the call are guarded: a missing
    constant or a failed/raising set must never turn into a failed open.
    """
    for prop_name in ("CAP_PROP_OPEN_TIMEOUT_MSEC", "CAP_PROP_READ_TIMEOUT_MSEC"):
        prop = getattr(cv2, prop_name, None)
        if prop is None:
            continue          # OpenCV too old: nothing to set, not an error
        try:
            if not cap.set(prop, CAMERA_READ_TIMEOUT_MS):
                log.debug("%s not accepted by backend %s", prop_name, backend)
        except Exception:
            log.debug("%s set failed on backend %s", prop_name, backend, exc_info=True)


class CameraReadTimeout(RuntimeError):
    """A read did not return within the ceiling; the capture was abandoned."""


def _fourcc_str(v: float) -> str:
    try:
        n = int(v)
        return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00") or "?"
    except Exception:
        return "?"


class Camera:
    """Thin wrapper over cv2.VideoCapture with open/close safety."""

    def __init__(self, index: int = 0, warmup_frames: int = 10, name: str = "",
                 read_cap_s: float = 5.0):
        self.index = index
        self.name = (name or "").strip()
        self.warmup_frames = warmup_frames
        self.read_cap_s = float(read_cap_s)
        self.not_found = False            # the named device was not present at the last open
        self._cap: cv2.VideoCapture | None = None

    def _log_format(self, cap, backend) -> None:
        try:
            log.info("camera open: device=%s index=%d backend=%s %dx%d fps=%.1f fourcc=%s",
                     repr(self.name) if self.name else "(by index)", self.index,
                     {cv2.CAP_DSHOW: "DSHOW", cv2.CAP_MSMF: "MSMF"}.get(backend, str(backend)),
                     int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                     float(cap.get(cv2.CAP_PROP_FPS)), _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)))
        except Exception:
            log.debug("camera format query failed", exc_info=True)

    def _backends(self):
        """(index, backends) for this open. By name: the resolved DSHOW index on DSHOW only."""
        if not self.name:
            self.not_found = False
            return self.index, (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY)
        from .camera_devices import resolve_index
        idx = resolve_index(self.name)
        self.not_found = idx is None
        if idx is None:
            log.warning("camera %r is not present -- not opening another camera instead", self.name)
            return None, ()
        self.index = idx
        return idx, (cv2.CAP_DSHOW,)

    def open(self) -> None:
        if self._cap is not None:
            return
        last_err: str | None = None
        # Three attempts per backend, because Windows webcam drivers often
        # report ``isOpened=True`` from a handle the previous process (or
        # a killed enroll wizard) didn't release cleanly. Releasing the
        # zombie capture + waiting a beat is enough for DirectShow to
        # hand the real device back.
        for attempt in range(3):
            index, backends = self._backends()
            if index is None:
                break
            for backend in backends:
                cap = cv2.VideoCapture(index, backend)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok = False
                for _ in range(5):
                    ret, _ = cap.read()
                    if ret:
                        ok = True
                        break
                    time.sleep(0.1)
                if ok:
                    self._cap = cap
                    self._log_format(cap, backend)
                    for _ in range(self.warmup_frames):
                        cap.read()
                        time.sleep(0.03)
                    return
                last_err = (f"attempt={attempt} backend={backend} "
                            f"isOpened={cap.isOpened()}")
                cap.release()
            if attempt < 2:
                time.sleep(1.0)  # let the driver flush stuck handles
        raise RuntimeError(f"Cannot open camera index {self.index} ({last_err})")

    def open_fast(self, deadline: float | None = None) -> bool:
        """Single-pass open for the busy-check path (Stage 3 / Step 4; deadline added in 7b-2).

        One quick pass over the backends with a few reads and NO long sleeps; returns True if a
        frame was grabbed (camera acquired), False if it could not be (e.g. the device is held by
        another process). Unlike ``open()`` it never raises and does NOT run the multi-second
        zombie-recovery retry -- the service wraps it in its own config-driven retry loop.
        (Stage 9, D-90: ``open()`` -- the slow 3x3 zombie recovery -- is used by the dev tools only;
        the service and the wizard open through here.)

        ``deadline`` (monotonic) makes this COOPERATIVELY bounded: it is checked between backends
        and before each read, and once passed we release the candidate and give up. That is
        best-effort by construction -- the checks sit between native calls, so a single
        ``VideoCapture()`` or ``read()`` already inside a wedged driver still runs to completion
        (no property on this hardware interrupts it). The hard ceiling is the caller's:
        ``camera_open.BoundedOpener`` waits on a whole attempt for at most ``cap_s`` and reclaims
        the capture if it lands late. ``None`` keeps the pre-7b-2 behaviour exactly, so the probes
        and benchmarks that call ``open_fast()`` with no argument are unaffected.
        """
        if self._cap is not None:
            return True
        index, backends = self._backends()
        for backend in backends:
            if deadline is not None and time.monotonic() >= deadline:
                return False       # no candidate open yet -- nothing to release
            cap = cv2.VideoCapture(index, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            _apply_timeout_props(cap, backend)
            ok = False
            for _ in range(4):
                if deadline is not None and time.monotonic() >= deadline:
                    cap.release()  # candidate we will not finish evaluating
                    return False
                ret, _ = cap.read()
                if ret:
                    ok = True
                    break
            if ok:
                self._cap = cap
                self._log_format(cap, backend)
                for _ in range(self.warmup_frames):
                    if deadline is not None and time.monotonic() >= deadline:
                        # A real capture, but under-warmed and out of budget. Hand back nothing
                        # rather than a half-configured handle; close() nulls _cap for us, so the
                        # next open starts clean.
                        self.close()
                        return False
                    cap.read()
                return True
            cap.release()
        return False

    def close(self) -> None:
        """Release the capture (idempotent, never raises).

        Swap-then-release, matching the wizard's hardened ``_release_capture``
        (block5-A0/5-B): the attribute is nulled BEFORE ``release()`` runs, so
        even a raising release leaves ``self._cap is None`` and the next
        ``open``/``open_fast`` reopens from scratch instead of short-circuiting
        on a dead handle.
        """
        cap, self._cap = self._cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                log.exception("camera release failed")

    def read(self) -> np.ndarray | None:
        """One frame, or None when the driver returned none. Bounded by ``read_cap_s``: a read
        that does not come back in time abandons the capture and raises CameraReadTimeout."""
        cap = self._cap
        assert cap is not None, "Camera not opened"
        box: dict = {}
        done = threading.Event()

        def run():
            try:
                box["r"] = cap.read()
            except Exception as e:           # a raising driver is a failed read, not a crash
                box["e"] = e
            finally:
                done.set()

        threading.Thread(target=run, name="camera-read", daemon=True).start()
        if not done.wait(self.read_cap_s):
            self._cap = None                 # never hand this capture out again

            def reap():
                done.wait()                  # the native call returns some day: release then
                try:
                    cap.release()
                except Exception:
                    pass
            threading.Thread(target=reap, name="camera-read-reaper", daemon=True).start()
            log.error("camera read did not return within %.1fs -- capture abandoned", self.read_cap_s)
            raise CameraReadTimeout(f"camera read exceeded {self.read_cap_s:.1f}s")
        if "e" in box:
            log.warning("camera read raised: %r", box["e"])
            return None
        ok, frame = box.get("r", (False, None))
        return frame if ok else None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
