"""Gated exposure boost for low-light unlock (Stage 3 / Step 3.3).

When an unlock burst comes back too dark (scene_luma below the floor), the service may try to
raise the webcam EXPOSURE and re-capture, to pull a genuine user out of the dark BEFORE falling
back to the honest too-dark refusal (Step 3.2). This is deliberately GATED -- only below the
floor -- because an unconditional boost blows out a normally-lit face (measured in Step 3.1: the
same boost in normal light drove scene 98->230 and lost the face 100%->0%).

Facts baked in from the 3.1 roundtrip on this webcam:
  * EXPOSURE is the only honored lever. The driver silently ignores GAIN and AUTO_EXPOSURE sets
    (set->get returned the old value), so we touch ONLY CAP_PROP_EXPOSURE -- nothing else.
  * Exposure units are driver-defined; here less-negative == brighter, so a POSITIVE step
    brightens (set -6 -> -4 was honored; scene 10 -> 50, distance 0.363 -> 0.249, match restored).
  * The boost MUST be transient. The service is long-lived and the same camera feeds the next
    unlock and the presence loop, so the original exposure is restored on EVERY path (honored or
    not, match or not, exception or not) via a finally in try_exposure_boost.

Stage 9 (act 9b R10, F-129): on other UVC drivers setting EXPOSURE also switches the control to
MANUAL, and writing the old NUMBER back left auto-exposure off for the rest of the process -- blown
out frames in normal light, strikes, false absences. Now CAP_PROP_AUTO_EXPOSURE is saved and
restored together with EXPOSURE, and both are READ BACK. When the device does not come back to
where it was, ``restored`` is False: the service then drops its capture (the next open gets the
driver's defaults) and switches the boost off for that device until the service restarts.

9e (F2-04): phase 2 of the same attempt INHERITS the exposure phase 1's boost set
(``ExposureHold``) -- no boost of its own -- and gets the original back after the round.

``plan_exposure`` / ``honored`` are pure and camera-free (unit-tested). ``try_exposure_boost``
takes a cv2.VideoCapture-like handle plus a ``recapture`` callable, so a fake camera can drive it
in tests. Nothing here imports the recognizer or edits camera.py.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

HONORED_TOL = 0.5             # |readback - requested| <= this counts as the driver honoring the set
AUTO_MODE_TOL = 0.1           # Stage 9: CAP_PROP_AUTO_EXPOSURE must read back as it was
# (Stage 9, D-91: DEFAULT_EXPOSURE_STEP had no reader -- the step is cfg.low_light_exposure_step.)


def plan_exposure(current: float, step: float) -> float:
    """Target exposure to brighten from ``current`` by ``step`` (less-negative == brighter here)."""
    return float(current) + float(step)


def honored(requested: float, readback: float, tol: float = HONORED_TOL) -> bool:
    """Did the driver accept our exposure set? True when the read-back lands ~at the request.

    On this webcam EXPOSURE round-trips exactly; a driver that ignores the set leaves the old value
    (readback far from the request) -> honored False -> the caller skips the wasted re-capture.
    """
    return abs(float(readback) - float(requested)) <= float(tol)


class BoostOutcome(NamedTuple):
    applied: bool                 # did we re-capture under a honored, boosted exposure?
    honored: bool                 # did the driver honor the exposure set (set->get roundtrip)?
    exposure_before: float        # exposure read before boosting (and restored to)
    exposure_target: float        # what we asked for (current + step)
    exposure_readback: float      # what the driver actually reported after the set
    recapture: object | None      # the re-capture result from recapture(), or None
    error: str | None = None      # set if re-capture raised (boost abandoned; exposure restored)
    restored: bool = True         # EXPOSURE and AUTO_EXPOSURE read back as before (Stage 9)

    def audit(self) -> dict:
        """Additive audit fields describing the boost attempt (never contains the password)."""
        d = {
            "boost_applied": self.applied,
            "boost_honored": self.honored,
            "exposure_before": round(self.exposure_before, 3),
            "exposure_after": round(self.exposure_readback, 3),
        }
        if self.error:
            d["boost_error"] = self.error
        if not self.restored:
            d["boost_restore_failed"] = True
        return d


class ExposureHold:
    """9e (F2-04, the A-7 decision): phase 2 of an attempt runs at the exposure phase 1's boost set.

    INHERITED, not a boost of its own: no light reading decides anything here -- the caller passes
    the exposure the phase-1 boost of the SAME attempt ran at (the driver's read-back), or makes
    no hold at all. Measured 2026-09-27 in the VM: phase 1 lifted -6 -> -4 (sceneL 42 -> 108), the
    restored -6 made phase 2 of that attempt dark again (44) -> too-dark.

    Restored like try_exposure_boost: EXPOSURE then AUTO_EXPOSURE written back and READ back after
    the round (``finish``), on every path but an abandoned capture (``abandon``: a read hung, the
    capture is gone and nothing is written to it). ``restored`` is True / False / None (abandoned).
    """

    def __init__(self, cap, target: float):
        import cv2
        self._cap = cap
        self._prop, self._aprop = cv2.CAP_PROP_EXPOSURE, cv2.CAP_PROP_AUTO_EXPOSURE
        self.target = float(target)
        self.before = float(cap.get(self._prop))
        self.auto_before = float(cap.get(self._aprop))
        self.restored: "bool | None" = True
        self._abandoned = False
        self._done = False
        cap.set(self._prop, self.target)
        self.readback = float(cap.get(self._prop))
        self.honored = honored(self.target, self.readback)

    def abandon(self) -> None:
        self._abandoned = True

    def finish(self) -> "bool | None":
        """Write the original exposure back (once) and report whether it read back as before."""
        if self._done:
            return self.restored
        self._done = True
        if self._abandoned:
            self.restored = None
            return None
        self._cap.set(self._prop, self.before)
        self._cap.set(self._aprop, self.auto_before)
        self.restored = bool(abs(float(self._cap.get(self._aprop)) - self.auto_before) < AUTO_MODE_TOL
                             and honored(self.before, float(self._cap.get(self._prop))))
        return self.restored

    def audit(self) -> dict:
        """Numbers only, for gesture_telemetry."""
        d = {"exposure_before": round(self.before, 3), "exposure_target": round(self.target, 3),
             "exposure_after": round(self.readback, 3), "honored": self.honored}
        if self._done:
            d["restored"] = self.restored
        return d


def try_exposure_boost(cap, step: float, recapture: Callable[[], object]) -> BoostOutcome:
    """Raise EXPOSURE, re-capture via ``recapture()``, and ALWAYS restore the original exposure.

    ``cap`` is a cv2.VideoCapture-like object (get/set on CAP_PROP_EXPOSURE). ``recapture`` runs one
    fresh analysis burst on the same (now-boosted) camera and returns whatever the caller needs
    (e.g. a VerifyOutcome). Behaviour:
      * driver ignores the set (roundtrip fails)  -> honored=False, NO re-capture (nothing changed);
      * driver honors it                          -> re-capture, applied=True;
      * re-capture raises                          -> swallowed (unlock must never crash), error set;
      * 9d (V-38): re-capture raises CameraReadTimeout -> RE-RAISED. The capture was abandoned by
        the read that hung (camera.Camera.read), so nothing is written to it any more -- a set on
        a wedged driver could hang the request the same way.
    In every other case the ORIGINAL exposure is restored in the finally, so the camera is never
    left boosted for the next unlock or the presence loop.
    """
    import cv2
    from .camera import CameraReadTimeout
    prop = cv2.CAP_PROP_EXPOSURE
    aprop = cv2.CAP_PROP_AUTO_EXPOSURE
    before = float(cap.get(prop))
    auto_before = float(cap.get(aprop))
    target = plan_exposure(before, step)
    out = None
    abandoned = False
    try:
        cap.set(prop, target)
        readback = float(cap.get(prop))
        if not honored(target, readback):
            out = BoostOutcome(False, False, before, target, readback, None)
        else:
            try:
                rc = recapture()
                out = BoostOutcome(True, True, before, target, readback, rc)
            except CameraReadTimeout:
                abandoned = True             # 9d (V-38): the capture is gone -- no restore on it
                raise
            except Exception as e:   # boost must never crash unlock; fall back to the dark outcome
                out = BoostOutcome(False, True, before, target, readback, None, error=repr(e))
    finally:
        # GUARANTEED restore on every path but an abandoned capture: success / no-match /
        # not-honored / exception -- the exposure value first, then the auto mode (which may take
        # over from the value).
        if not abandoned:
            cap.set(prop, before)
            cap.set(aprop, auto_before)
    # Conservative on purpose: anything not read back as it was counts as NOT restored -- the
    # price of a false alarm is one reopen and no boost until the next service start.
    # The auto mode is compared tightly: DSHOW reports it as 0.25 (manual) / 0.75 (auto), which
    # HONORED_TOL (0.5, an exposure-step tolerance) would not tell apart.
    restored = (abs(float(cap.get(aprop)) - auto_before) < AUTO_MODE_TOL
                and honored(before, float(cap.get(prop))))
    return out._replace(restored=bool(restored))
