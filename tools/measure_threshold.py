r"""Stage 2, Step 9 -- measure recognition distances to fix a safe threshold on REAL numbers.

For each sample it computes the best cosine distance to the enrolled face and prints per-source
stats. Samples come from live camera frames or a folder of images. Results accumulate across runs
(in APP_DIR/threshold_samples.json) so you can gather several classes and get one recommendation.

Any label containing "self" is the genuine class; every other label is impostor/spoof.

Usage (from repo root, enrollment must exist):
    # live self -- VARY conditions across the run (glasses on/off, dim light, angles, near/far):
    python -m tools.measure_threshold self camera:40

    # impostor: point the webcam at another person or a stranger's photo on your phone:
    python -m tools.measure_threshold impostor camera:25

    # or folders of images:
    python -m tools.measure_threshold self C:\path\to\my_photos
    python -m tools.measure_threshold impostor C:\path\to\stranger_photos spoof C:\path\to\phone_shots

    python -m tools.measure_threshold --show          # print current accumulation + recommendation
    python -m tools.measure_threshold --reset         # clear accumulated samples

While the service is running it owns the webcam, so camera mode asks it to pause_camera first
(and resumes after). Pass --no-pause to skip that (e.g. service stopped).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLES_FILENAME = "threshold_samples.json"
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


# ---------- pure recommendation logic (unit-tested without a camera) ----------

def recommend_threshold(samples: dict[str, list[float]], current: float,
                        buffer: float = 0.12) -> dict:
    """From accumulated {label: distances}, derive threshold stats + a suggested value.

    Genuine = distances under any label containing "self"; impostor = the rest. The suggestion
    is security-biased: it sits a small ``buffer`` above the observed genuine max (so self keeps
    passing across conditions) and, when impostors are present and cleanly separated, is clamped
    to stay clear below the impostor min. Impostors mainly confirm there is headroom -- with
    ArcFace, unrelated faces sit far away, so the binding constraint is the genuine spread.
    """
    genuine = [d for lbl, ds in samples.items() if "self" in lbl.lower() for d in ds]
    impostor = [d for lbl, ds in samples.items() if "self" not in lbl.lower() for d in ds]
    out: dict = {"current": current, "genuine_n": len(genuine), "impostor_n": len(impostor)}
    gmax = imin = None
    if genuine:
        gmax = max(genuine)
        out["genuine_max"] = round(gmax, 4)
        out["genuine_mean"] = round(statistics.mean(genuine), 4)
    if impostor:
        imin = min(impostor)
        out["impostor_min"] = round(imin, 4)
        out["impostor_mean"] = round(statistics.mean(impostor), 4)
    if gmax is not None:
        sug = gmax + buffer
        if imin is not None:
            out["separated"] = gmax < imin
            if gmax < imin:
                out["gap"] = round(imin - gmax, 4)
                sug = min(sug, imin - 0.05)       # stay clear of impostors
            else:
                out["overlap"] = round(gmax - imin, 4)
        out["suggested"] = round(max(sug, gmax + 1e-6), 3)
    return out


# ---------- accumulation ----------

def _accum_path() -> Path:
    from face_service.config import APP_DIR
    return APP_DIR / SAMPLES_FILENAME


def _load_accum() -> dict[str, list[float]]:
    try:
        return json.loads(_accum_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_accum(data: dict[str, list[float]]) -> None:
    p = _accum_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


# ---------- sampling (needs InsightFace / camera; imported lazily) ----------

def _distances_from_dir(recog, folder: Path) -> tuple[list[float], int]:
    import cv2
    dists: list[float] = []
    no_face = 0
    imgs = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMG_EXT)
    if not imgs:
        raise SystemExit(f"no images in {folder}")
    for p in imgs:
        img = cv2.imread(str(p))
        if img is None:
            print(f"  skip {p.name}: unreadable")
            continue
        a = recog.analyze_frame(img)
        if a.face:
            dists.append(a.distance)
            print(f"  {p.name:32} dist={a.distance:.4f}{'  MATCH' if a.is_match else ''}")
        else:
            no_face += 1
            print(f"  {p.name:32} no face")
    return dists, no_face


def _distances_from_camera(recog, n: int, cfg, pause: bool) -> tuple[list[float], int]:
    from face_service.camera import Camera
    paused = False
    if pause:
        try:
            from tools.pipe_client import send as pipe_send
        except ImportError:
            try:
                from pipe_client import send as pipe_send  # type: ignore
            except ImportError:
                pipe_send = None
        if pipe_send is not None:
            try:
                pipe_send({"cmd": "pause_camera", "seconds": 180})
                paused = True
                print("  (service camera paused; releasing in ~0.5s)")
                time.sleep(0.5)
            except SystemExit:
                pass  # service not running -> camera is free anyway
            except Exception as e:  # noqa: BLE001
                print(f"  (pause_camera failed, continuing: {e})")

    dists: list[float] = []
    no_face = 0
    try:
        cam = Camera(cfg.camera_index, cfg.camera_warmup_frames)
        cam.open()
        try:
            for _ in range(2):
                cam.read()
            print(f"  capturing {n} frames -- keep your face (or the sample) in view...")
            got = 0
            while got < n:
                frame = cam.read()
                if frame is None:
                    continue
                got += 1
                a = recog.analyze_frame(frame)
                if a.face:
                    dists.append(a.distance)
                    print(f"  frame {got:3}/{n}  dist={a.distance:.4f}"
                          f"{'  MATCH' if a.is_match else ''}")
                else:
                    no_face += 1
                    print(f"  frame {got:3}/{n}  no face")
        finally:
            cam.close()
    finally:
        if paused:
            try:
                from tools.pipe_client import send as pipe_send  # type: ignore
                pipe_send({"cmd": "resume_camera"})
                print("  (service camera resumed)")
            except Exception:  # noqa: BLE001
                print("  (!) could not resume service camera -- send resume_camera manually")
    return dists, no_face


def _print_report(accum: dict[str, list[float]], current: float) -> None:
    print("\n=== accumulated samples ===")
    for lbl, ds in accum.items():
        if ds:
            print(f"  {lbl:16} n={len(ds):3}  min={min(ds):.4f}  "
                  f"mean={statistics.mean(ds):.4f}  max={max(ds):.4f}")
    rec = recommend_threshold(accum, current)
    print("\n=== recommendation ===")
    print(json.dumps(rec, indent=2))
    if "suggested" in rec:
        note = ""
        if rec.get("separated") is True:
            note = f" (genuine max {rec['genuine_max']} << impostor min {rec['impostor_min']}, gap {rec['gap']})"
        elif rec.get("separated") is False:
            note = f" (!! OVERLAP {rec['overlap']} -- genuine and impostor distances overlap)"
        print(f"\n-> suggested threshold: {rec['suggested']}{note}")
        print(f"   current threshold:   {current}")
        print("   set it in ~/.face-unlock/config.toml as `threshold = <value>` then reload_config,")
        print("   or send the numbers over and I'll finalize it.")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Measure recognition distances for threshold tuning.")
    ap.add_argument("pairs", nargs="*", metavar="LABEL SOURCE",
                    help="label then source; source is a folder path or 'camera:N'")
    ap.add_argument("--show", action="store_true", help="print accumulation + recommendation only")
    ap.add_argument("--reset", action="store_true", help="clear accumulated samples and exit")
    ap.add_argument("--no-pause", action="store_true", help="don't pause the service camera")
    args = ap.parse_args(argv)

    from face_service.config import Config
    cfg = Config.load()

    if args.reset:
        _save_accum({})
        print("accumulated samples cleared.")
        return 0
    if args.show:
        _print_report(_load_accum(), cfg.threshold)
        return 0
    if len(args.pairs) % 2 != 0 or not args.pairs:
        ap.error("give LABEL SOURCE pairs, e.g.  self camera:40  impostor C:\\photos")

    from face_service.recognizer import Recognizer
    recog = Recognizer(cfg)
    if not recog.load():
        raise SystemExit("no enrollment found -- enroll first (build_enrollment).")

    accum = _load_accum()
    for i in range(0, len(args.pairs), 2):
        label, source = args.pairs[i], args.pairs[i + 1]
        print(f"\n--- sampling label={label!r} source={source!r} ---")
        if source.lower().startswith("camera:"):
            n = int(source.split(":", 1)[1] or "20")
            dists, no_face = _distances_from_camera(recog, n, cfg, pause=not args.no_pause)
        else:
            dists, no_face = _distances_from_dir(recog, Path(source))
        if dists:
            print(f"  -> {len(dists)} faces  min={min(dists):.4f}  "
                  f"mean={statistics.mean(dists):.4f}  max={max(dists):.4f}  (no-face: {no_face})")
        else:
            print(f"  -> no faces detected (no-face: {no_face}) -- nothing added")
        accum.setdefault(label, [])
        accum[label].extend(round(d, 6) for d in dists)

    _save_accum(accum)
    _print_report(accum, cfg.threshold)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
