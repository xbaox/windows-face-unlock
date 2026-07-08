"""tools/enroll_qc_probe.py -- Stage 3 / Step 1 (read-only).

Measure enrollment-frame quality on the CURRENT enroll photos and show which
frames the live QC gates would keep or drop. The gates come from config
(enroll_min_det_score / enroll_min_sharpness / enroll_luma_min / enroll_luma_max)
and the metrics come from face_service.enroll_qc -- the SAME code enroll_from_dir
uses -- so the "verdict" column here matches what enrollment would actually do.

Uses the same engine and largest-face selection as enroll_from_dir, so det_score
matches. Read-only: never touches embeddings.npz, config, or the running service.

Run from the repo root:
    python -m tools.enroll_qc_probe
    python -m tools.enroll_qc_probe --dir "C:\\path\\to\\photos"
    python -m tools.enroll_qc_probe --save-crops     # dump aligned crops to eyeball
"""
from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from face_service.config import Config, ENROLL_DIR
from face_service.recognizer import Recognizer
from face_service import enroll_qc

IMG_EXTS = {".jpg", ".jpeg", ".png"}

HEADER = (
    f"{'file':<26}{'#f':>3}{'det':>7}{'facepx':>8}"
    f"{'blur':>9}{'luma':>7}{'lo%':>6}{'hi%':>6}  verdict"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Enroll-frame quality probe (Stage 3 / Step 1, read-only)."
    )
    ap.add_argument("--dir", type=Path, default=ENROLL_DIR,
                    help="folder of enroll photos (default: the enroll dir)")
    ap.add_argument("--save-crops", action="store_true",
                    help="write aligned crops to <dir>/_qc_crops for eyeballing")
    args = ap.parse_args(argv)

    directory: Path = args.dir
    imgs = (
        sorted(p for p in directory.iterdir() if p.suffix.lower() in IMG_EXTS)
        if directory.is_dir() else []
    )
    if not imgs:
        print(f"No enroll images (.jpg/.jpeg/.png) in {directory}")
        return 1

    import cv2

    cfg = Config.load()
    print(f"threshold={cfg.threshold:g}  |  QC gates: det>={cfg.enroll_min_det_score:g}, "
          f"sharp>={cfg.enroll_min_sharpness:g}, luma {cfg.enroll_luma_min:g}-"
          f"{cfg.enroll_luma_max:g}, need>={cfg.enroll_min_frames}")
    print("loading engine (buffalo_l, det_size=640), warms CUDA once...")
    rec = Recognizer(cfg)
    app = rec._lazy_app()

    crop_dir = None
    if args.save_crops:
        crop_dir = directory / "_qc_crops"
        crop_dir.mkdir(exist_ok=True)

    print(f"\nProbing {len(imgs)} photo(s) in {directory}\n")
    print(HEADER)
    print("-" * len(HEADER))

    all_q = []          # FrameQuality of every detected face (for distribution)
    n_pass = 0
    for p in imgs:
        img = cv2.imread(str(p))
        if img is None:
            print(f"{p.name:<26}  cannot read image")
            continue
        faces = app.get(img)
        if not faces:
            print(f"{p.name:<26}{0:>3}   no face  ->  would be REJECTED")
            continue
        face = rec._largest_face(faces)
        q = enroll_qc.frame_quality(img, face)
        if q is None:
            print(f"{p.name:<26}{len(faces):>3}   crop failed")
            continue
        reasons = enroll_qc.qc_reasons(q, cfg)
        verdict = "ok" if not reasons else "DROP: " + ",".join(reasons)
        if not reasons:
            n_pass += 1
        all_q.append(q)
        print(
            f"{p.name:<26}{len(faces):>3}{q.det:>7.3f}{q.face_px:>8}"
            f"{q.sharpness:>9.1f}{q.luma:>7.1f}"
            f"{q.dark_frac * 100:>6.1f}{q.bright_frac * 100:>6.1f}  {verdict}"
        )
        if crop_dir is not None:
            crop = enroll_qc.aligned_crop(img, face)
            if crop is not None:
                cv2.imwrite(str(crop_dir / f"{p.stem}_crop.png"), crop)

    if not all_q:
        print("\nNo usable faces at all in this folder.")
        return 1

    def stat(vals):
        return min(vals), statistics.median(vals), max(vals)

    print("\n--- distribution over faces found (min / median / max) ---")
    rows = (
        ("det_score", [q.det for q in all_q], False),
        ("face_px", [q.face_px for q in all_q], False),
        ("blur(varLap112)", [q.sharpness for q in all_q], False),
        ("luma", [q.luma for q in all_q], False),
        ("dark_frac", [q.dark_frac for q in all_q], True),
        ("bright_frac", [q.bright_frac for q in all_q], True),
    )
    for label, vals, pct in rows:
        lo, md, hi = stat(vals)
        if pct:
            print(f"  {label:<17}{lo * 100:8.1f}% /{md * 100:8.1f}% /{hi * 100:8.1f}%")
        else:
            print(f"  {label:<17}{lo:9.2f} /{md:9.2f} /{hi:9.2f}")

    print(f"\nphotos read: {len(imgs)}   faces found: {len(all_q)}   "
          f"pass QC: {n_pass}/{len(all_q)}   (need >= {cfg.enroll_min_frames})")
    if n_pass < cfg.enroll_min_frames:
        print("Under the current gates enroll_from_dir would REJECT this set.")
    if crop_dir is not None:
        print(f"Aligned crops written to {crop_dir} -- open a few to sanity-check sharpness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
