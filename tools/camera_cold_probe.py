#!/usr/bin/env python3
"""tools/camera_cold_probe.py -- Stage 6 / block6-B: COLD open -> first-frame latency.

Measures how long the webcam actually takes to hand over its first usable frame,
split into the three segments the service pays on every on-demand acquisition.
It exists because the ~0.9s / ~3s figures in ``field.persistent_camera.desc``
(face_service/i18n.py:179 EN, :1060 RU) are undocumented -- ``git log -L`` shows
they arrived whole in the bulk GUI/i18n merge dcaa766, not from a measurement --
and because ``tools.camera_busy_integration`` only ever reports a WARM re-open
(~0.6s after releasing its own holder, in a process where cv2 and the backend
are already hot).

Segments, mirroring ``Camera.open_fast`` (face_service/camera.py) 1:1:

  construct    cv2.VideoCapture(index, backend) + FRAME_WIDTH/HEIGHT/BUFFERSIZE
               + the non-fatal *_TIMEOUT_MSEC props
  first_read   until the first cap.read() that returns ret=True -- i.e. the
               moment the device actually yields a frame
  warmup       the cfg.camera_warmup_frames discard loop that follows

The camera is opened by LOCAL code rather than by calling ``Camera.open_fast``,
because open_fast collapses all three segments into one call. Backend order,
properties and the read-attempt budget are copied from it verbatim so the number
describes the production path; the timeout-prop helper is IMPORTED from
face_service.camera rather than re-implemented, so the two cannot drift apart.

WHAT THIS IS NOT: this is open-to-first-frame only. A full ``verify`` adds
detection, embedding and liveness on top -- do not compare this number directly
against the ~0.9s / ~3s verify figures without accounting for that.

Cold vs warm: cycle 1 of a fresh process is the only truly cold sample (cv2 not
yet loaded, DSHOW graph not yet built). Later cycles re-open a device this
process just released, so they are reported separately as warm.

Run with the service STOPPED -- otherwise it holds the device and every cycle
measures contention instead of open latency.

    .venv\\Scripts\\python.exe -m tools.camera_cold_probe
    .venv\\Scripts\\python.exe -m tools.camera_cold_probe --runs 8 --backend dshow

Read-only: touches no config, writes no files, prints to stdout. The capture is
released in a finally on every cycle so the probe cannot leave the webcam held.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from time import perf_counter

import cv2

from face_service.camera import _apply_timeout_props
from face_service.config import Config

# Copied from Camera.open_fast so the probe measures the production path.
READ_ATTEMPTS = 4          # camera.py: `for _ in range(4)` before a backend is given up on
FRAME_W, FRAME_H = 640, 480
BACKENDS = (("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY))
BACKEND_BY_NAME = {name.lower(): (name, val) for name, val in BACKENDS}

# Let the driver actually let go between cycles; without it a re-open can be
# measuring the tail of the previous release rather than a fresh acquisition.
INTER_CYCLE_SLEEP_S = 1.0


def _release(cap) -> None:
    """Release a capture without ever raising (swap-then-release, as camera.py)."""
    if cap is None:
        return
    try:
        cap.release()
    except Exception as e:                       # pragma: no cover - defensive
        print(f"  ! release failed: {e}")


def _probe_once(index: int, backends, warmup_frames: int) -> dict:
    """One cold-ish acquisition. Returns a result dict; never leaves a capture open.

    Walks ``backends`` in order and reports the FIRST that yields a frame, which
    is the same selection rule open_fast uses. Backends that fail are reported
    in ``failed`` with the wall time they cost, since on a contended device that
    is where the seconds actually go.
    """
    failed: list[tuple[str, float]] = []
    for name, backend in backends:
        cap = None
        try:
            t0 = perf_counter()
            cap = cv2.VideoCapture(index, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            _apply_timeout_props(cap, backend)
            t1 = perf_counter()

            got = False
            for _ in range(READ_ATTEMPTS):
                ret, _frame = cap.read()
                if ret:
                    got = True
                    break
            t2 = perf_counter()

            if not got:
                failed.append((name, (t2 - t0) * 1000.0))
                continue                          # finally releases this candidate

            for _ in range(warmup_frames):
                cap.read()
            t3 = perf_counter()

            return {
                "ok": True,
                "backend": name,
                "construct_ms": (t1 - t0) * 1000.0,
                "first_read_ms": (t2 - t1) * 1000.0,
                "warmup_ms": (t3 - t2) * 1000.0,
                "total_ms": (t3 - t0) * 1000.0,
                "failed": failed,
            }
        finally:
            # Runs on the success return, on continue, and on any exception:
            # the probe must never be the thing that leaves the webcam held.
            _release(cap)
            cap = None

    return {"ok": False, "backend": None, "failed": failed}


def _warn_if_service_running() -> None:
    """Best-effort heads-up that the service still owns the camera. Never fatal."""
    try:
        import win32file  # type: ignore
        from face_service.config import PIPE_NAME
        win32file.WaitNamedPipe(PIPE_NAME, 0)
    except Exception:
        return                                    # not running / cannot tell -> stay quiet
    print("!! WARNING: the FaceUnlock service pipe answers, so the service is probably")
    print("!! running and holding the webcam. Stop it first or these numbers measure")
    print("!! contention, not open latency.\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Cold open -> first-frame latency probe (Stage 6 / block6-B).")
    ap.add_argument("--index", type=int, default=None,
                    help="camera index (default: cfg.camera_index)")
    ap.add_argument("--backend", choices=sorted(BACKEND_BY_NAME),
                    default=None, help="pin one backend (default: DSHOW->MSMF->ANY as open_fast)")
    ap.add_argument("--runs", type=int, default=5, help="number of cycles (default 5)")
    args = ap.parse_args(argv)

    if args.runs < 1:
        print("--runs must be >= 1")
        return 2

    try:
        cfg = Config.load()                       # READ ONLY: never saves
    except Exception as e:
        print(f"config load failed ({e}); falling back to dataclass defaults")
        cfg = Config()

    index = cfg.camera_index if args.index is None else args.index
    warmup_frames = int(cfg.camera_warmup_frames)
    backends = [BACKEND_BY_NAME[args.backend]] if args.backend else list(BACKENDS)

    print("=" * 72)
    print("COLD OPEN -> FIRST FRAME probe")
    print(f"  index={index}  warmup_frames={warmup_frames}  runs={args.runs}")
    print(f"  backend order: {' -> '.join(n for n, _ in backends)}")
    print(f"  props: {FRAME_W}x{FRAME_H}, BUFFERSIZE=1, *_TIMEOUT_MSEC (non-fatal)")
    print("=" * 72)
    _warn_if_service_running()

    results: list[dict] = []
    for i in range(1, args.runs + 1):
        if i > 1:
            time.sleep(INTER_CYCLE_SLEEP_S)
        r = _probe_once(index, backends, warmup_frames)
        r["cycle"] = i
        results.append(r)
        if not r["ok"]:
            tried = ", ".join(f"{n} {ms:.0f}ms" for n, ms in r["failed"]) or "(none)"
            print(f"  cycle {i}: NO BACKEND yielded a frame; tried: {tried}")
        elif r["failed"]:
            tried = ", ".join(f"{n} {ms:.0f}ms" for n, ms in r["failed"])
            print(f"  cycle {i}: note -- fell through {tried} before {r['backend']}")

    good = [r for r in results if r["ok"]]
    if not good:
        print("\nNo cycle acquired a frame. Is the camera present, and is the service stopped?")
        return 1

    print()
    print(f"{'cycle':>5} {'backend':>8} {'construct':>12} {'first_read':>12} "
          f"{'warmup':>10} {'total':>11}   (all ms)")
    print("-" * 62)
    for r in results:
        if not r["ok"]:
            print(f"{r['cycle']:>5} {'-':>8}   FAILED -- no backend yielded a frame")
            continue
        print(f"{r['cycle']:>5} {r['backend']:>8} {r['construct_ms']:>12.1f} "
              f"{r['first_read_ms']:>12.1f} {r['warmup_ms']:>10.1f} {r['total_ms']:>11.1f}")
    print("-" * 62)

    label_w = 32
    cold = results[0]
    if cold["ok"]:
        print(f"{'COLD (cycle 1, fresh process)':<{label_w}}{cold['total_ms']:>9.1f} ms   "
              f"[{cold['backend']}: construct {cold['construct_ms']:.1f} + "
              f"first_read {cold['first_read_ms']:.1f} + warmup {cold['warmup_ms']:.1f}]")
    else:
        print(f"{'COLD (cycle 1, fresh process)':<{label_w}}   FAILED")

    warm = [r for r in good if r["cycle"] > 1]
    if warm:
        print(f"{f'WARM (median of {len(warm)} re-opens)':<{label_w}}"
              f"{statistics.median(r['total_ms'] for r in warm):>9.1f} ms   "
              f"[construct {statistics.median(r['construct_ms'] for r in warm):.1f} + "
              f"first_read {statistics.median(r['first_read_ms'] for r in warm):.1f} + "
              f"warmup {statistics.median(r['warmup_ms'] for r in warm):.1f}]")
    else:
        print(f"{'WARM (median)':<{label_w}}      n/a   [run with --runs 2 or more]")

    print()
    print("NOTE: this is OPEN -> FIRST FRAME only. A full `verify` adds detection,")
    print("      embedding and liveness on top, so do NOT compare this straight to")
    print("      the ~0.9s / ~3s verify figures in field.persistent_camera.desc.")
    print("      Only cycle 1 is genuinely cold; later cycles re-open a device this")
    print("      same process just released.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
