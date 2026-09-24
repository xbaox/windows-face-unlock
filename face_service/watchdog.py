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
import logging
import math
from pathlib import Path

log = logging.getLogger(__name__)

# Stage 8b (F-29): the longest pause any legitimate writer can ask for -- the upper bound validate()
# puts on watchdog_pause_ttl_s. A reader that knows the configured TTL passes it instead.
MAX_PAUSE_TTL_S = 3600.0


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
    """Drop a self-expiring pause marker (a deliberate stop) valid until ``now + ttl_s``.
    Stage 8b (F-29): a non-finite ``now`` or ``ttl_s`` is refused (ValueError) and the TTL is
    capped at MAX_PAUSE_TTL_S, so no writer can produce an endless pause."""
    now, ttl_s = float(now), float(ttl_s)
    if not (math.isfinite(now) and math.isfinite(ttl_s)) or ttl_s < 0:
        raise ValueError(f"invalid pause (now={now!r}, ttl_s={ttl_s!r})")
    ttl_s = min(ttl_s, MAX_PAUSE_TTL_S)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"until": now + ttl_s, "created": now}), encoding="utf-8")


def clear_pause(path) -> None:
    """Remove the pause marker (successful start / after a watchdog restart). Never raises.
    Stage 8b (F-29): it used to swallow only FileNotFoundError, so a marker it could not delete
    (sharing violation, access denied) raised out of here -- and out of the watchdog loop, which
    then ended with nothing to restart it."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("watchdog pause marker %s could not be removed: %r", path, e)


def is_paused(path, now: float, ttl_s: float = MAX_PAUSE_TTL_S) -> bool:
    """True iff a NON-expired pause marker exists. Never raises.

    An expired marker (``now >= until``) or an unreadable/corrupt one is DELETED and treated as not
    paused -- so a stale pause can never permanently silence the watchdog. A missing file is simply
    not paused.

    Stage 8b (F-29). Defect: ``until`` was trusted as written -- NaN, inf or a date years ahead
    all paused supervision for good, and the only error handling covered a few exception types.
    Consequence: one bad marker could switch the watchdog off indefinitely. Fix: ``until`` must be
    finite, and it is clamped to ``now + ttl_s`` (the reader's configured TTL); the clamp is
    written back, so it holds from the first observation, and a clamp that cannot be persisted
    counts as no pause. Any failure at all -> not paused.
    """
    p = Path(path)
    try:
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        until = float(data["until"])
        if not math.isfinite(until):
            raise ValueError(f"non-finite until {until!r}")
        now = float(now)
        if now >= until:
            clear_pause(p)   # expired -> self-heal
            return False
        limit = now + min(float(ttl_s), MAX_PAUSE_TTL_S)
        if until > limit:
            log.warning("watchdog pause ran past its TTL (until in %.0fs > %.0fs); clamped",
                        until - now, limit - now)
            created = data.get("created", now) if isinstance(data, dict) else now
            p.write_text(json.dumps({"until": limit, "created": created}), encoding="utf-8")
        return True
    except Exception as e:
        log.warning("watchdog pause marker %s unusable (%r) -- ignored and removed", p, e)
        clear_pause(p)   # corrupt/unreadable -> don't let it silence us
        return False
