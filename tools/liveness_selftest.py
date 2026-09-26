#!/usr/bin/env python3
"""Synthetic self-test for face_service.liveness -- no camera, no real waiting.

Covers: EAR/blink state machine, blink window, head-pose gesture tasks, and the
LivenessChallenge engine (issue -> AWAITING -> PASSED/FAILED). All clocks are injected
(a manual counter) and pose/landmarks are constructed, so results are deterministic.

Run from repo root:
    python tools\\liveness_selftest.py
"""
from __future__ import annotations

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from face_service.liveness import (
    EYE_IDX, compute_ear, BlinkDetector, BlinkWindow, EAR_THRESH,
    Challenge, ChallengeState, LivenessChallenge,
    POSE_PITCH, POSE_YAW, YAW_DELTA, PITCH_DOWN_DELTA,
    ScreenFeatures, is_screen_features, screen_features, HF_THRESH,
)

OPEN_EAR = 0.25
CLOSED_EAR = 0.07

# Neutral pose baseline [pitch, yaw, roll] roughly matching measured values.
NEUTRAL = [-13.0, -3.0, 2.0]


def synthetic_landmark(ear: float) -> np.ndarray:
    lm = np.zeros((106, 2), dtype=np.float32)
    W = 20.0
    h = ear * W
    for o, t1, t2, i_, b2, b1 in (EYE_IDX["left"], EYE_IDX["right"]):
        lm[o] = (0.0, 0.0)
        lm[i_] = (W, 0.0)
        lm[t1] = (W / 3.0, +h / 2.0)
        lm[b1] = (W / 3.0, -h / 2.0)
        lm[t2] = (2 * W / 3.0, +h / 2.0)
        lm[b2] = (2 * W / 3.0, -h / 2.0)
    return lm


OPEN = synthetic_landmark(OPEN_EAR)
CLOSED = synthetic_landmark(CLOSED_EAR)


def pose(pitch=None, yaw=None, roll=None):
    p = list(NEUTRAL)
    if pitch is not None:
        p[POSE_PITCH] = pitch
    if yaw is not None:
        p[POSE_YAW] = yaw
    if roll is not None:
        p[2] = roll
    return np.asarray(p, dtype=np.float32)


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def tick(self, dt=1.0):
        self.t += dt


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


