"""Active liveness primitives for the face-unlock service (Stage 2).

Blink detection (2d106) + head-pose gesture challenges (1k3d68 face.pose), built on the
landmarks InsightFace already produces per frame. No InsightFace import here and no extra
detections: the recognizer runs one app.get(bgr); the resulting face carries
.normed_embedding (recognition), .landmark_2d_106 (blink) and .pose (gesture). Enabling
those landmark modules in the recognizer's allowed_modules happens at integration
(Stage 2, Step 5) -- this module does not touch the recognizer.

Numbers (measured on this webcam)
---------------------------------
Blink: open eye avg EAR ~= 0.26, closed ~= 0.09 (Step 1/2). EAR_THRESH = 0.18.
Pose (Step 3, face.pose = [pitch, yaw, roll] in degrees):
  neutral  ~ pitch -13, yaw -3, roll +2
  turn L/R ~ yaw +-35     nod down ~ pitch -16 from neutral
Thresholds are taken relative to a baseline captured at challenge start so they don't
depend on the user's resting head position.
"""
from __future__ import annotations

import time
import random
from enum import Enum, auto

import numpy as np

# --- Blink (2d106) -----------------------------------------------------------------------

# Canonical eye landmark indices for the InsightFace 106-pt model. Single source of truth
# for the whole project (tools/liveness_probe.py imports these).
EYE_IDX: dict[str, list[int]] = {
    "left":  [35, 41, 42, 39, 37, 36],   # eye A: [outer, top1, top2, inner, bottom2, bottom1]
    "right": [89, 95, 96, 93, 91, 90],   # eye B
}

EAR_THRESH = 0.18       # below this = eye considered closed
BLINK_CONSEC = 2        # consecutive closed frames required to arm a blink

# --- Head pose (1k3d68 face.pose) --------------------------------------------------------

# Axis mapping in InsightFace's face.pose, confirmed on camera (Step 3).
POSE_PITCH = 0   # nod   (down = more negative)
POSE_YAW = 1     # turn left/right
POSE_ROLL = 2    # tilt

# Gesture thresholds in degrees, applied relative to a per-challenge baseline.
YAW_DELTA = 20.0          # |yaw - baseline| to count a left/right turn (full turn ~35)
PITCH_DOWN_DELTA = 10.0   # (baseline_pitch - pitch) to count a downward nod (nod ~16)
GESTURE_BASELINE_FRAMES = 3   # frames averaged for the neutral baseline

# Left/right sign convention on the mirror-flipped frame. If the probe shows prompts
# reversed (says LEFT but only a right turn resolves it), flip this single flag.
LEFT_IS_NEGATIVE_YAW = True

# --- Timing ------------------------------------------------------------------------------

BLINK_TIMEOUT_S = 4.0      # default blink window
GESTURE_TIMEOUT_S = 5.0    # gestures need a beat to read the prompt and move


def _ear_one(lm: np.ndarray, idx: list[int]) -> float:
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

    A completed blink is: open -> closed for >= consec_frames -> open. Feed one frame's
    landmarks per call; state persists across calls so one instance can span a verify loop
    or a challenge window.
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
            if self._state == EyeState.CLOSED:
                self.blinks += 1
            self._below = 0
            self._state = EyeState.OPEN
        return e

    def window(self, timeout_s: float = BLINK_TIMEOUT_S, clock=time.monotonic) -> "BlinkWindow":
        return BlinkWindow(self, timeout_s=timeout_s, clock=clock)


class BlinkWindow:
    """One blink challenge: require >= 1 completed blink before a deadline.

    feed() tolerates frames with no face (landmark=None) -- they still tick the deadline.
    `clock` is injectable so this is unit-testable without a camera or real waiting.
    """

    def __init__(self, detector: BlinkDetector, timeout_s: float = BLINK_TIMEOUT_S, clock=time.monotonic):
        self._det = detector
        self._clock = clock
        self._start = clock()
        self._deadline = self._start + timeout_s
        self._blinks_at_start = detector.blinks
        self._resolved = False
        self._passed = False

    def feed(self, landmark=None) -> tuple[bool, bool]:
        if self._resolved:
            return True, self._passed
        if landmark is not None:
            self._det.update(landmark)
            if self._det.blinks > self._blinks_at_start:
                self._resolved, self._passed = True, True
                return True, True
        if self._clock() >= self._deadline:
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


# --- Active challenge engine -------------------------------------------------------------

class Challenge(Enum):
    BLINK = auto()
    TURN_LEFT = auto()
    TURN_RIGHT = auto()
    NOD = auto()


class ChallengeState(Enum):
    IDLE = auto()
    AWAITING = auto()
    PASSED = auto()
    FAILED = auto()


