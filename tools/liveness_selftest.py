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
    POSE_PITCH, POSE_YAW, YAW_DELTA, PITCH_DOWN_DELTA, LEFT_IS_NEGATIVE_YAW,
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
    left_yaw = NEUTRAL[POSE_YAW] + (-1 if LEFT_IS_NEGATIVE_YAW else 1) * (YAW_DELTA + 10)
    right_yaw = NEUTRAL[POSE_YAW] + (1 if LEFT_IS_NEGATIVE_YAW else -1) * (YAW_DELTA + 10)
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

    print("\nAll liveness self-tests passed.")


if __name__ == "__main__":
    main()
