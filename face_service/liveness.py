"""Active liveness primitives for the face-unlock service (Stage 2).

Blink detection (2d106) + head-pose gesture challenges (1k3d68 face.pose) + an anti-screen
check on the face crop (frequency + texture). Everything here consumes what the recognizer
already produces per frame; no InsightFace import and no extra detections. Enabling the
landmark modules in the recognizer's allowed_modules happens at integration (Step 5).

Numbers (measured on this webcam)
---------------------------------
Blink: open EAR ~0.26, closed ~0.09 (Step 1/2). EAR_THRESH = 0.18.
Pose (face.pose = [pitch, yaw, roll] deg, Step 3): neutral ~ pitch -13 / yaw -3;
  turn L/R ~ yaw +-35; nod down ~ pitch -16 from neutral.
Anti-screen (Step 4, 3 sessions): live-face FFT/texture features drift with lighting as much
as screen features do (live hf 0.198->0.189 across sessions; live lap std 28->52; lap sep
collapsed to 0.24 in dim light). Absolute thresholds therefore trade FP vs detection poorly.
Kept as a WEAK doubt trigger only: hf-only, conservative threshold (<1% live FP on the worst
session), escalates to a gesture rather than hard-rejecting. The gesture is the real replay
defense (behavioral, lighting-independent). lap/peak -> audit telemetry, not in the gate.
"""
from __future__ import annotations

import time
import random
from enum import Enum, auto
from typing import NamedTuple

import numpy as np

# --- Blink (2d106) -----------------------------------------------------------------------

EYE_IDX: dict[str, list[int]] = {
    "left":  [35, 41, 42, 39, 37, 36],   # eye A: [outer, top1, top2, inner, bottom2, bottom1]
    "right": [89, 95, 96, 93, 91, 90],   # eye B
}

EAR_THRESH = 0.18
BLINK_CONSEC = 2

# --- Head pose (1k3d68 face.pose) --------------------------------------------------------

POSE_PITCH = 0   # nod   (down = more negative)
POSE_YAW = 1     # turn left/right
POSE_ROLL = 2    # tilt

YAW_DELTA = 20.0
PITCH_DOWN_DELTA = 10.0
GESTURE_BASELINE_FRAMES = 3
LEFT_IS_NEGATIVE_YAW = False  # Live lock-screen calibration 2026-07-27 on the production
                              # camera: the earlier "left turn -> yaw ~ -37" reading had the
                              # sign the wrong way round against the USER, so both turn
                              # challenges asked for one direction and only accepted the other
                              # (prompt "turn right" + a real right turn -> gesture-failed with
                              # the face held all round). InsightFace's pose convention on this
                              # hardware reports a POSITIVE yaw deviation when the user turns to
                              # their OWN left. Sign confirmed by a lock-screen run; prompt /
                              # kind / audit now all mean what the user physically does.

BLINK_TIMEOUT_S = 4.0
GESTURE_TIMEOUT_S = 5.0

# --- Anti-screen (frequency + texture on the face crop) ----------------------------------

SCREEN_CROP = 128
# Anti-screen is a WEAK conditional signal, not a hard gate. Across 3 sessions the live-face
# FFT/texture features drifted with lighting (live hf 0.198->0.189, live lap std 28->52) and
# lap's separation collapsed (sep 0.24 under variable light). So: hf-only gate with a
# conservative threshold chosen for <1% live false-positive on the WORST observed session
# (drift-robust), used only as a doubt trigger that escalates to an active gesture -- never a
# hard reject in fast mode. The robust replay defense is the gesture challenge. lap/peak are
# logged to the audit trail as telemetry but are NOT in the gate.
HF_THRESH = 0.150     # gate: screen-like when hf < this (drift-robust, live FP <1% all sessions)
LAP_AUDIT = 260.0     # telemetry only (moire energy reference; unreliable across lighting)
PEAK_AUDIT = 8.8      # telemetry only (weakest separator)


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
    """Streaming blink counter. A completed blink is open -> closed(>=consec) -> open.
    State persists across update() calls so one instance can span a verify loop or window.
    """

    def __init__(self, eye_idx=EYE_IDX, ear_thresh=EAR_THRESH, consec_frames=BLINK_CONSEC):
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
    """Require >= 1 completed blink before a deadline. feed() tolerates no-face frames
    (landmark=None) -- they still tick the deadline. Clock is injectable for tests.
    """

    def __init__(self, detector: BlinkDetector, timeout_s=BLINK_TIMEOUT_S, clock=time.monotonic):
        self._det = detector
        self._clock = clock
        self._deadline = clock() + timeout_s
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
    def __init__(self, timeout_s: float, clock):
        self._win = BlinkDetector().window(timeout_s=timeout_s, clock=clock)

    def feed(self, landmark, pose) -> tuple[bool, bool]:
        return self._win.feed(landmark)