PROMPTS: dict[Challenge, str] = {
    Challenge.BLINK: "Blink",
    Challenge.TURN_LEFT: "Turn head LEFT",
    Challenge.TURN_RIGHT: "Turn head RIGHT",
    Challenge.NOD: "Nod (chin down)",
}

GESTURE_KINDS = (Challenge.TURN_LEFT, Challenge.TURN_RIGHT, Challenge.NOD)
ALL_KINDS = (Challenge.BLINK,) + GESTURE_KINDS


class _BlinkTask:
    """Adapts BlinkWindow to the (landmark, pose) feed interface."""
    def __init__(self, timeout_s: float, clock):
        self._win = BlinkDetector().window(timeout_s=timeout_s, clock=clock)

    def feed(self, landmark, pose) -> tuple[bool, bool]:
        return self._win.feed(landmark)


class _PoseTask:
    """Head-pose gesture: reach a yaw/pitch deviation from a captured baseline before timeout."""
    def __init__(self, kind: Challenge, timeout_s: float, clock):
        self.kind = kind
        self._clock = clock
        self._deadline = clock() + timeout_s
        self._samples: list[tuple[float, float]] = []
        self._baseline: tuple[float, float] | None = None
        self._resolved = False
        self._passed = False

    def _target_met(self, pitch: float, yaw: float) -> bool:
        bpitch, byaw = self._baseline  # type: ignore[misc]
        left_sign = -1.0 if LEFT_IS_NEGATIVE_YAW else 1.0
        if self.kind == Challenge.TURN_LEFT:
            return (yaw - byaw) * left_sign > YAW_DELTA
        if self.kind == Challenge.TURN_RIGHT:
            return (yaw - byaw) * -left_sign > YAW_DELTA
        if self.kind == Challenge.NOD:
            return (bpitch - pitch) > PITCH_DOWN_DELTA
        return False

    def feed(self, landmark, pose) -> tuple[bool, bool]:
        if self._resolved:
            return True, self._passed
        if pose is not None:
            p = np.asarray(pose, dtype=np.float32).ravel()
            pitch, yaw = float(p[POSE_PITCH]), float(p[POSE_YAW])
            if self._baseline is None:
                self._samples.append((pitch, yaw))
                if len(self._samples) >= GESTURE_BASELINE_FRAMES:
                    arr = np.asarray(self._samples, dtype=np.float32)
                    self._baseline = (float(arr[:, 0].mean()), float(arr[:, 1].mean()))
            elif self._target_met(pitch, yaw):
                self._resolved, self._passed = True, True
                return True, True
        if self._clock() >= self._deadline:
            self._resolved, self._passed = True, False
        return self._resolved, self._passed


class LivenessChallenge:
    """Issue one random challenge and track it to PASS/FAIL.

    States: IDLE -> (issue) -> AWAITING -> PASSED | FAILED. Feed the per-frame landmarks
    and pose; the active task resolves on success or timeout. `rng` and `clock` are
    injectable for deterministic tests.
    """

    def __init__(self, kinds=ALL_KINDS, timeout_s: float | None = None,
                 rng: random.Random | None = None, clock=time.monotonic):
        self._kinds = tuple(kinds)
        self._timeout_s = timeout_s
        self._rng = rng or random.Random()
        self._clock = clock
        self.state = ChallengeState.IDLE
        self.kind: Challenge | None = None
        self._task = None

    def _timeout_for(self, kind: Challenge) -> float:
        if self._timeout_s is not None:
            return self._timeout_s
        return BLINK_TIMEOUT_S if kind == Challenge.BLINK else GESTURE_TIMEOUT_S

    def issue(self, kind: Challenge | None = None) -> Challenge:
        """Start a challenge (random from allowed kinds unless one is forced)."""
        self.kind = kind or self._rng.choice(self._kinds)
        t = self._timeout_for(self.kind)
        if self.kind == Challenge.BLINK:
            self._task = _BlinkTask(t, self._clock)
        else:
            self._task = _PoseTask(self.kind, t, self._clock)
        self.state = ChallengeState.AWAITING
        return self.kind

    @property
    def prompt(self) -> str:
        return PROMPTS.get(self.kind, "") if self.kind else ""

    def feed(self, landmark, pose) -> ChallengeState:
        """Feed one frame. Returns current state; terminal once PASSED or FAILED."""
        if self.state != ChallengeState.AWAITING or self._task is None:
            return self.state
        resolved, passed = self._task.feed(landmark, pose)
        if resolved:
            self.state = ChallengeState.PASSED if passed else ChallengeState.FAILED
        return self.state

    @property
    def done(self) -> bool:
        return self.state in (ChallengeState.PASSED, ChallengeState.FAILED)

    @property
    def passed(self) -> bool:
        return self.state == ChallengeState.PASSED
