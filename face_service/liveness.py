"""Active liveness primitives for the face-unlock service (Stage 2).

Blink detection (2d106) + head-pose gesture challenges (1k3d68 face.pose) + an anti-screen
check on the face crop (frequency + texture). Everything here consumes what the recognizer
already produces per frame; no InsightFace import and no extra detections. (Stage 9, D-78: the
landmark modules have been enabled in the recognizer since Stage 5.)

Stage 9 (act 9b R4): the lock screen's phase 2 is a GestureSequence -- two DIFFERENT head
movements from {turn_left, turn_right, nod} in a random order, performed in that order, after a
still start. A blink is no longer a phase-2 gesture (any video of the owner contains blinks); the
passive blink still counts in phase 1.

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
defense (behavioral, lighting-independent). lap/peak are reported per attempt as telemetry
(Stage 9, R6) and are not in the gate.
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

# --- Stage 9 (act 9b R4): the phase-2 sequence ------------------------------------------------
# New constants only; every value above is unchanged. To be revisited with the 9e measurements.
GESTURE_SEQUENCE_LEN = 2      # two different movements from GESTURE_KINDS, in a random order
STILLNESS_WINDOW_S = 0.4      # the first frames of phase 2 must be still...
STILLNESS_MAX_DEG = 4.0       # ...frame-to-frame |d yaw| and |d pitch| at most this many degrees
# The whole round: the still start, each step with its own GESTURE_TIMEOUT_S, and 2 s of slack.
ROUND_CAP_S = STILLNESS_WINDOW_S + GESTURE_SEQUENCE_LEN * GESTURE_TIMEOUT_S + 2.0

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
# (Stage 9, D-79: the unused LAP_AUDIT / PEAK_AUDIT reference numbers are gone; lap and peak are
# reported raw in the per-attempt telemetry.)


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


def default_left_sign() -> float:
    """+1 when a turn to the user's own left raises yaw (LEFT_IS_NEGATIVE_YAW False), else -1."""
    return -1.0 if LEFT_IS_NEGATIVE_YAW else 1.0


def kind_met(kind: Challenge, baseline: tuple[float, float], pitch: float, yaw: float,
             left_sign: float) -> bool:
    """Has (pitch, yaw) moved far enough from ``baseline`` for ``kind``? ``left_sign`` is the
    per-camera calibration (Stage 9, R6), or default_left_sign() without one."""
    bpitch, byaw = baseline
    if kind == Challenge.TURN_LEFT:
        return (yaw - byaw) * left_sign > YAW_DELTA
    if kind == Challenge.TURN_RIGHT:
        return (yaw - byaw) * -left_sign > YAW_DELTA
    if kind == Challenge.NOD:
        return (bpitch - pitch) > PITCH_DOWN_DELTA
    return False


class _PoseTask:
    """Reach a yaw/pitch deviation from a captured baseline before timeout."""

    def __init__(self, kind: Challenge, timeout_s: float, clock, left_sign: float | None = None):
        self.kind = kind
        self._clock = clock
        self._deadline = clock() + timeout_s
        self._samples: list[tuple[float, float]] = []
        self._baseline: tuple[float, float] | None = None
        self._resolved = False
        self._passed = False
        self._left_sign = default_left_sign() if left_sign is None else float(left_sign)

    @property
    def baseline(self) -> "tuple[float, float] | None":
        return self._baseline

    def _target_met(self, pitch: float, yaw: float) -> bool:
        return kind_met(self.kind, self._baseline, pitch, yaw, self._left_sign)  # type: ignore[arg-type]

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


