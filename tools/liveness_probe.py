#!/usr/bin/env python3
"""liveness_probe.py -- Stage 2, Step 1 probe: InsightFace 106-pt landmarks + blink (EAR).

Goal: prove buffalo_l's landmark_2d_106 model yields usable eyelid points and that an
openness / EAR signal responds to a real blink. Reuses the exact Stage-1 CUDA fix
(_prep_cuda_dlls) so this runs on the GPU just like the service does. Does NOT touch the
service, recognizer, config, or the enrolled data -- read-only webcam probe with its own
FaceAnalysis instance.

Modes
-----
--map  (default): draw the 106 landmarks; mark the detector's two eye-centers; label ONLY
                  the landmark points nearest each eye-center (so you can read off the eye
                  indices); show a live, index-agnostic openness reading + blink counter so
                  you can confirm a blink moves the signal on the very first run.
                  'p' dumps the eye-region indices/coords to console. 'q'/ESC quits.
--ear           : canonical 6-point EAR using EYE_IDX (fill EYE_IDX in after --map).

Run from repo root (C:\\dev\\windows-face-unlock):
    python tools\\liveness_probe.py            # map mode (default)
    python tools\\liveness_probe.py --ear      # after EYE_IDX is set
    python tools\\liveness_probe.py --camera 1 # if webcam is not index 0
"""
from __future__ import annotations

import sys
import time
import argparse
from pathlib import Path

# Make `face_service` importable whether this is run as a script or a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

# Reuse the Stage-1 GPU fix and provider selection verbatim -- do not reimplement.
from face_service.recognizer import _prep_cuda_dlls, _select_providers

DET_SIZE = 640
CAM_INDEX = 0
N_EYE_NEIGHBORS = 12   # landmark points nearest an eye-center treated as that eye's cluster

# --- canonical EAR indices (106-pt model). LEAVE None until confirmed via --map. ---
# After --map, set each eye to 6 indices IN ORDER:
#   [outer_corner, top1, top2, inner_corner, bottom2, bottom1]
EYE_IDX = {
    "left":  [35, 41, 42, 39, 37, 36],   # eye A: [outer, top1, top2, inner, bottom2, bottom1]
    "right": [89, 95, 96, 93, 91, 90],   # eye B
}
# example once known:
# EYE_IDX = {"left": [.., .., .., .., .., ..], "right": [.., .., .., .., .., ..]}

EAR_BLINK_THRESH = 0.21   # canonical EAR "closed" threshold (provisional -- tune on your face)
BLINK_CONSEC = 2          # consecutive frames below threshold to count one blink


def build_app():
    """Own FaceAnalysis with detection + 2d106 landmarks on GPU (Stage-1 CUDA path reused)."""
    _prep_cuda_dlls()  # must run before any CUDA session is created
    import onnxruntime as ort
    try:
        ort.preload_dlls()
    except Exception:
        pass
    from insightface.app import FaceAnalysis
    providers, ctx_id = _select_providers(ort)
    app = FaceAnalysis(
        name="buffalo_l",
        allowed_modules=["detection", "landmark_2d_106"],
        providers=providers,
    )
    app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))
    print(f"[probe] providers={ort.get_available_providers()} ctx_id={ctx_id} "
          f"(ctx_id=0 => GPU, -1 => CPU)")
    return app


def open_cam(index):
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)  # DSHOW opens faster on Windows
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


def eye_clusters(lmk, kps, k=N_EYE_NEIGHBORS):
    """Indices of the k landmarks nearest each detector eye-center (kps[0], kps[1])."""
    out = []
    for c in (kps[0], kps[1]):
        d = np.linalg.norm(lmk - c, axis=1)
        out.append(np.argsort(d)[:k])
    return out[0], out[1]


def openness(pts):
    """Index-agnostic eye openness = cluster height / width. Drops when the eye closes."""
    w = pts[:, 0].max() - pts[:, 0].min()
    h = pts[:, 1].max() - pts[:, 1].min()
    return float(h / (w + 1e-9))


def ear(lmk, idx):
    """Canonical eye aspect ratio from 6 ordered points."""
    p1, p2, p3, p4, p5, p6 = (lmk[i] for i in idx)
    a = np.linalg.norm(p2 - p6)
    b = np.linalg.norm(p3 - p5)
    c = np.linalg.norm(p1 - p4)
    return float((a + b) / (2.0 * c + 1e-9))


def _fps(prev, smooth):
    now = time.time()
    inst = 1.0 / max(1e-3, now - prev)
    return now, (0.9 * smooth + 0.1 * inst)


