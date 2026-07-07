#!/usr/bin/env python3
"""screen_probe.py -- Stage 2 Step 4: anti-screen feature collection & separation.

Computes candidate "is this a display, not a real face?" features on the detected face crop
and lets you record labelled samples (LIVE vs SCREEN) to see which feature separates on YOUR
camera. Nothing is hardcoded: we tune thresholds on these numbers and measure the
false-positive rate on live before wiring a detector.

Features (per face crop, 128x128 gray, Hann-windowed):
  hf   : fraction of FFT magnitude energy in the high-frequency ring (moire lifts this)
  peak : max/mean of the mid-high frequency band (moire = sharp spectral peaks)
  lap  : variance of the Laplacian (raw texture / sharpness)

Reuses the Stage-1 CUDA fix. Read-only webcam probe. Touches nothing in the service.

Run from repo root:
    python tools\\screen_probe.py
    python tools\\screen_probe.py --camera 1

Keys:
  l : toggle recording as LIVE   (hold a real face in frame)
  s : toggle recording as SCREEN (hold a phone showing your photo/video in frame)
  c : clear all collected samples
  q / ESC : quit and print the separation summary
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
CROP = 128


def build_app():
    _prep_cuda_dlls()
    import onnxruntime as ort
    try:
        ort.preload_dlls()
    except Exception:
        pass
    from insightface.app import FaceAnalysis
    providers, ctx_id = _select_providers(ort)
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"], providers=providers)
    app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))
    print(f"[screen] providers={ort.get_available_providers()} ctx_id={ctx_id} "
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


def face_gray(frame, bbox, size=CROP):
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    g = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (size, size)).astype(np.float32)


_WIN = np.outer(np.hanning(CROP), np.hanning(CROP)).astype(np.float32)
_R = None


def _radius_norm():
    global _R
    if _R is None:
        cy = cx = CROP // 2
        Y, X = np.ogrid[:CROP, :CROP]
        r = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
        _R = (r / r.max()).astype(np.float32)
    return _R


def features(gray):
    g = (gray - gray.mean()) * _WIN
    mag = np.abs(np.fft.fftshift(np.fft.fft2(g)))
    rn = _radius_norm()
    total = float(mag.sum()) + 1e-9
    hf = float(mag[rn > 0.5].sum()) / total
    band = mag[(rn > 0.35) & (rn < 0.75)]
    peak = float(band.max() / (band.mean() + 1e-9)) if band.size else 0.0
    lap = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    return hf, peak, lap


def summary(live, screen):
    names = ["hf", "peak", "lap"]
    L = np.asarray(live, dtype=np.float64)
    S = np.asarray(screen, dtype=np.float64)
    print("\n================ SEPARATION SUMMARY ================")
    print(f"samples: LIVE={len(L)}  SCREEN={len(S)}")
    if len(L) == 0 or len(S) == 0:
        print("Need both LIVE and SCREEN samples. Record with 'l' and 's'.")
        return
    for i, nm in enumerate(names):
        lm, ls = L[:, i].mean(), L[:, i].std() + 1e-9
        sm, ss = S[:, i].mean(), S[:, i].std() + 1e-9
        # separation: gap between means over pooled spread (higher = cleaner split)
        sep = abs(sm - lm) / (ls + ss)
        thr = (lm + sm) / 2.0
        direction = "screen>live" if sm > lm else "screen<live"
        print(f"  {nm:5s}: LIVE {lm:10.4f} +/- {ls:8.4f} | SCREEN {sm:10.4f} +/- {ss:8.4f} "
              f"| sep={sep:5.2f} | midpoint thr~={thr:.4f} ({direction})")
    # Empirical per-frame flag rate at the module's hf-only gate on THESE samples (real):
    try:
        from face_service.liveness import HF_THRESH
        live_fp = float((L[:, 0] < HF_THRESH).mean())
        screen_det = float((S[:, 0] < HF_THRESH).mean())
        print(f"\nAnti-screen gate is hf-only (lap drifts with light -> telemetry, not gated).")
        print(f"At hf < {HF_THRESH}, per frame:")
        print(f"  live false-positive = {live_fp*100:5.2f}%   (want LOW; drift-robust target <1%)")
        print(f"  screen detection    = {screen_det*100:5.2f}%   (weak/conditional bonus trigger)")
        print("  Production: doubt trigger -> escalate to gesture (the real replay defense),")
        print("  aggregated over the verify window. Not a hard reject in fast mode.")
    except Exception as e:
        print(f"\n(could not import HF_THRESH: {e})")
    print("====================================================")


def main():
    ap = argparse.ArgumentParser(description="Stage 2 anti-screen feature probe")
    ap.add_argument("--camera", type=int, default=CAM_INDEX)
    args = ap.parse_args()

    app = build_app()
    cap = open_cam(args.camera)
    live: list[tuple[float, float, float]] = []
    screen: list[tuple[float, float, float]] = []
    mode = None  # None | "live" | "screen"

    print("[screen] 'l'=record LIVE, 's'=record SCREEN, 'c'=clear, 'q'/ESC=quit+summary.")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame = cv2.flip(frame, 1)
        faces = app.get(frame)
        feat = None
        if faces:
            f = largest(faces)
            g = face_gray(frame, f.bbox)
            if g is not None:
                feat = features(g)
                if mode == "live":
                    live.append(feat)
                elif mode == "screen":
                    screen.append(feat)
                x1, y1, x2, y2 = [int(v) for v in f.bbox]
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 0), 1)

        rec = {None: "idle", "live": "REC LIVE", "screen": "REC SCREEN"}[mode]
        rec_col = {None: (180, 180, 180), "live": (0, 255, 0), "screen": (0, 0, 255)}[mode]
        line1 = f"[{rec}]  live={len(live)}  screen={len(screen)}"
        cv2.putText(frame, line1, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, rec_col, 2, cv2.LINE_AA)
        if feat is not None:
            cv2.putText(frame, f"hf={feat[0]:.4f}  peak={feat[1]:6.1f}  lap={feat[2]:7.1f}",
                        (10, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(frame, "l=LIVE  s=SCREEN  c=clear  q=quit", (10, 82),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)

        cv2.imshow("screen_probe :: anti-screen features", frame)
        key = cv2.waitKey(1) & 0xFF
        if key in (27, ord('q')):
            break
        elif key == ord('l'):
            mode = None if mode == "live" else "live"
        elif key == ord('s'):
            mode = None if mode == "screen" else "screen"
        elif key == ord('c'):
            live.clear(); screen.clear(); mode = None
            print("[screen] cleared.")

    cap.release()
    cv2.destroyAllWindows()
    summary(live, screen)


if __name__ == "__main__":
    main()
