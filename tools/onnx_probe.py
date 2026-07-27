"""tools/onnx_probe.py — Шаг 3 (v4): InsightFace buffalo_l на GPU.
Фикс cuDNN SUBLIBRARY_LOADING_FAILED: nvidia/*/bin на PATH + явный ctypes-preload всех cuDNN DLL по полному пути."""
from __future__ import annotations
import os, sys, time, ctypes, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np

print(">>> ONNX_PROBE v4 <<<")


def prep_nvidia_dlls():
    dirs, loaded, failed = [], [], []
    try:
        import nvidia
    except ImportError:
        print("nvidia namespace НЕ найден"); return dirs, loaded, failed
    roots = [Path(p) for p in nvidia.__path__]
    for root in roots:
        for sub in sorted(root.iterdir()):
            b = sub / "bin"
            if b.is_dir():
                try: os.add_dll_directory(str(b))
                except OSError: pass
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")
                dirs.append(f"{sub.name}/bin")
    cudnn_bin = next((r / "cudnn" / "bin" for r in roots if (r / "cudnn" / "bin").is_dir()), None)
    if cudnn_bin:
        dlls = sorted(cudnn_bin.glob("*.dll"))
        for _ in range(2):   # 2 прохода: добираем упавшие на порядке зависимостей
            for d in dlls:
                if d.name in loaded: continue
                try: ctypes.WinDLL(str(d)); loaded.append(d.name)
                except OSError: pass
        failed = [d.name for d in dlls if d.name not in loaded]
    return dirs, loaded, failed


_dirs, _loaded, _failed = prep_nvidia_dlls()
print("nvidia bin на PATH:", ", ".join(_dirs) or "(нет)")
print(f"cuDNN DLL предзагружено: {len(_loaded)} | НЕ удалось: {_failed or '—'}")

import onnxruntime as ort
ort.preload_dlls()
from insightface.app import FaceAnalysis
from face_service.camera import Camera


def stats(xs):
    a = np.array(xs); return f"min={a.min()*1000:6.1f}ms  avg={a.mean()*1000:6.1f}ms  max={a.max()*1000:6.1f}ms"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--det", type=int, default=640)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--cpu", action="store_true")
    args = ap.parse_args()

    providers = ["CPUExecutionProvider"] if args.cpu else ["CUDAExecutionProvider", "CPUExecutionProvider"]
    print("providers requested:", providers)

    t0 = time.perf_counter()
    app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"], providers=providers)
    app.prepare(ctx_id=(-1 if args.cpu else 0), det_size=(args.det, args.det))
    print(f"FaceAnalysis init+prepare: {time.perf_counter()-t0:.2f}s (det_size={args.det})")

    black = np.zeros((args.det, args.det, 3), dtype=np.uint8)
    tw = time.perf_counter(); app.get(black)
    print(f"warmup: {time.perf_counter()-tw:.3f}s")

    print(f"\nкамера index={args.index}...")
    frame = None
    with Camera(index=args.index) as cam:
        for _ in range(10):
            frame = cam.read()
            if frame is not None: break
            time.sleep(0.05)
    if frame is None: print("нет кадра"); return
    faces = app.get(frame)
    if not faces: print("ЛИЦО НЕ НАЙДЕНО — сядь перед камерой"); return
    f0 = faces[0]
    print(f"лиц: {len(faces)} | dim={f0.normed_embedding.shape[0]} | det_score={f0.det_score:.3f}")

    det_t, full_t = [], []
    for _ in range(args.n):
        td = time.perf_counter(); app.det_model.detect(frame); det_t.append(time.perf_counter()-td)
        tf = time.perf_counter(); app.get(frame); full_t.append(time.perf_counter()-tf)
    print(f"\n--- тайминги, {args.n} прогонов ---")
    print(f"total detect+align+embed : {stats(full_t)}")
    print(f"detect only              : {stats(det_t)}")
    e = np.array(full_t) - np.array(det_t); e = e[e > 0]
    if len(e): print(f"embed+align (derived)    : {stats(e)}")
    print(f"\navg total = {np.mean(full_t):.3f}s/кадр | сток end-to-end ≈ 0.67s")
    print("↑ ~0.02–0.03s = GPU | ~0.10s = всё ещё CPU (ищи 'Could not locate'/'Falling back' выше)")


if __name__ == "__main__":
    main()
