"""tools/bench.py - Stage 1 acceptance benchmark for the InsightFace engine.

Measures the real Recognizer.verify_frame path (liveness + embed + cosine),
warmup time, self-distance distribution, and effective ORT provider.
Usage: python -m tools.bench [N]   (default N=10)
"""
from __future__ import annotations
import sys
import time
import logging
import numpy as np

from face_service.config import Config
from face_service.recognizer import Recognizer
from face_service.camera import Camera

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def eff_provider(rec: Recognizer) -> str:
    try:
        return str(rec._app.det_model.session.get_providers())
    except Exception:
        return "?"


def stats(xs):
    a = np.array(xs, dtype=float)
    return (f"min={a.min():.3f}s avg={a.mean():.3f}s max={a.max():.3f}s"
            f"  (min={a.min()*1000:.0f}ms avg={a.mean()*1000:.0f}ms)")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    cfg = Config.load()
    print(f"config: threshold={cfg.threshold} anti_spoofing={cfg.anti_spoofing} "
          f"detector={cfg.detector_backend}")

    rec = Recognizer(cfg)
    if not rec.load():
        print("ERROR: no valid enrollment (run: python -m tools.enroll capture --count 15)")
        return

    t0 = time.perf_counter()
    rec._lazy_app()  # triggers _prep_cuda_dlls + FaceAnalysis + warmup
    warm = time.perf_counter() - t0
    print(f"\nwarmup (engine init incl. cuDNN preload): {warm:.2f}s   [target < 5s]")
    print(f"effective provider: {eff_provider(rec)}")

    print(f"\ncapturing {n} live frames + verify (sit in front of camera)...")
    lat, dist = [], []
    match_true = real_true = 0
    with Camera(cfg.camera_index, warmup_frames=cfg.camera_warmup_frames) as cam:
        for i in range(n):
            for _ in range(3):
                cam.read()
            frame = cam.read()
            if frame is None:
                print(f"  [{i+1}/{n}] no frame"); continue
            t = time.perf_counter()
            is_match, d, is_real = rec.verify_frame(frame)
            lat.append(time.perf_counter() - t)
            dist.append(d)
            match_true += int(is_match); real_true += int(is_real)
            print(f"  [{i+1}/{n}] match={is_match!s:5} dist={d:.4f} real={is_real!s:5} "
                  f"({lat[-1]*1000:.0f}ms)")

    if not lat:
        print("no successful frames"); return

    print("\n================ STAGE 1 RESULTS ================")
    print(f"end-to-end verify : {stats(lat)}   [stock avg ~0.67s]")
    dd = np.array(dist)
    print(f"self-distance     : min={dd.min():.4f} avg={dd.mean():.4f} max={dd.max():.4f}   [need <= 0.35]")
    print(f"match=True        : {match_true}/{len(lat)}")
    print(f"real=True         : {real_true}/{len(lat)}")
    print(f"provider          : {eff_provider(rec)}")
    print(f"warmup            : {warm:.2f}s")
    margin = cfg.threshold - dd.max()
    print(f"\nthreshold={cfg.threshold}  worst self-dist={dd.max():.4f}  margin={margin:.4f}")
    if dd.max() > 0.35:
        print("  ! self-distance above 0.35 - check lighting / re-enroll")
    print("  (stranger/photo distance will be measured in Step 8 regression)")


if __name__ == "__main__":
    main()
