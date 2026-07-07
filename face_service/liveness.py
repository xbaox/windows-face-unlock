"""Active liveness primitives for the face-unlock service (Stage 2).

Blink detection built on InsightFace's 2d106 landmarks. This module is deliberately
free of side effects and of any InsightFace import: it consumes the (106, 2) landmark
array that the recognizer already produces per frame, so enabling active liveness costs
*zero* extra detections (the whole reason MiniFASNet/DeepFace is being removed).

Pipeline role
-------------
recognizer.verify_frame() runs one app.get(bgr); the resulting face carries both
.normed_embedding (recognition) and .landmark_2d_106 (landmarks). We feed the latter to
a BlinkDetector. Enabling `landmark_2d_106` in the recognizer's allowed_modules happens at
integration time (Stage 2, Step 5) -- this module does not touch the recognizer.

Numbers (measured on this webcam, Step 1)
-----------------------------------------
open eye avg EAR ~= 0.249, closed ~= 0.073 (a 3.4x gap). EAR_THRESH = 0.18 sits at ~35%
of open / ~2.5x closed; BLINK_CONSEC = 2 rejects single-frame EAR noise.

Landmark indices are the InsightFace 106-pt (2d106det) scheme, locked on numbers in Step 1:
each eye is [outer_corner, top1, top2, inner_corner, bottom2, bottom1].
"""
from __future__ import annotations

import time
from enum import Enum, auto

import numpy as np

# Canonical eye landmark indices for the InsightFace 106-pt model. Single source of truth
# for the whole project (tools/liveness_probe.py holds a copy to be de-duped against this).
EYE_IDX: dict[str, list[int]] = {
    "left":  [35, 41, 42, 39, 37, 36],   # eye A: [outer, top1, top2, inner, bottom2, bottom1]
    "right": [89, 95, 96, 93, 91, 90],   # eye B
}

# Defaults derived from Step 1 measurements. At integration these come from Config so they
# stay tunable without code changes; kept here as sane fallbacks.
EAR_THRESH = 0.18       # below this = eye considered closed
BLINK_CONSEC = 2        # consecutive closed frames required to arm a blink
BLINK_TIMEOUT_S = 4.0   # default challenge window length


def _ear_one(lm: np.ndarray, idx: list[int]) -> float:
    """Eye aspect ratio for one eye from 6 ordered points."""
    p1, p2, p3, p4, p5, p6 = (lm[i] for i in idx)
    a = float(np.linalg.norm(p2 - p6))
    b = float(np.linalg.norm(p3 - p5))
    c = float(np.linalg.norm(p1 - p4))
    return (a + b) / (2.0 * c + 1e-9)


def compute_ear(landmark, eye_idx: dict[str, list[int]] = EYE_IDX) -> float:
    """Average EAR over both eyes. `landmark` is the (106, 2) array from InsightFace 2d106."""
    lm = np.asarray(landmark, dtype=np.float32)
    return 0.5 * (_ear_one(lm, eye_idx["left"]) + _ear_one(lm, eye_idx["right"]))


class EyeState(Enum):
    OPEN = auto()
    CLOSED = auto()


class BlinkDetector:
    """Streaming blink counter with a small state machine.

    A *completed* blink is: eyes open -> closed for >= consec_frames -> open again. Feed one
    frame's landmarks per call to update(); the detector remembers state across calls, so the
    same instance can span an entire verify loop or an active-challenge window.
    """

    def __init__(
        self,
        eye_idx: dict[str, list[int]] = EYE_IDX,
        ear_thresh: float = EAR_THRESH,
        consec_frames: int = BLINK_CONSEC,
    ):
        self.eye_idx = eye_idx
        self.ear_thresh = ear_thresh
        self.consec_frames = consec_frames
        self.reset()

    def reset(self) -> None:
        self._below = 0
        self._state = EyeState.OPEN
        self.blinks = 0
        self.last_ear: float | None = None

    @property
    def state(self) -> EyeState:
        return self._state

    def update(self, landmark) -> float:
        """Feed one frame's 106-pt landmarks. Updates state, returns this frame's EAR."""
        e = compute_ear(landmark, self.eye_idx)
        self.last_ear = e
        if e < self.ear_thresh:
            self._below += 1
            if self._below >= self.consec_frames:
                self._state = EyeState.CLOSED
        else:
            # rising edge: a real blink only if we had actually reached CLOSED
            if self._state == EyeState.CLOSED:
                self.blinks += 1
            self._below = 0
            self._state = EyeState.OPEN
        return e

    def window(self, timeout_s: float = BLINK_TIMEOUT_S, clock=time.monotonic) -> "BlinkWindow":
        """Open a challenge window on this detector: require >= 1 blink before timeout_s."""
        return BlinkWindow(self, timeout_s=timeout_s, clock=clock)


class BlinkWindow:
    """One blink challenge: require at least one completed blink before a deadline.

    Feed frames via .feed(); it drives the underlying detector and reports when the challenge
    resolves. `clock` is injectable (default time.monotonic) so this is unit-testable without
    a camera and without real waiting.
    """

    def __init__(self, detector: BlinkDetector, timeout_s: float = BLINK_TIMEOUT_S, clock=time.monotonic):
        self._det = detector
        self._clock = clock
        self._start = clock()
        self._deadline = self._start + timeout_s
        self._blinks_at_start = detector.blinks
        self._resolved = False
        self._passed = False

    def feed(self, landmark) -> tuple[bool, bool]:
        """Feed one frame. Returns (resolved, passed).

        resolved=True once a blink is seen (passed=True) or the deadline is reached
        (passed=False). After resolution further feeds are inert and return the frozen result.
        """
        if self._resolved:
            return True, self._passed
        self._det.update(landmark)
        if self._det.blinks > self._blinks_at_start:
            self._resolved, self._passed = True, True
        elif self._clock() >= self._deadline:
            self._resolved, self._passed = True, False
        return self._resolved, self._passed

    @property
    def resolved(self) -> bool:
        return self._resolved

    @property
    def passed(self) -> bool:
        return self._passed

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start
