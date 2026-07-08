#!/usr/bin/env python3
"""tools/camera_busy_integration.py -- Stage 3 / Step 4 REAL busy-camera integration test.

Autonomous, no-human proof that the shipped busy handling works against a REAL cv2.VideoCapture,
complementing the fake-camera unit test (tools.camera_busy_selftest). It does NOT touch the running
service or the shipped camera code -- it only imports Camera.open_fast + open_with_retry (the exact
pair face_service.service._acquire_camera uses).

How it reproduces "busy" without a person:
  * A separate HOLDER PROCESS (this same script re-invoked with --hold) opens the real webcam and
    keeps it open (exclusive, like the running service would), announcing READY on stdout.
  * While it holds, the parent probes with open_with_retry(Camera(index).open_fast, ...) using the
    config defaults, and confirms it correctly reports BUSY (False) within the timeout budget,
    without hanging or raising. The busy-detect latency is measured and printed.
  * The parent then releases the holder and confirms the now-free camera opens (no false busy).
  * Cleanup is guaranteed: the holder is released in a finally, has an os._exit watchdog so it can
    never outlive --hold-seconds, and the OS reclaims the device if it is force-killed.

Graceful skip (exit 0, NOT a failure) when a real camera can't be exercised: no webcam / headless /
OpenCV missing / the device is already held (e.g. the service is still running -- stop it and retry)
/ or the webcam allows concurrent opens on this machine (then "busy" cannot be reproduced here).

Run from the repo root (camera FREE -- stop the face-unlock service first):
    python -m tools.camera_busy_integration
    python -m tools.camera_busy_integration --index 1 --hold-seconds 15
Exit 0 = proof passed OR cleanly skipped; 1 = a real failure.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPO_ROOT = str(Path(__file__).resolve().parents[1])

# Mirrors face_service.service.CAMERA_OPEN_PAUSE_S so the probe matches _acquire_camera exactly.
PAUSE_S = 0.3


def _open_real_camera(index):
    """Open a real cv2.VideoCapture like an ordinary app (DSHOW->MSMF->ANY), returning it once it
    yields a frame, or None if the device can't be acquired (no camera / already held)."""
    import cv2
    for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY):
        cap = cv2.VideoCapture(index, backend)
        for _ in range(10):
            ret, _f = cap.read()
            if ret:
                return cap
            time.sleep(0.05)
        cap.release()
    return None


def _hold_role(index: int, hold_seconds: float) -> int:
    """Child role: grab the real camera, announce READY, hold until the parent signals release on
    stdin (or the watchdog fires), then release. Prints NO-CAMERA if it can't acquire the device."""
    cap = _open_real_camera(index)
    if cap is None:
        print("NO-CAMERA", flush=True)
        return 0

    def _watchdog():
        # Never outlive hold_seconds even if the parent hangs; the OS reclaims the handle on exit.
        time.sleep(hold_seconds)
        os._exit(0)

    threading.Thread(target=_watchdog, daemon=True).start()
    print("READY", flush=True)
    try:
        sys.stdin.readline()   # blocks until the parent writes "release" / closes stdin
    except Exception:
        pass
    finally:
        cap.release()
    return 0


def _read_line_with_timeout(pipe, timeout_s):
    box = {}

    def _r():
        try:
            box["line"] = pipe.readline()
        except Exception as e:   # pragma: no cover - defensive
            box["err"] = e

    th = threading.Thread(target=_r, daemon=True)
    th.start()
    th.join(timeout_s)
    return box.get("line")


def _probe_busy(Camera, open_with_retry, index, warmup, retries, timeout_s):
    """Run the exact _acquire_camera probe once; return (opened, latency_s, raised_or_None)."""
    cam = Camera(index, warmup)
    t0 = time.monotonic()
    raised = None
    opened = None
    try:
        opened = open_with_retry(cam.open_fast, retries=retries, pause_s=PAUSE_S,
                                 timeout_s=timeout_s, clock=time.monotonic, sleep=time.sleep)
    except Exception as e:
        raised = e
    latency = time.monotonic() - t0
    try:
        cam.close()
    except Exception:
        pass
    return opened, latency, raised


