#!/usr/bin/env python3
"""Synthetic self-test for face_service.liveness -- no camera, no real waiting.

Constructs (106, 2) landmark arrays that yield a chosen EAR, then drives the state machine
through open/closed sequences and asserts blink counting + challenge-window behavior. The
BlinkWindow clock is injected (a manual counter) so timeouts are deterministic and instant.

Run from repo root:
    python tools\\liveness_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from face_service.liveness import (
    EYE_IDX, compute_ear, BlinkDetector, BlinkWindow, EAR_THRESH,
)

OPEN_EAR = 0.25    # matches measured open ~0.249
CLOSED_EAR = 0.07  # matches measured closed ~0.073


def synthetic_landmark(ear: float) -> np.ndarray:
    """Build a (106, 2) landmark array whose both eyes have the given EAR.

    Construction: outer corner at (0,0), inner at (W,0) -> width = W. Two vertical lid pairs
    at x=W/3 and x=2W/3, each +-h/2 -> each pair spans h. Then EAR = (h + h)/(2W) = h/W, so
    setting h = ear*W gives the target EAR exactly.
    """
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


class FakeClock:
    """Manually advanced monotonic clock for deterministic window timeouts."""
    def __init__(self):
        self.t = 0.0
    def __call__(self) -> float:
        return self.t
    def tick(self, dt: float = 1.0):
        self.t += dt


def check(name: str, cond: bool):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise AssertionError(name)


def main():
    print("EAR sanity:")
    e_open = compute_ear(OPEN)
    e_closed = compute_ear(CLOSED)
    print(f"  open EAR   = {e_open:.3f} (target {OPEN_EAR})")
    print(f"  closed EAR = {e_closed:.3f} (target {CLOSED_EAR})")
    check("open EAR reproduces target", abs(e_open - OPEN_EAR) < 1e-3)
    check("closed EAR reproduces target", abs(e_closed - CLOSED_EAR) < 1e-3)
    check("threshold separates open/closed", e_closed < EAR_THRESH < e_open)

    print("\nBlinkDetector state machine:")

    d = BlinkDetector()
    for _ in range(30):
        d.update(OPEN)
    check("steady open -> 0 blinks", d.blinks == 0)

    d.reset()
    seq = [OPEN] * 5 + [CLOSED] * 3 + [OPEN] * 5
    for lm in seq:
        d.update(lm)
    check("open/closed(3)/open -> 1 blink", d.blinks == 1)

    d.reset()
    seq = [OPEN] * 5 + [CLOSED] * 1 + [OPEN] * 5   # single-frame dip = noise
    for lm in seq:
        d.update(lm)
    check("single-frame dip filtered -> 0 blinks", d.blinks == 0)

    d.reset()
    seq = ([OPEN] * 3 + [CLOSED] * 2) * 2 + [OPEN] * 3   # two blinks
    for lm in seq:
        d.update(lm)
    check("two blinks -> 2", d.blinks == 2)

    print("\nBlinkWindow challenge (injected clock):")

    clk = FakeClock()
    d.reset()
    w = BlinkWindow(d, timeout_s=4.0, clock=clk)
    # a few open frames (time passing), then a blink before deadline
    for _ in range(3):
        w.feed(OPEN); clk.tick(0.1)
    w.feed(CLOSED); clk.tick(0.1)
    w.feed(CLOSED); clk.tick(0.1)
    resolved, passed = w.feed(OPEN)   # rising edge -> blink -> pass
    check("blink before deadline -> resolved+passed", resolved and passed)

    clk = FakeClock()
    d.reset()
    w = BlinkWindow(d, timeout_s=2.0, clock=clk)
    resolved = passed = None
    for _ in range(30):
        resolved, passed = w.feed(OPEN)   # never blink
        clk.tick(0.2)                     # 30 * 0.2 = 6s >> 2s deadline
    check("no blink past deadline -> resolved+failed", resolved and not passed)

    # post-resolution feeds stay frozen
    r2, p2 = w.feed(CLOSED)
    check("post-resolution feed is inert", r2 and not p2)

    print("\nAll liveness self-tests passed.")


if __name__ == "__main__":
    main()
