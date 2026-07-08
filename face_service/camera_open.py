"""Bounded camera-open retry (Stage 3 / Step 4).

Pure helper that turns a possibly-failing camera open into a bounded, NON-HANGING busy check: it
retries an ``open_fn`` a few times within a wall-clock budget and reports success/busy without ever
raising or blocking indefinitely. Kept camera- and service-free so the retry/timeout logic is unit-
testable with a fake ``open_fn`` + injected clock/sleep (see ``tools.camera_busy_selftest``).
"""
from __future__ import annotations

from typing import Callable


def open_with_retry(open_fn: Callable[[], bool], *, retries, pause_s, timeout_s,
                    clock, sleep) -> bool:
    """Try ``open_fn()`` until it succeeds or the budget is spent. Returns True on success, else
    False (camera busy).

    Attempts = 1 initial try + ``retries`` extra (so ``retries`` is the number of RE-tries; 0 means
    a single attempt). An attempt succeeds when ``open_fn()`` returns truthy without raising; a
    raised exception OR a falsy return counts as a failed attempt (never propagated). Between
    attempts it pauses ``pause_s`` -- but only while attempts remain AND the elapsed
    ``clock() - start`` is still under ``timeout_s`` (once the budget is spent it stops early, so
    it can never hang). ``clock``/``sleep`` are injected, so tests drive it without real time.
    """
    attempts = 1 + max(0, int(retries))
    start = clock()
    for i in range(attempts):
        try:
            if open_fn():
                return True
        except Exception:
            pass
        if i + 1 >= attempts:
            break
        if clock() - start >= timeout_s:
            break
        sleep(pause_s)
    return False