class GestureSequence:
    """Stage 9 (act 9b R4): the lock screen's phase 2.

    ``kinds`` is GESTURE_SEQUENCE_LEN DIFFERENT movements from GESTURE_KINDS, to be performed in
    that order. Nothing runs until the first frame arrives: the round clock (ROUND_CAP_S), the
    stillness window and the first step's own GESTURE_TIMEOUT_S all start there, not at issue time
    -- a cold camera no longer eats the user's window (F-140). Then:
      * still start: during the first STILLNESS_WINDOW_S of frames, frame-to-frame |d yaw| and
        |d pitch| must stay within STILLNESS_MAX_DEG, else the round fails with
        ``motion-before-prompt`` (a replay that is already moving when the prompt appears);
      * each step is the existing pose detector with its own baseline and GESTURE_TIMEOUT_S; the
        next step starts when the previous one passes;
      * the order is checked: reaching a LATER step's target while an earlier step is still open
        fails the round with ``gesture-order``;
      * the whole round ends by ROUND_CAP_S (``round-timeout``).
    ``reason`` names the failure; ``steps_done`` counts the steps passed. Deterministic with an
    injected clock.
    """

    def __init__(self, kinds, *, clock=time.monotonic, left_sign: float | None = None):
        kinds = tuple(kinds)
        if (len(kinds) != GESTURE_SEQUENCE_LEN or len(set(kinds)) != len(kinds)
                or any(k not in GESTURE_KINDS for k in kinds)):
            raise ValueError(f"a phase-2 sequence is {GESTURE_SEQUENCE_LEN} different head "
                             f"movements from {[k.name for k in GESTURE_KINDS]}, got {kinds}")
        self.kinds = kinds
        self._clock = clock
        self._left_sign = default_left_sign() if left_sign is None else float(left_sign)
        self.state = ChallengeState.AWAITING
        self.reason: str | None = None
        self.steps_done = 0
        self._t0: float | None = None
        self._task: _PoseTask | None = None
        self._prev: tuple[float, float] | None = None

    @property
    def started(self) -> bool:
        return self._t0 is not None

    @property
    def done(self) -> bool:
        return self.state in (ChallengeState.PASSED, ChallengeState.FAILED)

    @property
    def passed(self) -> bool:
        return self.state == ChallengeState.PASSED

    def _fail(self, reason: str) -> ChallengeState:
        self.state, self.reason = ChallengeState.FAILED, reason
        return self.state

    def start(self) -> None:
        """Start the clocks (the first frame arrived). Idempotent."""
        if self._t0 is None:
            self._t0 = self._clock()
            self._task = _PoseTask(self.kinds[0], GESTURE_TIMEOUT_S, self._clock, self._left_sign)

    def tick(self) -> ChallengeState:
        """Account for time passing without a usable frame (no face, a dropped frame)."""
        if self.done or self._t0 is None:
            return self.state
        if self._clock() - self._t0 >= ROUND_CAP_S:
            return self._fail("round-timeout")
        resolved, passed = self._task.feed(None, None)   # type: ignore[union-attr]
        if resolved and not passed:
            return self._fail("gesture-timeout")
        return self.state

    def feed(self, landmark, pose) -> ChallengeState:
        if self.done:
            return self.state
        self.start()
        now = self._clock()
        if now - self._t0 >= ROUND_CAP_S:                          # type: ignore[operator]
            return self._fail("round-timeout")
        if pose is not None:
            p = np.asarray(pose, dtype=np.float32).ravel()
            pitch, yaw = float(p[POSE_PITCH]), float(p[POSE_YAW])
            if now - self._t0 <= STILLNESS_WINDOW_S and self._prev is not None:   # type: ignore[operator]
                if (abs(yaw - self._prev[1]) > STILLNESS_MAX_DEG
                        or abs(pitch - self._prev[0]) > STILLNESS_MAX_DEG):
                    return self._fail("motion-before-prompt")
            self._prev = (pitch, yaw)
            base = self._task.baseline                              # type: ignore[union-attr]
            if base is not None:
                for later in self.kinds[self.steps_done + 1:]:
                    if kind_met(later, base, pitch, yaw, self._left_sign):
                        return self._fail("gesture-order")
        resolved, passed = self._task.feed(landmark, pose)         # type: ignore[union-attr]
        if not resolved:
            return self.state
        if not passed:
            return self._fail("gesture-timeout")
        self.steps_done += 1
        if self.steps_done >= len(self.kinds):
            self.state = ChallengeState.PASSED
            return self.state
        self._task = _PoseTask(self.kinds[self.steps_done], GESTURE_TIMEOUT_S, self._clock,
                               self._left_sign)
        return self.state


def random_sequence(rng=None) -> tuple:
    """GESTURE_SEQUENCE_LEN different kinds from GESTURE_KINDS in a random order. ``rng`` must
    offer sample(); the service passes nothing, which means secrets.SystemRandom, so the order
    cannot be predicted."""
    import secrets as _secrets
    rng = rng or _secrets.SystemRandom()
    return tuple(rng.sample(list(GESTURE_KINDS), GESTURE_SEQUENCE_LEN))


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
    NEEDS_GESTURE = auto()  # ambiguous -> the lock screen runs phase 2 (the GestureSequence);
                            # the Credential Provider has done so since Stage 7-i (D-78).


# Decision-layer policy knobs. These are POLICY, not measured spoof constants -- but they are
# informed by the locked numbers: anti-screen live per-frame FP <1% and screen detection
# ~40-70% (liveness.py header), and a self distance of ~0.07-0.12 at the 0.32 threshold. The
# 9a-5 spoof measurement ("before") and 9e ("after") are the references for revisiting them.
SCREEN_DOUBT_FRAC = 0.34   # >= this share of frames screen-flagged -> screen suspicion.
                           # With the default 5-frame burst that is >=2 flagged frames: a live
                           # face (<1% per-frame FP) will not reach it, a screen (~40-70% per
                           # frame) will. Fraction-based so it is robust to the frame count.
STRONG_MARGIN = 0.10       # threshold - best_distance >= this -> confident recognition (may skip
                           # the gesture in fast mode). Self margin ~0.2-0.25 at the 0.32
                           # threshold; a borderline impostor sits near 0.


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
