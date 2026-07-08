"""Low-light detection + honest refusal (Stage 3 / Step 3.2).

Two small pieces, both pure/unit-testable without a camera (like ``adaptive.py``):

* ``scene_luma(bgr)`` -- mean gray level of the WHOLE frame (0..255). This is a
  face-INDEPENDENT scene-brightness proxy: it exists on every frame, including ones
  where no face is detected, so "the room is too dark to see a face at all" is caught
  by the scene, not by a per-crop metric that only exists once a face is found. This is
  the CANONICAL formula -- ``tools.lowlight_probe`` imports this same function, and the
  ``low_light_luma_min`` floor was measured on this webcam with exactly this metric.

* ``evaluate_low_light(scene_luma, luma_min, would_grant)`` -- a PURE decision. Below the
  floor we force an honest deny (``too-dark``) EVEN IF recognition would otherwise grant,
  because in the dark the passive defenses are unreliable (per Step 3.1: the match distance
  climbs past threshold and the anti-screen hf signal drifts and false-flags ~88% of live
  frames). Above the floor it is a pass-through: the caller's existing grant/deny/reason is
  untouched. The gate is deliberately conservative (floor well below the clean-pass band and
  well above the match-breakdown point measured in 3.1).

This module makes NO policy about the lockout counter or the audit trail -- those are the
service's job. In particular ``too-dark`` is an ENVIRONMENT outcome, so the service must NOT
count it as a failed match (no lockout strike, no reset); this module only tells it *whether*
the frame is too dark. Nothing here mutates state, touches the camera, or performs I/O beyond
the one cvtColor in ``scene_luma``.
"""
from __future__ import annotations

# The reason token surfaced in the service's deny response / audit for a low-light refusal.
# kebab-case to match the existing service reasons ("no-match", "no-credentials", ...).
TOO_DARK_REASON = "too-dark"


def scene_luma(bgr) -> float:
    """Mean gray level of the whole BGR frame, 0..255 (float).

    CANONICAL scene-brightness metric. Identical formula to the one
    ``tools.lowlight_probe`` measured the floor with, so the probe and the production gate
    can never numerically diverge. cv2 is imported lazily so importing this module (e.g. from
    the pure gate self-test) needs neither OpenCV nor a camera.
    """
    import cv2
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(gray.mean())


def evaluate_low_light(scene_luma_value, luma_min, would_grant):
    """Decide the low-light gate. Returns ``(grant, reason, too_dark)``.

    * ``scene_luma_value < luma_min`` -> ``(False, TOO_DARK_REASON, True)``: force a deny even
      when ``would_grant`` is True (in the dark the passive anti-spoof / recognition margin is
      not trustworthy). The comparison is STRICT ``<`` so ``scene_luma == luma_min`` passes.
    * otherwise -> ``(would_grant, None, False)``: pass through the caller's existing decision
      and reason unchanged.

    ``luma_min <= 0`` disables the gate: no scene luma (a mean of non-negative pixels) is ever
    ``< 0``, so nothing is ever flagged too-dark -- an intentional escape hatch. Pure: no I/O,
    no camera, no state -> fully unit-testable.
    """
    too_dark = float(scene_luma_value) < float(luma_min)
    if too_dark:
        return False, TOO_DARK_REASON, True
    return bool(would_grant), None, False
