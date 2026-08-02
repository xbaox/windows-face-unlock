"""Block-7 A3 synthetic tests for the lock-screen-aware lockout toast gate.

The bug this guards: a lockout episode ALWAYS begins while the workstation is locked
(it is raised by the SYSTEM/LogonUI `unlock` path), but a Shell_NotifyIcon balloon is
invisible on the lock screen and the OS does not queue it. The old code fired the toast
anyway and latched its dedup flag, so the user never saw it and it was never re-raised.

No camera, no service, no tray, no real desktop probe: the pipe response is crafted by
hand, `session_locked` is stubbed, and `_notify` is spied on -- so the whole
defer/re-raise sequence is verified in milliseconds.

Run:  python -m tools.lockout_notify_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service.config import Config
from presence_monitor import monitor as M


_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


class Harness:
    """Drives PresenceMonitor._check_service_events with a crafted status response.

    Patches the module-level `pipe_call` and `session_locked` that _check_service_events
    resolves at call time, and replaces the bound `_notify` with a spy. Nothing else in
    the monitor is exercised, so no tray, no Tk and no pipe are needed.
    """

    def __init__(self) -> None:
        self.mon = M.PresenceMonitor(Config())
        self.notifications: list[tuple[str, str]] = []
        self.locked = False          # face-lockout episode active?
        self.remaining_s = 0         # what the service reports right now
        self.desktop_locked = False  # workstation lock screen up?
        self.session_locked_calls = 0

        self.mon._notify = lambda gate, message: self.notifications.append((gate, message))
        M.pipe_call = lambda req, timeout_s=30.0: {
            "ok": True,
            "lockout": {"locked": self.locked, "remaining_s": self.remaining_s},
        }
        # 7c-7: the toast gate now asks _is_session_locked (the authoritative WTS detector with the
        # desktop predicate only as a fallback). Both names are patched to the same stub -- without
        # the first one the real detector answers about the developer's own machine and the
        # desktop_locked knob below stops meaning anything.
        M._is_session_locked = self._session_locked
        M.session_locked = self._session_locked

    def _session_locked(self) -> bool:
        self.session_locked_calls += 1
        return self.desktop_locked

    def tick(self) -> None:
        self.mon._check_service_events()

    @property
    def lockout_toasts(self) -> list[str]:
        return [msg for gate, msg in self.notifications if gate == "notify_lockout"]


def test_deferred_while_locked_then_replayed() -> None:
    print("lockout toast -- deferred on the lock screen, replayed after unlock")
    h = Harness()

    # Baseline tick so the service-reachability transition is consumed silently
    # (it fires on the FIRST change, which would otherwise pollute the spy).
    h.tick()
    h.notifications.clear()

    # 1. Episode starts while the workstation is locked -> nothing must be raised,
    #    and the dedup flag must NOT latch, or the toast would be lost forever.
    h.locked, h.remaining_s, h.desktop_locked = True, 300, True
    h.tick()
    check("locked screen: no toast", len(h.lockout_toasts), 0)
    check("locked screen: flag NOT latched", h.mon._lockout_notified, False)

    h.tick()
    check("locked screen, 2nd poll: still no toast", len(h.lockout_toasts), 0)
    check("locked screen, 2nd poll: flag still clear", h.mon._lockout_notified, False)

    # 2. User unlocks. The episode is still live, so the very next poll raises it --
    #    with the remaining_s of THAT poll, not the stale value from step 1.
    h.desktop_locked, h.remaining_s = False, 218
    h.tick()
    check("after unlock: exactly one toast", len(h.lockout_toasts), 1)
    check("after unlock: flag latched", h.mon._lockout_notified, True)
    check("toast carries FRESH remaining_s", "218" in h.lockout_toasts[0], True)
    check("toast does NOT carry stale remaining_s", "300" in h.lockout_toasts[0], False)

    # 3. Episode still live on later polls -> no duplicate.
    h.remaining_s = 120
    h.tick()
    h.tick()
    check("still locked out: no duplicate toast", len(h.lockout_toasts), 1)

    # 4. Episode expires -> flag re-arms for the next episode.
    h.locked, h.remaining_s = False, 0
    h.tick()
    check("episode over: flag re-armed", h.mon._lockout_notified, False)
    check("episode over: no extra toast", len(h.lockout_toasts), 1)

    # 5. A NEW episode, this time starting while the session is already unlocked
    #    (e.g. the user locked out from a tool rather than the lock screen).
    h.locked, h.remaining_s = True, 300
    h.tick()
    check("new episode while unlocked: toast fires", len(h.lockout_toasts), 2)
    check("new episode: flag latched", h.mon._lockout_notified, True)


def test_probe_is_not_called_when_already_notified() -> None:
    """The desktop probe must sit behind the dedup flag, not in front of it: once the
    episode has been announced there is nothing to decide, so we must not probe on
    every single poll for the rest of the episode."""
    print("lockout toast -- session_locked() is only consulted when it can change the outcome")
    h = Harness()
    h.tick()

    h.locked, h.remaining_s, h.desktop_locked = True, 300, False
    h.tick()
    check("probed once to decide", h.session_locked_calls, 1)
    check("toast fired", len(h.lockout_toasts), 1)

    before = h.session_locked_calls
    h.tick()
    h.tick()
    check("no further probes once latched", h.session_locked_calls - before, 0)

    # And when the episode is over, the not-locked branch short-circuits too.
    h.locked = False
    before = h.session_locked_calls
    h.tick()
    check("no probe when no episode", h.session_locked_calls - before, 0)


def test_service_state_notification_untouched() -> None:
    """The gate must apply to the lockout toast ONLY -- the service up/down toast keeps
    firing regardless of the lock screen (it is a separate concern and a separate gate)."""
    print("lockout toast -- the service-state notification is not affected by the gate")
    h = Harness()
    h.desktop_locked = True
    h.tick()  # first tick sets the reachability baseline silently

    gates_before = [g for g, _ in h.notifications]
    check("baseline tick raises no service-state toast", gates_before, [])

    # Service goes away while the screen is locked -> that transition still notifies.
    M.pipe_call = lambda req, timeout_s=30.0: None
    h.tick()
    check("service-down toast still fires while locked",
          [g for g, _ in h.notifications], ["notify_service_state"])


def main() -> int:
    print("=== Block-7 A3: lock-screen-aware lockout toast ===")
    test_deferred_while_locked_then_replayed()
    test_probe_is_not_called_when_already_notified()
    test_service_state_notification_untouched()
    print()
    if _fails:
        print(f"LOCKOUT-NOTIFY SELFTEST FAILED: {len(_fails)} check(s): {', '.join(_fails)}")
        return 1
    print("LOCKOUT-NOTIFY SELFTEST OK: the toast is withheld (and not latched) while the "
          "workstation is locked, replayed with a fresh remaining_s on the first poll after "
          "unlock, never duplicated, re-armed when the episode ends, and the service-state "
          "notification is untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
