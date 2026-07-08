#!/usr/bin/env python3
"""pose_probe.py -- Stage 2 Step 3 probe: 3D head pose from InsightFace 1k3d68.

Decision B: enable landmark_3d_68 (already on disk in buffalo_l) to get head pose for the
gesture-escalation challenge (turn left/right, nod). This probe checks EMPIRICALLY what the
3d68 model exposes on this machine -- does face.pose exist? what are the real yaw/pitch/roll
ranges when the head turns? -- so gesture thresholds are locked on numbers, not assumed.

Reuses the Stage-1 CUDA fix. Read-only webcam probe. Does not touch service/recognizer/config.

Run from repo root:
    python tools\\pose_probe.py
    python tools\\pose_probe.py --camera 1
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from face_service.recognizer import _prep_cuda_dlls, _select_providers

DET_SIZE = 640
CAM_INDEX = 0


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
        allowed_modules=["detection", "landmark_3d_68"],   # pose comes from 3d68
        providers=providers,
    )
    app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))
    print(f"[pose] providers={ort.get_available_providers()} ctx_id={ctx_id} "
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
    ap = argparse.ArgumentParser(description="Stage 2 head-pose probe (1k3d68)")
    ap.add_argument("--camera", type=int, default=CAM_INDEX)
    args = ap.parse_args()

    app = build_app()
    cap = open_cam(args.camera)
    dumped = False
    have_pose = None

    print("[pose] Look straight, then: turn LEFT, turn RIGHT, NOD down, tilt HEAD. "
          "Watch which value moves. 'q'/ESC quits.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)
        faces = app.get(frame)
        info = "no face"
        if faces:
            f = largest(faces)

            if not dumped:
                # One-time: show exactly what the 3d68 model populated on this build.
                keys = list(f.keys()) if hasattr(f, "keys") else dir(f)
                print(f"\n[pose] face keys: {keys}")
                p = f.get("pose") if hasattr(f, "get") else None
                have_pose = p is not None
                if have_pose:
                    print(f"[pose] face.pose present -> {np.asarray(p)} "
                          f"(shape {np.asarray(p).shape})")
                    print("[pose] mapping to confirm by moving your head: one of these is "
                          "yaw (turn L/R), one pitch (nod), one roll (tilt).")
                else:
                    print("[pose] face.pose NOT present. We'll switch to a solvePnP fallback "
                          "from the 68 landmarks -- tell me and I'll ship that instead.")
                dumped = True

            pose = f.get("pose") if hasattr(f, "get") else None
            lmk = f.get("landmark_3d_68") if hasattr(f, "get") else None

            if lmk is not None:
                for p in np.asarray(lmk)[:, :2].astype(int):
                    cv2.circle(frame, tuple(p), 1, (0, 200, 255), -1)

            if pose is not None:
                a = np.asarray(pose, dtype=np.float32).ravel()
                if a.size >= 3:
                    info = f"pose[0]={a[0]:+6.1f}  pose[1]={a[1]:+6.1f}  pose[2]={a[2]:+6.1f}"
            else:
                info = "no face.pose (see console)"

        cv2.putText(frame, info, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, "straight / left / right / nod / tilt   [q]=quit", (10, 56),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
        cv2.imshow("pose_probe :: 1k3d68", frame)
        if (cv2.waitKey(1) & 0xFF) in (27, ord('q')):
            break
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
