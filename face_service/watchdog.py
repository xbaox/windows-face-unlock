"""Watchdog decision + self-expiring pause file (Stage 3 / Step 5).

Shared by ``tools.watchdog`` (the runner) and the service (which drops a pause on a deliberate
``shutdown`` so the watchdog does not resurrect an intentional stop). No pywin32 and no subprocess
here -- only the restart decision and a self-expiring pause marker -- so the policy is unit-testable
without processes (see ``tools.watchdog_selftest``).

The pause is deliberately SELF-EXPIRING: a stale pause can never silence the watchdog forever. An
expired (or unreadable) marker is ignored AND deleted, and any successful service start / watchdog
restart clears it. For a permanent disable, stop the FaceUnlock-Watchdog task itself.
"""
from __future__ import annotations

import json
from pathlib import Path


def should_restart(consecutive_fails: int, threshold: int, paused: bool) -> bool:
    """Restart iff we've seen >= ``threshold`` consecutive ping failures AND no pause is active.

    Pure: the caller resolves ``paused`` via ``is_paused`` (which self-expires stale markers). Kept
    tiny and side-effect-free so the exact restart policy is trivially unit-testable.
    """
    return int(consecutive_fails) >= int(threshold) and not paused


def restart_outcome(alive_after_start: int) -> str:
    """Classify a kill-then-start attempt by whether a service instance SURVIVED the start.

    Pure -> unit-testable. ``alive_after_start`` is the number of pythonw ``-m face_service``
    processes running a few seconds after the task start (long enough for a mutex-loser to exit):

    * ``>= 1`` -> ``"started"``: a pythonw service is up (the ping loop confirms recovery next cycle);
      the normal "hung pythonw -> killed -> fresh one starts" path.
    * ``== 0`` -> ``"unrecoverable"``: NOTHING survived the start -- the single-instance mutex is held
      by a NON-pythonw instance (e.g. a dev ``python -m face_service``, which the kill deliberately
      spares) or the task launch is misconfigured. The watchdog logs a clear warning and BACKS OFF
      instead of tight-looping a no-op kill-start.
    """
    return "started" if int(alive_after_start) >= 1 else "unrecoverable"


def write_pause(path, now: float, ttl_s: float) -> None:
    """Drop a self-expiring pause marker (a deliberate stop) valid until ``now + ttl_s``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"until": float(now) + float(ttl_s), "created": float(now)}),
        encoding="utf-8",
    )


def clear_pause(path) -> None:
    """Remove the pause marker (successful start / after a watchdog restart). No error if absent."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def is_paused(path, now: float) -> bool:
    """True iff a NON-expired pause marker exists.

    An expired marker (``now >= until``) or an unreadable/corrupt one is DELETED and treated as not
    paused -- so a stale pause can never permanently silence the watchdog. A missing file is simply
    not paused.
    """
    p = Path(path)
    if not p.exists():
        return False
    try:
        until = float(json.loads(p.read_text(encoding="utf-8"))["until"])
    except (OSError, ValueError, KeyError, TypeError):
        clear_pause(p)   # corrupt/unreadable -> don't let it silence us
        return False
    if float(now) >= until:
        clear_pause(p)   # expired -> self-heal
        return False
    return True
