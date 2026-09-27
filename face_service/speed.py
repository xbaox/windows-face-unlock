"""How fast this PC runs the sign-in screen's frame pipeline (9e, F2-05).

The live 9e run measured the lock screen in a VM: at 1.1-1.6 frames/s (4 vCPU) phase 2 ran out of
time on every try (gesture-timeout x2, gesture-order); at 2.3-2.5 frames/s (8 vCPU) the user was
signed in 8.4 s after the request. The liveness constants stay as they are (architect's decision
in 9e): a slow PC gets an HONEST warning instead -- in the wizard's readiness panel and in the
tray's Status window, from this estimate.

Sources, in this order:
  * "attempts" -- the frame rate of the last sign-in rounds that saw a face (phase 1 burst,
    phase 2 gesture round), the real pipeline on the real camera;
  * "enroll" -- the seconds per photo of the last face-profile build (the same detection +
    recognition + landmarks per image, det_size as configured): 1 / s_per_image.
Numbers only; nothing here decides a sign-in. Pure, camera-free (tools/live9e_selftest.py).
"""
from __future__ import annotations

from statistics import median

FPS_QUICK = 4.0      # at or above: face sign-in is quick
FPS_MIN = 2.0        # below: sign-in with a head movement runs out of time (9e: 1.1-1.6 failed)
RECENT = 5           # the last rounds that count


def _positive(v) -> "float | None":
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def estimate(attempt_fps, enroll_s_per_image) -> dict:
    """{"fps": float | None, "source": "attempts" | "enroll" | None, "samples": int}."""
    vals = [f for f in (_positive(v) for v in (attempt_fps or [])) if f is not None][-RECENT:]
    if vals:
        return {"fps": round(float(median(vals)), 1), "source": "attempts", "samples": len(vals)}
    s = _positive(enroll_s_per_image)
    if s is not None:
        return {"fps": round(1.0 / s, 1), "source": "enroll", "samples": 1}
    return {"fps": None, "source": None, "samples": 0}


def level(fps) -> str:
    """"quick" | "slow" | "too-slow" | "unknown" -- on the rounded value that is shown."""
    f = _positive(fps)
    if f is None:
        return "unknown"
    f = round(f, 1)
    if f >= FPS_QUICK:
        return "quick"
    if f >= FPS_MIN:
        return "slow"
    return "too-slow"


def from_records(records) -> "tuple[list[float], float | None]":
    """The frame rates and the last build's seconds per photo in audit records (oldest first):
    ``unlock`` / ``gesture_telemetry`` rounds that saw a face, ``enroll_build`` with s_per_image."""
    fps: "list[float]" = []
    enroll = None
    for r in records or []:
        if not isinstance(r, dict):
            continue
        ev = r.get("event")
        if ev in ("unlock", "gesture_telemetry"):
            try:
                faces = int(r.get("faces") or 0)
            except (TypeError, ValueError):
                faces = 0
            f = _positive(r.get("fps"))
            if faces > 0 and f is not None:
                fps.append(f)
        elif ev == "enroll_build" and r.get("ok"):
            s = _positive(r.get("s_per_image"))
            if s is not None:
                enroll = s
    return fps[-RECENT:], enroll


def ready_text(speed: "dict | None") -> "tuple[str, str]":
    """The readiness-panel line and its level ("ok" | "warn" | "err" | "info")."""
    from .i18n import t
    fps = (speed or {}).get("fps")
    lv = level(fps)
    if lv == "unknown":
        return t("enroll.ready.speed.unknown"), "info"
    key, colour = {"quick": ("enroll.ready.speed.quick", "ok"),
                   "slow": ("enroll.ready.speed.slow", "warn"),
                   "too-slow": ("enroll.ready.speed.too_slow", "err")}[lv]
    return t(key, fps=f"{float(fps):.1f}"), colour


def status_text(speed: "dict | None") -> str:
    """The tray Status row."""
    from .i18n import t
    fps = (speed or {}).get("fps")
    lv = level(fps)
    if lv == "unknown":
        return t("status.val.speed.unknown")
    src = t("status.val.speed.src.attempts" if (speed or {}).get("source") == "attempts"
            else "status.val.speed.src.enroll")
    verdict = t({"quick": "status.val.speed.quick", "slow": "status.val.speed.slow",
                 "too-slow": "status.val.speed.too_slow"}[lv])
    return t("status.val.speed", fps=f"{float(fps):.1f}", source=src, verdict=verdict)
