"""Stage 2, Step 6 synthetic tests for the face-auth Lockout state machine.

No camera, no service -- a temp state file + an injectable fake clock, so the whole
lock/cooldown/persist/reset flow is verified in milliseconds.

Run:  python -m tools.lockout_selftest      (or: python tools/lockout_selftest.py)
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from face_service.lockout import Lockout
except ImportError:
    from lockout import Lockout  # type: ignore


_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_lock_after_max() -> None:
    print("lockout -- locks after max consecutive failures")
    with tempfile.TemporaryDirectory() as d:
        clk = FakeClock()
        lo = Lockout(Path(d) / "lockout.json", max_attempts=3, lockout_seconds=300, clock=clk)
        check("fresh not locked", lo.locked(), False)
        check("fail 1 -> not locked", lo.record(False), False)
        check("fail 2 -> not locked", lo.record(False), False)
        check("fail 3 -> LOCKED", lo.record(False), True)
        check("remaining ~= 300", lo.remaining(), 300.0)
        check("status locked", lo.status()["locked"], True)


def test_success_resets() -> None:
    print("lockout -- a success clears the counter")
    with tempfile.TemporaryDirectory() as d:
        clk = FakeClock()
        lo = Lockout(Path(d) / "lockout.json", max_attempts=3, lockout_seconds=300, clock=clk)
        lo.record(False)
        lo.record(False)
        check("2 fails, not locked yet", lo.locked(), False)
        check("success returns False", lo.record(True), False)
        check("counter reset -> fails=0", lo.status()["fails"], 0)
        # after reset it takes a full max run again
        check("fail 1 again", lo.record(False), False)
        check("fail 2 again", lo.record(False), False)
        check("fail 3 again -> LOCKED", lo.record(False), True)


def test_cooldown_expires() -> None:
    print("lockout -- unlocks after cooldown; failures during lock don't stack")
    with tempfile.TemporaryDirectory() as d:
        clk = FakeClock()
        lo = Lockout(Path(d) / "lockout.json", max_attempts=2, lockout_seconds=300, clock=clk)
        lo.record(False)
        check("2nd fail -> LOCKED", lo.record(False), True)
        clk.advance(100)
        check("still locked at t+100", lo.locked(), True)
        locked_until_before = lo._locked_until  # noqa: SLF001 (test peek)
        lo.record(False)                          # failing while locked must NOT extend window
        check("window not extended", lo._locked_until, locked_until_before)  # noqa: SLF001
        clk.advance(201)                          # total 301 > 300
        check("unlocked after cooldown", lo.locked(), False)
        check("remaining 0 after cooldown", lo.remaining(), 0.0)


def test_persistence() -> None:
    print("lockout -- state survives a restart (new instance, same file)")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "lockout.json"
        clk = FakeClock()
        lo1 = Lockout(path, max_attempts=2, lockout_seconds=300, clock=clk)
        lo1.record(False)
        check("locked on instance 1", lo1.record(False), True)
        # simulate service restart: brand-new object, same file + same clock time
        lo2 = Lockout(path, max_attempts=2, lockout_seconds=300, clock=clk)
        check("still locked on instance 2", lo2.locked(), True)
        check("remaining carried over ~300", round(lo2.remaining()), 300)
        # counter also persists
        lo3a = Lockout(path, max_attempts=5, lockout_seconds=300, clock=FakeClock(5000.0))
        lo3a.record(False)  # fails=1 (past any lock)
        lo3b = Lockout(path, max_attempts=5, lockout_seconds=300, clock=FakeClock(5000.0))
        check("fail counter persisted", lo3b.status()["fails"], 1)


def test_reset_and_reconfigure() -> None:
    print("lockout -- reset() clears; reconfigure() changes thresholds")
    with tempfile.TemporaryDirectory() as d:
        clk = FakeClock()
        lo = Lockout(Path(d) / "lockout.json", max_attempts=2, lockout_seconds=300, clock=clk)
        lo.record(False)
        lo.record(False)
        check("locked", lo.locked(), True)
        lo.reset()
        check("reset -> not locked", lo.locked(), False)
        check("reset -> fails 0", lo.status()["fails"], 0)
        lo.reconfigure(max_attempts=1, lockout_seconds=60)
        check("reconfigure applied max_attempts", lo.status()["max_attempts"], 1)
        check("single fail now locks (max=1)", lo.record(False), True)
        check("new cooldown ~60", round(lo.remaining()), 60)


def main() -> int:
    for t in (
        test_lock_after_max,
        test_success_resets,
        test_cooldown_expires,
        test_persistence,
        test_reset_and_reconfigure,
    ):
        t()
        print()
    print(f"{'FAILED' if _fails else 'OK'}: all checks "
          f"({len(_fails)} failing{': ' + ', '.join(_fails) if _fails else ''})")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
