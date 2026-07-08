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

``plan_exposure`` / ``honored`` are pure and camera-free (unit-tested). ``try_exposure_boost``
takes a cv2.VideoCapture-like handle plus a ``recapture`` callable, so a fake camera can drive it
in tests. Nothing here imports the recognizer or edits camera.py.
"""
from __future__ import annotations

from typing import Callable, NamedTuple

DEFAULT_EXPOSURE_STEP = 2.0   # +EV; matches the honored Step-3.1 step (-6 -> -4). cfg overrides.
HONORED_TOL = 0.5             # |readback - requested| <= this counts as the driver honoring the set


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
        return d


def try_exposure_boost(cap, step: float, recapture: Callable[[], object]) -> BoostOutcome:
    """Raise EXPOSURE, re-capture via ``recapture()``, and ALWAYS restore the original exposure.

    ``cap`` is a cv2.VideoCapture-like object (get/set on CAP_PROP_EXPOSURE). ``recapture`` runs one
    fresh analysis burst on the same (now-boosted) camera and returns whatever the caller needs
    (e.g. a VerifyOutcome). Behaviour:
      * driver ignores the set (roundtrip fails)  -> honored=False, NO re-capture (nothing changed);
      * driver honors it                          -> re-capture, applied=True;
      * re-capture raises                          -> swallowed (unlock must never crash), error set.
    In every case the ORIGINAL exposure is restored in the finally, so the camera is never left
    boosted for the next unlock or the presence loop.
    """
    import cv2
    prop = cv2.CAP_PROP_EXPOSURE
    before = float(cap.get(prop))
    target = plan_exposure(before, step)
    try:
        cap.set(prop, target)
        readback = float(cap.get(prop))
        if not honored(target, readback):
            return BoostOutcome(False, False, before, target, readback, None)
        try:
            rc = recapture()
        except Exception as e:   # boost must never crash unlock; fall back to the dark outcome
            return BoostOutcome(False, True, before, target, readback, None, error=repr(e))
        return BoostOutcome(True, True, before, target, readback, rc)
    finally:
        # GUARANTEED restore on every path: success / no-match / not-honored / exception.
        cap.set(prop, before)
