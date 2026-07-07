#!/usr/bin/env python3
"""recog_smoke.py -- Stage 2 Step 5a: validate the DeepFace-free recognizer.

Checks, with no camera and no service:
  - recognizer imports and runs WITHOUT pulling DeepFace / TensorFlow / torch
  - InsightFace runs on GPU (ctx_id=0), all 4 modules loaded
  - warmup time (should drop ~5.6s now that the TF/MiniFASNet warmup is gone)
  - analyze_frame + verify_frame work on an enrolled image (match/distance/is_real/landmark/pose)

Run from repo root:
    python tools\\recog_smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np

from face_service.config import Config, ENROLL_DIR
from face_service.recognizer import Recognizer


def main():
    cfg = Config.load()
    r = Recognizer(cfg)

    loaded = r.load()
    print(f"enrollment loaded: {loaded}")

    t0 = time.time()
    r._lazy_app()                      # build + warm models
    warm = time.time() - t0
    print(f"warmup (build + warm models): {warm:.2f}s   (was ~7.3s with TF; want < 5s)")

    tf_like = [m for m in list(sys.modules) if m.startswith(("tensorflow", "deepface", "torch"))]
    print(f"TF/torch/deepface modules imported: {tf_like or 'none'}   (want none)")

    imgs = sorted(list(ENROLL_DIR.glob("*.jpg")) + list(ENROLL_DIR.glob("*.png")))
    if not imgs:
        print("no enroll images found; skipping verify test")
        return
    img = cv2.imread(str(imgs[0]))
    if img is None:
        print(f"could not read {imgs[0]}; skipping verify test")
        return

    a = r.analyze_frame(img)
    print(f"analyze_frame: face={a.face} match={a.is_match} dist={a.distance:.4f} "
          f"screen={a.screen} landmark={'yes' if a.landmark is not None else 'NO'} "
          f"pose={'yes' if a.pose is not None else 'NO'}")

    ts = []
    m = d = real = None
    for _ in range(10):
        t = time.time()
        m, d, real = r.verify_frame(img)
        ts.append(time.time() - t)
    print(f"verify_frame: match={m} dist={d:.4f} real={real}   "
          f"avg={np.mean(ts)*1000:.1f}ms min={np.min(ts)*1000:.1f}ms")
    print("\n(on your enroll photo expect: match=True, dist ~0.07, landmark/pose=yes, "
          "real=True unless the photo is dim enough to trip anti-screen)")


if __name__ == "__main__":
    main()
