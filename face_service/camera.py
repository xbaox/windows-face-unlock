from __future__ import annotations
import logging
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


class Camera:
    """Thin wrapper over cv2.VideoCapture with open/close safety."""

    def __init__(self, index: int = 0, warmup_frames: int = 10):
        self.index = index
        self.warmup_frames = warmup_frames
        self._cap: cv2.VideoCapture | None = None

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
            for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
                cap = cv2.VideoCapture(self.index, backend)
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
        ``open()`` stays the authoritative, robust opener for enrollment / warmup.

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
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
            if deadline is not None and time.monotonic() >= deadline:
                return False       # no candidate open yet -- nothing to release
            cap = cv2.VideoCapture(self.index, backend)
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
        assert self._cap is not None, "Camera not opened"
        ok, frame = self._cap.read()
        return frame if ok else None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
