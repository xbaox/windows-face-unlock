"""Watchdog decision + self-expiring pause file (Stage 3 / Step 5).

Shared by ``tools.watchdog`` (the runner) and the service (which drops a pause on a deliberate
``shutdown`` so the watchdog does not resurrect an intentional stop). No pywin32 and no subprocess
here -- only the restart decision and a self-expiring pause marker -- so the policy is unit-testable
without processes (see ``tools.watchdog_selftest``).

The pause is deliberately SELF-EXPIRING: a stale pause can never silence the watchdog forever. An
expired (or unreadable) marker is ignored AND deleted, and a successful service start clears it
(Stage 9, F-250: the runner no longer clears it after a restart of its own). For a permanent disable, stop the FaceUnlock-Watchdog task itself.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Stage 8b (F-29): the longest pause any legitimate writer can ask for -- the upper bound validate()
# puts on watchdog_pause_ttl_s. A reader that knows the configured TTL passes it instead.
MAX_PAUSE_TTL_S = 3600.0

# Stage 9 (act 9b R11): after a restart the runner waits this long for a pong; none -> the restart
# failed. Restarts are spaced by RESTART_BACKOFF_BASE_S x 2^n (n = restarts since the service was
# last healthy for HEALTHY_RESET_S), capped at RESTART_BACKOFF_CAP_S.
POST_RESTART_PONG_S = 30.0
RESTART_BACKOFF_BASE_S = 60.0
RESTART_BACKOFF_CAP_S = 1800.0
HEALTHY_RESET_S = 600.0


def should_restart(consecutive_fails: int, threshold: int, paused: bool) -> bool:
    """Restart iff we've seen >= ``threshold`` consecutive ping failures AND no pause is active.

    Pure: the caller resolves ``paused`` via ``is_paused`` (which self-expires stale markers). Kept
    tiny and side-effect-free so the exact restart policy is trivially unit-testable.
    """
    return int(consecutive_fails) >= int(threshold) and not paused


def restart_outcome(pong_after_start) -> str:
    """Classify a kill-then-start attempt. Pure -> unit-testable.

    Stage 9 (act 9b R11, F-248; D-53). The argument is whether the restarted service ANSWERED A
    PING within POST_RESTART_PONG_S (truthy) or not. It used to be a process count taken 5 s after
    the task start -- before the new instance had even bound its pipe, so "a process exists" was
    read as recovery while the service was still warming up (or already wedged).

    * truthy -> ``"started"``: the service answers again.
    * falsy  -> ``"unrecoverable"``: no pong in time -- the start failed, the instance is wedged
      in its warmup, or something else holds the single-instance mutex. The runner backs off
      (restart_backoff_s) instead of tight-looping kill-start.
    """
    return "started" if int(bool(pong_after_start)) >= 1 else "unrecoverable"


def restart_backoff_s(n: int) -> float:
    """The pause before restart number ``n + 1`` of an unhealthy run (n >= 0): 60 s x 2^n,
    capped at 30 min (R11). n resets once the service has been healthy for HEALTHY_RESET_S."""
    n = max(0, int(n))
    if n >= 16:
        return RESTART_BACKOFF_CAP_S
    return min(RESTART_BACKOFF_BASE_S * (2 ** n), RESTART_BACKOFF_CAP_S)


def _write_atomic(p: Path, text: str) -> None:
    """Stage 9 (F-254): temp + replace, so a reader never sees a truncated marker."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


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
    _write_atomic(p, json.dumps({"until": now + ttl_s, "created": now}))


def clear_pause(path) -> None:
    """Remove the pause marker (a successful service start). Never raises.
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
            _write_atomic(p, json.dumps({"until": limit, "created": created}))
        return True
    except Exception as e:
        log.warning("watchdog pause marker %s unusable (%r) -- ignored and removed", p, e)
        clear_pause(p)   # corrupt/unreadable -> don't let it silence us
        return False