def main():
    print("EAR sanity:")
    check("open EAR reproduces target", abs(compute_ear(OPEN) - OPEN_EAR) < 1e-3)
    check("closed EAR reproduces target", abs(compute_ear(CLOSED) - CLOSED_EAR) < 1e-3)
    check("threshold separates open/closed", compute_ear(CLOSED) < EAR_THRESH < compute_ear(OPEN))

    print("\nBlinkDetector state machine:")
    d = BlinkDetector()
    for _ in range(30):
        d.update(OPEN)
    check("steady open -> 0 blinks", d.blinks == 0)
    d.reset()
    for lm in [OPEN] * 5 + [CLOSED] * 3 + [OPEN] * 5:
        d.update(lm)
    check("open/closed(3)/open -> 1 blink", d.blinks == 1)
    d.reset()
    for lm in [OPEN] * 5 + [CLOSED] * 1 + [OPEN] * 5:
        d.update(lm)
    check("single-frame dip filtered -> 0 blinks", d.blinks == 0)
    d.reset()
    for lm in ([OPEN] * 3 + [CLOSED] * 2) * 2 + [OPEN] * 3:
        d.update(lm)
    check("two blinks -> 2", d.blinks == 2)

    print("\nBlinkWindow:")
    clk = FakeClock()
    w = BlinkDetector().window(timeout_s=4.0, clock=clk)
    for _ in range(3):
        w.feed(OPEN); clk.tick(0.1)
    w.feed(CLOSED); clk.tick(0.1)
    w.feed(CLOSED); clk.tick(0.1)
    resolved, passed = w.feed(OPEN)
    check("blink before deadline -> passed", resolved and passed)

    clk = FakeClock()
    w = BlinkDetector().window(timeout_s=2.0, clock=clk)
    resolved = passed = None
    for _ in range(30):
        resolved, passed = w.feed(OPEN); clk.tick(0.2)
    check("no blink past deadline -> failed", resolved and not passed)
    # no-face frames still tick the deadline
    clk = FakeClock()
    w = BlinkDetector().window(timeout_s=1.0, clock=clk)
    r = p = None
    for _ in range(10):
        r, p = w.feed(None); clk.tick(0.2)
    check("no-face frames time out cleanly", r and not p)

    print("\nHead-pose gesture (LivenessChallenge, forced kind):")
    # Signs are written out LITERALLY, in terms of what the user physically does, and are no
    # longer derived from LEFT_IS_NEGATIVE_YAW. Deriving them made these checks tautological --
    # they passed for either value of the constant, so the inverted turn directions sailed
    # through here and were only caught on a live lock screen. Live calibration 2026-07-27:
    # a turn to the user's OWN left reads as a POSITIVE yaw deviation on this hardware.
    left_yaw = NEUTRAL[POSE_YAW] + (YAW_DELTA + 10)
    right_yaw = NEUTRAL[POSE_YAW] - (YAW_DELTA + 10)
    down_pitch = NEUTRAL[POSE_PITCH] - (PITCH_DOWN_DELTA + 6)

    def run_pose(kind, do_move, timeout=5.0, frames_before=4, move_frames=6, extra_neutral=0):
        clk = FakeClock()
        ch = LivenessChallenge(timeout_s=timeout, clock=clk)
        ch.issue(kind)
        # baseline: a few neutral frames
        for _ in range(frames_before):
            ch.feed(OPEN, pose()); clk.tick(0.1)
        seq = do_move() if do_move else [pose()] * (move_frames + extra_neutral)
        for pz in seq:
            ch.feed(OPEN, pz); clk.tick(0.1)
        return ch

    ch = run_pose(Challenge.TURN_LEFT, lambda: [pose(yaw=left_yaw)] * 6)
    check("TURN_LEFT with left turn -> PASSED", ch.state == ChallengeState.PASSED)
    ch = run_pose(Challenge.TURN_RIGHT, lambda: [pose(yaw=right_yaw)] * 6)
    check("TURN_RIGHT with right turn -> PASSED", ch.state == ChallengeState.PASSED)
    ch = run_pose(Challenge.NOD, lambda: [pose(pitch=down_pitch)] * 6)
    check("NOD with chin down -> PASSED", ch.state == ChallengeState.PASSED)

    # wrong direction must NOT pass: TURN_LEFT fed a right turn -> timeout FAILED
    clk = FakeClock()
    ch = LivenessChallenge(timeout_s=2.0, clock=clk)
    ch.issue(Challenge.TURN_LEFT)
    for _ in range(4):
        ch.feed(OPEN, pose()); clk.tick(0.1)
    for _ in range(40):
        ch.feed(OPEN, pose(yaw=right_yaw)); clk.tick(0.1)
    check("TURN_LEFT fed a RIGHT turn -> FAILED (direction enforced)",
          ch.state == ChallengeState.FAILED)

    # no movement -> timeout FAILED
    clk = FakeClock()
    ch = LivenessChallenge(timeout_s=2.0, clock=clk)
    ch.issue(Challenge.NOD)
    for _ in range(40):
        ch.feed(OPEN, pose()); clk.tick(0.1)
    check("NOD with no movement -> FAILED", ch.state == ChallengeState.FAILED)

    print("\nEngine wiring:")
    ch = LivenessChallenge(clock=FakeClock())
    check("starts IDLE", ch.state == ChallengeState.IDLE)
    rng = random.Random(0)
    ch = LivenessChallenge(rng=rng, clock=FakeClock())
    ch.issue()
    check("issue -> AWAITING with a kind + prompt", ch.state == ChallengeState.AWAITING and ch.prompt)
    # BLINK via engine
    clk = FakeClock()
    ch = LivenessChallenge(timeout_s=4.0, clock=clk)
    ch.issue(Challenge.BLINK)
    for lm in [OPEN] * 3 + [CLOSED] * 2 + [OPEN]:
        ch.feed(lm, None); clk.tick(0.1)
    check("engine BLINK resolves via blink", ch.state == ChallengeState.PASSED)


    print("\nAnti-screen classifier (hf-only gate; lap/peak are audit telemetry):")
    live_like = ScreenFeatures(hf=0.189, peak=7.0, lap=213.0)     # worst-session live means
    screen_like = ScreenFeatures(hf=0.134, peak=10.0, lap=252.0)  # screen means
    check("live-like NOT flagged", is_screen_features(live_like) is False)
    check("screen-like flagged", is_screen_features(screen_like) is True)
    check("hf just below thr -> flagged",
          is_screen_features(ScreenFeatures(HF_THRESH - 1e-3, 7.0, 150.0)) is True)
    check("hf just above thr -> not flagged",
          is_screen_features(ScreenFeatures(HF_THRESH + 1e-3, 7.0, 150.0)) is False)
    # lap is telemetry ONLY: a high lap must NOT flag when hf is fine (drift-robustness)
    check("high lap does NOT flag when hf ok (lap out of gate)",
          is_screen_features(ScreenFeatures(0.22, 20.0, 999.0)) is False)
    # smoke test: screen_features runs and returns finite numbers on a real-ish crop
    g = (np.random.RandomState(0).rand(128, 128) * 255).astype(np.float32)
    f = screen_features(g)
    check("screen_features returns finite hf/peak/lap",
          all(np.isfinite(v) for v in f))

    print("\nStage 9 (R4) GestureSequence: two movements, still start, checked order:")
    from face_service.liveness import (GestureSequence, STILLNESS_WINDOW_S, STILLNESS_MAX_DEG,
                                       ROUND_CAP_S, GESTURE_SEQUENCE_LEN, random_sequence,
                                       GESTURE_KINDS)
    check("ROUND_CAP_S = 0.4 + 2 x 5 + 2 = 12.4",
          abs(ROUND_CAP_S - 12.4) < 1e-9 and GESTURE_SEQUENCE_LEN == 2
          and STILLNESS_WINDOW_S == 0.4 and STILLNESS_MAX_DEG == 4.0)
    for bad in ((Challenge.BLINK, Challenge.NOD), (Challenge.NOD, Challenge.NOD), (Challenge.NOD,)):
        try:
            GestureSequence(bad)
            check(f"sequence {[k.name for k in bad]} refused", False)
        except ValueError:
            check(f"sequence {[k.name for k in bad]} refused", True)
    draws = {random_sequence() for _ in range(60)}
    check("random_sequence: two different kinds, never blink, all six orders appear",
          all(len(d) == 2 and d[0] != d[1] and set(d) <= set(GESTURE_KINDS) for d in draws)
          and len(draws) == 6)

    def run_seq(kinds, poses, step=0.1, left_sign=None):
        clk = FakeClock()
        seq = GestureSequence(kinds, clock=clk, left_sign=left_sign)
        for p in poses:
            seq.feed(OPEN, p)
            clk.tick(step)
            if seq.done:
                break
        for _ in range(200):
            if seq.done:
                break
            seq.tick()
            clk.tick(step)
        return seq

    still = [pose()] * 6
    ok = run_seq((Challenge.TURN_LEFT, Challenge.NOD),
                 still + [pose(yaw=left_yaw)] * 4 + [pose(yaw=left_yaw, pitch=down_pitch)] * 4)
    check("left then nod, in order -> PASSED", ok.passed and ok.steps_done == 2)
    one = run_seq((Challenge.TURN_LEFT, Challenge.NOD), still + [pose(yaw=left_yaw)] * 4)
    check("only the first movement -> FAILED on the second step's timeout",
          not one.passed and one.steps_done == 1 and one.reason in ("gesture-timeout", "round-timeout"))
    rev = run_seq((Challenge.TURN_LEFT, Challenge.NOD), still + [pose(pitch=down_pitch)] * 4)
    check("the second movement first -> gesture-order", rev.reason == "gesture-order")
    mv = run_seq((Challenge.NOD, Challenge.TURN_RIGHT),
                 [pose(), pose(yaw=NEUTRAL[POSE_YAW] + STILLNESS_MAX_DEG + 1.0)] + still)
    check("moving inside the first 0.4 s -> motion-before-prompt", mv.reason == "motion-before-prompt")
    late = run_seq((Challenge.NOD, Challenge.TURN_RIGHT),
                   still + [pose(yaw=NEUTRAL[POSE_YAW] + STILLNESS_MAX_DEG + 1.0)] * 2
                   + [pose(pitch=down_pitch)] * 4 + [pose(pitch=down_pitch, yaw=right_yaw)] * 4)
    check("small drift AFTER the still window is fine", late.passed)
    slow = run_seq((Challenge.TURN_LEFT, Challenge.NOD), still + [pose()] * 200, step=0.1)
    check("nothing happens -> FAILED by a timeout", not slow.passed and slow.reason in
          ("gesture-timeout", "round-timeout"))
    mirror_yaw = NEUTRAL[POSE_YAW] - (YAW_DELTA + 10)
    mirror = still + [pose(yaw=mirror_yaw)] * 4 + [pose(yaw=mirror_yaw, pitch=down_pitch)] * 4
    check("mirrored camera: a left turn read as negative yaw fails without calibration",
          not run_seq((Challenge.TURN_LEFT, Challenge.NOD), mirror).passed)
    check("... and passes with the calibrated sign (R6, B14 N-14)",
          run_seq((Challenge.TURN_LEFT, Challenge.NOD), mirror, left_sign=-1.0).passed)
    clk = FakeClock()
    seq = GestureSequence((Challenge.TURN_LEFT, Challenge.NOD), clock=clk)
    clk.tick(30.0)                      # a slow camera: nothing started yet
    check("the clocks start at the first frame (F-140): nothing expires before it",
          not seq.started and seq.tick() == ChallengeState.AWAITING)

    print("\nAll liveness self-tests passed.")


if __name__ == "__main__":
    main()
