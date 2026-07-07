#!/usr/bin/env python3
"""challenge_probe.py -- Stage 2 Step 3: live test of the active-liveness challenge engine.

Runs the full stack the service will use for gestures: detection + 2d106 (blink) + 1k3d68
(pose). The engine issues a random challenge (blink / turn left / turn right / nod); you
respond; it resolves PASS/FAIL on success or timeout, then issues the next after a short
pause. Use this to confirm the left/right convention and that thresholds feel right on your
face.

Reuses the Stage-1 CUDA fix. Read-only webcam probe -- does not touch service/recognizer/config.

Run from repo root:
    python tools\\challenge_probe.py
    python tools\\challenge_probe.py --blink-only   # only blink challenges
    python tools\\challenge_probe.py --camera 1
"""
from __future__ import annotations

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from face_service.recognizer import _prep_cuda_dlls, _select_providers
from face_service.liveness import (
    LivenessChallenge, ChallengeState, Challenge, EYE_IDX,
)

DET_SIZE = 640
CAM_INDEX = 0
PAUSE_AFTER_RESOLVE_S = 1.5


def build_app():
    _prep_cuda_dlls()
    import onnxruntime as ort
    try:
        ort.preload_dlls()
    except Exception:
        pass
    from insightface.app import FaceAnalysis
    providers, ctx_id = _select_providers(ort)
    app = FaceAnalysis(
        name="buffalo_l",
        allowed_modules=["detection", "landmark_2d_106", "landmark_3d_68"],
        providers=providers,
    )
    app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))
    print(f"[chal] providers={ort.get_available_providers()} ctx_id={ctx_id} "
          f"(ctx_id=0 => GPU, -1 => CPU)")
    return app


def open_cam(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"cannot open camera index {index}")
    return cap


def largest(faces):
    def area(f):
        x1, y1, x2, y2 = f.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return max(faces, key=area)


def main():
    ap = argparse.ArgumentParser(description="Stage 2 active-liveness challenge probe")
    ap.add_argument("--camera", type=int, default=CAM_INDEX)
    ap.add_argument("--blink-only", action="store_true", help="issue only blink challenges")
    args = ap.parse_args()

    app = build_app()
    cap = open_cam(args.camera)

    kinds = (Challenge.BLINK,) if args.blink_only else None
    ch = LivenessChallenge(**({"kinds": kinds} if kinds else {}))
    ch.issue()
    resolved_at = None
    passes = 0
    fails = 0

    print(f"[chal] first challenge: {ch.prompt}. Respond to the prompt. 'q'/ESC quits.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)
        faces = app.get(frame)

        landmark = pose = None
        if faces:
            f = largest(faces)
            landmark = f.get("landmark_2d_106") if hasattr(f, "get") else None
            pose = f.get("pose") if hasattr(f, "get") else None
            if landmark is not None:
                for side in ("left", "right"):
                    for i in EYE_IDX[side]:
                        cv2.circle(frame, tuple(np.asarray(landmark)[i].astype(int)), 2, (0, 220, 0), -1)

        if not ch.done:
            ch.feed(landmark, pose)
            if ch.done:
                resolved_at = time.time()
                if ch.passed:
                    passes += 1
                else:
                    fails += 1
        else:
            # brief pause showing the verdict, then next challenge
            if resolved_at and (time.time() - resolved_at) >= PAUSE_AFTER_RESOLVE_S:
                ch.issue()
                resolved_at = None

        # --- overlay ---
        if ch.state == ChallengeState.AWAITING:
            banner, color = f"CHALLENGE: {ch.prompt}", (0, 255, 255)
        elif ch.state == ChallengeState.PASSED:
            banner, color = "PASS", (0, 255, 0)
        else:
            banner, color = "FAIL (timeout / wrong move)", (0, 0, 255)
        cv2.putText(frame, banner, (10, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
        pz = np.asarray(pose).ravel() if pose is not None else None
        sub = f"pass={passes} fail={fails}"
        if pz is not None and pz.size >= 3:
            sub += f"   pitch={pz[0]:+5.1f} yaw={pz[1]:+5.1f}"
        cv2.putText(frame, sub, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1, cv2.LINE_AA)
        cv2.putText(frame, "respond to prompt   [q]=quit", (10, 84),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1, cv2.LINE_AA)

        cv2.imshow("challenge_probe :: engine", frame)
        if (cv2.waitKey(1) & 0xFF) in (27, ord('q')):
            break
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