class _PoseTask:
    """Reach a yaw/pitch deviation from a captured baseline before timeout."""

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
    IDLE -> (issue) -> AWAITING -> PASSED | FAILED. rng/clock injectable for tests.
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
        self.kind = kind or self._rng.choice(self._kinds)
        t = self._timeout_for(self.kind)
        self._task = _BlinkTask(t, self._clock) if self.kind == Challenge.BLINK \
            else _PoseTask(self.kind, t, self._clock)
        self.state = ChallengeState.AWAITING
        return self.kind

    @property
    def prompt(self) -> str:
        return PROMPTS.get(self.kind, "") if self.kind else ""

    def feed(self, landmark, pose) -> ChallengeState:
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


# --- Anti-screen detector ----------------------------------------------------------------

class ScreenFeatures(NamedTuple):
    hf: float
    peak: float
    lap: float


_SCREEN_WIN: np.ndarray | None = None
_SCREEN_R: np.ndarray | None = None


def _screen_win() -> np.ndarray:
    global _SCREEN_WIN
    if _SCREEN_WIN is None:
        _SCREEN_WIN = np.outer(np.hanning(SCREEN_CROP), np.hanning(SCREEN_CROP)).astype(np.float32)
    return _SCREEN_WIN


def _screen_radius() -> np.ndarray:
    global _SCREEN_R
    if _SCREEN_R is None:
        c = SCREEN_CROP // 2
        Y, X = np.ogrid[:SCREEN_CROP, :SCREEN_CROP]
        r = np.sqrt((X - c) ** 2 + (Y - c) ** 2)
        _SCREEN_R = (r / r.max()).astype(np.float32)
    return _SCREEN_R