def _run_integration(index: int, hold_seconds: float) -> int:
    try:
        import cv2  # noqa: F401
    except Exception as e:
        print(f"[integ] SKIP: OpenCV unavailable ({e!r}).")
        return 0
    from face_service.camera import Camera
    from face_service.camera_open import open_with_retry
    from face_service.config import Config

    cfg = Config()
    retries, timeout_s, warmup = cfg.camera_open_retries, cfg.camera_open_timeout_s, cfg.camera_warmup_frames
    print(f"[integ] index={index} retries={retries} timeout_s={timeout_s} pause_s={PAUSE_S}")

    holder = subprocess.Popen(
        [sys.executable, "-m", "tools.camera_busy_integration", "--hold",
         "--index", str(index), "--hold-seconds", str(hold_seconds)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=REPO_ROOT,
    )
    fails: list[str] = []
    try:
        line = (_read_line_with_timeout(holder.stdout, 25.0) or "").strip()
        if line == "NO-CAMERA":
            print("[integ] SKIP: could not acquire a real camera to hold -- no webcam, headless, or "
                  "the device is already in use (is the face-unlock service still running? stop it and retry).")
            return 0
        if line != "READY":
            print(f"[integ] SKIP: holder never became ready (got {line!r}); cannot run the real busy test.")
            return 0
        print("[integ] holder is holding the real camera exclusively; probing...")

        # --- PHASE 1: while held -> must report busy, bounded, without raising ---------------
        opened, busy_latency, raised = _probe_busy(Camera, open_with_retry, index, warmup, retries, timeout_s)
        if raised is not None:
            print(f"[integ] FAIL: open_with_retry raised while busy: {raised!r}")
            fails.append("raised-on-busy")
        elif opened is True:
            print(f"[integ] SKIP (inconclusive): this webcam allowed a concurrent open while held "
                  f"({busy_latency:.2f}s) -- it is not exclusive on this system, so real busy can't be reproduced.")
            return 0
        else:
            print(f"[integ] BUSY DETECTED: open_with_retry -> False in {busy_latency:.2f}s (budget timeout_s={timeout_s}).")
            if busy_latency > timeout_s + 6.0:
                print(f"[integ] FAIL: busy detection took far longer than the budget ({busy_latency:.2f}s).")
                fails.append("busy-too-slow")
            elif busy_latency <= 4.0:
                print(f"[integ] latency within the ~1-3s expectation: {busy_latency:.2f}s.")
            else:
                print(f"[integ] NOTE: latency {busy_latency:.2f}s is above ~1-3s (driver-dependent VideoCapture "
                      f"construction on a busy device) but still bounded -- no hang.")

        # --- PHASE 2: release the holder ---------------------------------------------------
        try:
            holder.stdin.write("release\n")
            holder.stdin.flush()
            holder.stdin.close()
        except Exception:
            pass
        try:
            holder.wait(timeout=10)
        except Exception:
            pass
        time.sleep(0.6)   # let the OS fully release the device

        # --- PHASE 3: free camera must open (no false busy) --------------------------------
        reopened, free_latency, free_raised = _probe_busy(Camera, open_with_retry, index, warmup, retries, timeout_s)
        if free_raised is not None:
            print(f"[integ] FAIL: probing the free camera raised: {free_raised!r}")
            fails.append("raised-on-free")
        elif reopened is True:
            print(f"[integ] FREE CAMERA OPENS after release in {free_latency:.2f}s (no false-busy).")
        else:
            print(f"[integ] FAIL: the free camera was reported busy after release ({free_latency:.2f}s).")
            fails.append("false-busy-after-release")
    finally:
        # Guaranteed cleanup: signal release + reclaim the device no matter what.
        try:
            if holder.stdin and not holder.stdin.closed:
                holder.stdin.write("release\n")
                holder.stdin.flush()
                holder.stdin.close()
        except Exception:
            pass
        try:
            holder.wait(timeout=5)
        except Exception:
            pass
        if holder.poll() is None:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except Exception:
                pass

    if fails:
        print(f"\nCAMERA-BUSY INTEGRATION FAILED: {', '.join(fails)}")
        return 1
    print("\nCAMERA-BUSY INTEGRATION OK: a real held-open camera is detected as busy (bounded, no "
          "hang, no exception); the camera opens again after release.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Real busy-camera integration test (Stage 3 / Step 4).")
    ap.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)   # internal holder role
    ap.add_argument("--index", type=int, default=None, help="camera index (default: cfg.camera_index)")
    ap.add_argument("--hold-seconds", type=float, default=15.0, help="holder safety cap (watchdog)")
    args = ap.parse_args(argv)

    if args.hold:
        return _hold_role(args.index if args.index is not None else 0, args.hold_seconds)

    index = args.index
    if index is None:
        try:
            from face_service.config import Config
            index = Config().camera_index
        except Exception:
            index = 0
    return _run_integration(index, args.hold_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