def run_map(app, cam_index):
    cap = open_cam(cam_index)
    base = 0.0      # adaptive open-eye baseline for index-agnostic openness
    below = 0
    blinks = 0
    snapshot = None
    t_prev = time.time()
    fps = 0.0
    print("[probe] MAP mode. Cyan cross = eye-center. Green = eye-region landmarks "
          "(read these indices). 'p' dumps indices, 'q'/ESC quits.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)  # mirror for natural feel
        faces = app.get(frame)
        info = "no face"
        if faces:
            f = largest(faces)
            if not hasattr(f, "landmark_2d_106") or f.landmark_2d_106 is None:
                info = "landmark_2d_106 MISSING (module not loaded?)"
            else:
                lmk = f.landmark_2d_106.astype(np.float32)
                kps = f.kps.astype(np.float32)
                li, ri = eye_clusters(lmk, kps)
                eye_idx = np.concatenate([li, ri])
                snapshot = (li, ri, lmk.copy())

                for p in lmk.astype(int):
                    cv2.circle(frame, tuple(p), 1, (120, 120, 120), -1)
                for c in kps[:2].astype(int):
                    cv2.drawMarker(frame, tuple(c), (255, 255, 0),
                                   cv2.MARKER_CROSS, 14, 2)
                for i in eye_idx:
                    p = lmk[i].astype(int)
                    cv2.circle(frame, tuple(p), 2, (0, 255, 0), -1)
                    cv2.putText(frame, str(int(i)), (p[0] + 2, p[1] - 2),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 255, 0), 1, cv2.LINE_AA)

                op = 0.5 * (openness(lmk[li]) + openness(lmk[ri]))
                base = max(op, base * 0.99)   # slow-decay running max = open baseline
                thr = base * 0.55
                if op < thr:
                    below += 1
                else:
                    if below >= BLINK_CONSEC:
                        blinks += 1
                    below = 0
                info = f"openness={op:.3f} base={base:.3f} thr={thr:.3f} blinks={blinks}"

        t_prev, fps = _fps(t_prev, fps)
        cv2.putText(frame, info, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 255), 2, cv2.LINE_AA)
        cv2.putText(frame, f"{fps:4.1f} fps  [p]=dump  [q]=quit", (10, 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)
        cv2.imshow("liveness_probe :: map", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break
        if key == ord('p') and snapshot is not None:
            li, ri, lmk = snapshot

            def dump(name, idx):
                print(f"\n[{name}] landmark indices nearest eye-center "
                      f"(index: x, y), sorted by distance:")
                for i in idx:
                    print(f"  {int(i):3d}: {lmk[i][0]:6.1f}, {lmk[i][1]:6.1f}")

            dump("eye-A (kps[0])", li)
            dump("eye-B (kps[1])", ri)
            print("\nFor canonical EAR pick 6 per eye, in order: "
                  "[outer_corner, top1, top2, inner_corner, bottom2, bottom1]")
    cap.release()
    cv2.destroyAllWindows()


def run_ear(app, cam_index):
    if EYE_IDX is None:
        print("EYE_IDX is not set. Run --map first, read the eye indices off the overlay "
              "(or the 'p' dump), then fill EYE_IDX at the top of this file (6 per eye).")
        return
    cap = open_cam(cam_index)
    below = 0
    blinks = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)
        faces = app.get(frame)
        info = "no face"
        if faces:
            f = largest(faces)
            lmk = f.landmark_2d_106.astype(np.float32)
            el = ear(lmk, EYE_IDX["left"])
            er = ear(lmk, EYE_IDX["right"])
            e = 0.5 * (el + er)
            if e < EAR_BLINK_THRESH:
                below += 1
            else:
                if below >= BLINK_CONSEC:
                    blinks += 1
                below = 0
            for side in ("left", "right"):
                for i in EYE_IDX[side]:
                    cv2.circle(frame, tuple(lmk[i].astype(int)), 2, (0, 255, 0), -1)
            info = f"EAR L={el:.3f} R={er:.3f} avg={e:.3f} thr={EAR_BLINK_THRESH} blinks={blinks}"
        cv2.putText(frame, info, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow("liveness_probe :: ear", frame)
        if (cv2.waitKey(1) & 0xFF) in (27, ord('q')):
            break
    cap.release()
    cv2.destroyAllWindows()


def main():
    ap = argparse.ArgumentParser(description="Stage 2 liveness probe (106-pt landmarks + blink)")
    ap.add_argument("--ear", action="store_true", help="canonical EAR mode (needs EYE_IDX set)")
    ap.add_argument("--camera", type=int, default=CAM_INDEX, help="camera index (default 0)")
    args = ap.parse_args()
    app = build_app()
    if args.ear:
        run_ear(app, args.camera)
    else:
        run_map(app, args.camera)


if __name__ == "__main__":
    main()
