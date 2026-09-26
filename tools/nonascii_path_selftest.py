"""tools/nonascii_path_selftest.py -- Unicode-safe image I/O (Stage 9, act 9b R8 / F-116; B14 N-11).

cv2.imread / cv2.imwrite go through the ANSI code page and fail on any non-ASCII path; a profile
path with one Cyrillic, Chinese or accented character made enrollment impossible. No camera, no
engine: synthetic images in a temp directory this test creates.
  [1] face_service.imio round-trips an image under paths with Cyrillic, Chinese and é, in PNG
      and JPG, and plain cv2 is shown to fail there (so the test proves the point it makes).
  [2] a write that cannot happen returns False and leaves no file (the wizard no longer counts
      such a frame as saved).
  [3] the gallery build reads its images through imio: from a non-ASCII enroll directory it finds
      them ("no images" was the symptom) -- with a stub engine, no model needed.

Run:  python -m tools.nonascii_path_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FACE_UNLOCK_HOME", tempfile.mkdtemp(prefix="faceunlock_nonascii_"))

import cv2
import numpy as np

from face_service import imio

FAILS: list[str] = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


def main() -> int:
    import shutil
    root = Path(tempfile.mkdtemp(prefix="fu-na-"))
    try:
        return _run(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)      # R20: clean up after ourselves


def _run(root: Path) -> int:
    img = np.zeros((48, 64, 3), np.uint8)
    img[:, :32] = (40, 120, 200)
    img[10:20, 40:60] = 255

    print("[1] round trip under non-ASCII paths")
    for sub in ("Бао", "张三", "José é", "mixed Бао 张三 é"):
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        for ext in (".png", ".jpg"):
            path = d / f"frame{ext}"
            wrote = imio.imwrite(path, img)
            back = imio.imread(path)
            same = back is not None and back.shape == img.shape and (
                ext == ".jpg" or np.array_equal(back, img))
            check(f"{sub}{ext}: written and read back", wrote and path.is_file() and same)
        plain = cv2.imwrite(str(d / "plain.png"), img)
        check(f"{sub}: plain cv2.imwrite fails here (the bug this fixes)",
              not plain or cv2.imread(str(d / "plain.png")) is None)

    print("[2] a write that cannot happen")
    blocker = root / "not-a-dir.txt"
    blocker.write_text("x")
    bad = blocker / "frame.jpg"
    check("imwrite into a path under a FILE returns False", imio.imwrite(bad, img) is False)
    check("... and leaves nothing behind", not bad.exists())
    check("imread of a missing file -> None", imio.imread(root / "nope.png") is None)
    (root / "garbage.jpg").write_bytes(b"not an image")
    check("imread of a non-image -> None", imio.imread(root / "garbage.jpg") is None)

    print("[3] the gallery build finds images in a non-ASCII enroll directory")
    from face_service.config import Config
    from face_service import recognizer as R
    enroll = root / "Пользователь 张 é" / "enroll"
    enroll.mkdir(parents=True)
    for i in range(3):
        imio.imwrite(enroll / f"enroll_{i}.jpg", img)
    seen = []

    class _Face:
        bbox = (0, 0, 40, 40)
        det_score = 0.9
        normed_embedding = np.ones(512, np.float32) / np.sqrt(512)

        def get(self, k):
            return None

    class _App:
        def get(self, bgr):
            seen.append(bgr.shape)
            return [_Face()]

    rec = R.Recognizer(Config())
    rec._lazy_app = lambda: _App()
    R.frame_quality = lambda im, f: None          # stop right after the read: "crop-failed"
    try:
        rec.build_gallery(enroll)
    except RuntimeError as e:
        msg = str(e)
    check("all three images were READ and handed to the engine", len(seen) == 3, seen)
    check("the refusal is about quality, not 'no images'", "No enroll images" not in msg, msg)

    if FAILS:
        print(f"\nNON-ASCII PATH SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nNON-ASCII PATH SELFTEST OK: images round-trip under Cyrillic / Chinese / accented "
          "paths; a failed write is reported; the gallery build reads them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