def face_gray(bgr, bbox, size: int = SCREEN_CROP):
    """Resized grayscale face crop for anti-screen features; None if the bbox is too small."""
    import cv2
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(bgr.shape[1], x2), min(bgr.shape[0], y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    g = cv2.cvtColor(bgr[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (size, size)).astype(np.float32)


def screen_features(gray) -> ScreenFeatures:
    """FFT high-freq fraction (hf) + mid-band spectral peakiness (peak) + Laplacian variance
    (lap) on a grayscale face crop. hf drops for a display recapture; peak/lap rise with moire.
    """
    import cv2
    g = (gray - gray.mean()) * _screen_win()
    mag = np.abs(np.fft.fftshift(np.fft.fft2(g)))
    rn = _screen_radius()
    total = float(mag.sum()) + 1e-9
    hf = float(mag[rn > 0.5].sum()) / total
    band = mag[(rn > 0.35) & (rn < 0.75)]
    peak = float(band.max() / (band.mean() + 1e-9)) if band.size else 0.0
    lap = float(cv2.Laplacian(gray, cv2.CV_32F).var())   # CV_32F: OpenCV 5 drops 32F->64F
    return ScreenFeatures(hf, peak, lap)


def is_screen_features(f: ScreenFeatures, hf_thresh: float = HF_THRESH) -> bool:
    """Anti-screen gate: screen-like when the fine-texture fraction is low (a display recapture
    loses skin texture). hf-only and conservative -- across sessions this drifts with lighting,
    so the threshold is set for <1% live false-positive on the worst observed session and the
    result is used as a doubt trigger (escalate to gesture), not a hard reject. lap/peak are
    telemetry, deliberately NOT in the gate. Pure function -> unit-testable without a camera."""
    return f.hf < hf_thresh


class ScreenDetector:
    """Per-frame anti-screen check on the face crop. Primary signal = FFT high-frequency
    fraction (a display recapture loses fine skin texture, dropping hf). Returns a per-frame
    flag; the verify loop aggregates across frames so one noisy frame can't cause a false
    reject, and a screen suspicion escalates to an active gesture rather than hard-rejecting.
    """

    def __init__(self, hf_thresh: float = HF_THRESH):
        self.hf_thresh = hf_thresh
        self.last: ScreenFeatures | None = None

    def check(self, bgr, bbox) -> bool | None:
        """True = screen-like (doubt -> escalate), False = live-like, None = crop unusable.
        self.last holds the full ScreenFeatures (hf/peak/lap) for the audit trail."""
        g = face_gray(bgr, bbox)
        if g is None:
            return None
        self.last = screen_features(g)
        return is_screen_features(self.last, self.hf_thresh)


# --- Decision layer (mode-aware verdict) -------------------------------------------------

class Verdict(Enum):
    PASS = auto()           # recognized + live enough -> unlock now
    NOT_LIVE = auto()       # deny: no confirmed live enrolled face (too few matches, or a
                            # static-spoof signature = screen-flagged AND never blinked)
    NEEDS_GESTURE = auto()  # ambiguous -> escalate to an active gesture (Credential Provider,
                            # master-Stage 5). At the Stage-2 PASSIVE lockscreen there is no UI
                            # to run the gesture, so the service maps this to a deny; the
                            # tri-state exists so the *same* verdict drives the CP gesture loop
                            # unchanged once Stage 5 lands.


# Decision-layer policy knobs. These are POLICY, not measured spoof constants -- but they are
# informed by the locked numbers: anti-screen live per-frame FP <1% and screen detection
# ~40-70% (liveness.py header), and the Stage-1 self distance ~0.07 at threshold 0.45. Re-check
# them against real impostor/spoof margins in Step 9; conservative is safe until then.
SCREEN_DOUBT_FRAC = 0.34   # >= this share of frames screen-flagged -> screen suspicion.
                           # With the default 5-frame burst that is >=2 flagged frames: a live
                           # face (<1% per-frame FP) will not reach it, a screen (~40-70% per
                           # frame) will. Fraction-based so it is robust to the frame count.
STRONG_MARGIN = 0.10       # threshold - best_distance >= this -> confident recognition (may skip
                           # the gesture in fast mode). Self margin ~0.38 today (~0.28 after a
                           # ~0.35 threshold in Step 9); a borderline impostor sits near 0.


def verdict(
    matches: int,
    blinked: bool,
    screen_frac: float,
    margin: float,
    mode: str = "fast",
    *,
    required_matches: int,
    challenge_on_doubt: bool = True,
    screen_doubt_frac: float = SCREEN_DOUBT_FRAC,
    strong_margin: float = STRONG_MARGIN,
) -> Verdict:
    """Fold multi-frame recognition + passive liveness into one decision.

    The service verify loop accumulates these over a short burst of frames and calls this once:
      matches           frames whose embedding matched the enrolled face
      blinked           a spontaneous blink was observed in the window (passive liveness)
      screen_frac       fraction of analyzed frames the anti-screen check flagged (0..1)
      margin            threshold - best_distance (>0 = confident match; may be <0 if no match)
      mode              "fast" (challenge only on doubt) | "paranoid" (always challenge)
      required_matches  matching frames the recognition gate needs (cfg.verify_required)
      challenge_on_doubt  fast mode only: on doubt escalate to a gesture (True) or hard-deny (False)

    Pure function (no camera / no InsightFace) -> the whole decision matrix is unit-testable.

    fast:
      - clean + confident (not screen-flagged, margin >= strong_margin) -> PASS (subsecond;
        a spontaneous blink is a bonus, not required on the clean path)
      - screen-flagged AND no blink -> NOT_LIVE (flat photo / static screen: no texture, no life)
      - any other doubt (screen-flagged-but-blinked, or thin margin) -> NEEDS_GESTURE
        (or NOT_LIVE when challenge_on_doubt is False)
    paranoid:
      - a hard static-spoof signature (screen-flagged AND no blink) -> NOT_LIVE
      - otherwise every valid match -> NEEDS_GESTURE (always require the gesture)
    both modes: matches < required_matches -> NOT_LIVE (nothing to admit).
    """
    if mode not in ("fast", "paranoid"):
        mode = "fast"  # defensive: an unknown mode falls back to the safe default

    # Recognition gate: without enough matching frames there is no enrolled face to admit.
    if matches < required_matches:
        return Verdict.NOT_LIVE

    screen_doubt = screen_frac >= screen_doubt_frac
    static_spoof = screen_doubt and not blinked   # screen-like texture that never blinked

    if mode == "paranoid":
        return Verdict.NOT_LIVE if static_spoof else Verdict.NEEDS_GESTURE

    # fast
    if static_spoof:
        return Verdict.NOT_LIVE
    doubt = screen_doubt or (margin < strong_margin)
    if doubt:
        return Verdict.NEEDS_GESTURE if challenge_on_doubt else Verdict.NOT_LIVE
    return Verdict.PASS
