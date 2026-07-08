"""tools/enroll_qc_selftest.py -- Stage 3 / Step 1 QC logic self-test (no camera/GPU).

Proves the enrollment quality gates behave: a clean synthetic crop passes;
blurred / dark / washed-out / low-confidence crops are dropped for the right
reason; and a mostly-bad set falls below enroll_min_frames (so enroll_from_dir
would raise its clear "too few usable frames" error). Exercises the exact
face_service.enroll_qc functions enroll_from_dir uses -- no insightface, no GPU.

Run from the repo root:
    python -m tools.enroll_qc_selftest
"""
from __future__ import annotations

import numpy as np

from face_service.config import Config
from face_service import enroll_qc


class _Face:
    """Minimal stand-in for an InsightFace face: det_score + bbox, no kps.

    With kps absent, enroll_qc.aligned_crop takes its bbox-crop fallback, so the
    metrics see exactly the pixels we synthesize here.
    """
    def __init__(self, det, bbox):
        self.det_score = float(det)
        self.bbox = bbox
        self.kps = None


BBOX = [20, 20, 180, 180]


def _make(kind):
    import cv2
    rng = np.random.default_rng(abs(hash(kind)) % (2 ** 32))
    if kind == "sharp":
        img = rng.integers(40, 215, (200, 200, 3), dtype=np.uint8)   # texture, mid exposure
        return img, _Face(0.88, BBOX)
    if kind == "blurry":
        img = rng.integers(40, 215, (200, 200, 3), dtype=np.uint8)
        img = cv2.GaussianBlur(img, (0, 0), sigmaX=6)                # kill high frequency
        return img, _Face(0.88, BBOX)
    if kind == "dark":
        return np.full((200, 200, 3), 10, np.uint8), _Face(0.88, BBOX)   # luma ~10
    if kind == "bright":
        return np.full((200, 200, 3), 250, np.uint8), _Face(0.88, BBOX)  # luma ~250
    if kind == "lowdet":
        img = rng.integers(40, 215, (200, 200, 3), dtype=np.uint8)   # sharp + exposed, but
        return img, _Face(0.30, BBOX)                                # detector unsure
    raise ValueError(kind)


# kind -> substrings that MUST appear among the drop reasons ([] means "must pass")
CASES = {
    "sharp": [],
    "blurry": ["blur"],
    "dark": ["dark"],
    "bright": ["bright"],
    "lowdet": ["det"],
}


def main(argv=None) -> int:
    cfg = Config()  # defaults == the locked Stage-3 gates
    print(f"gates: det>={cfg.enroll_min_det_score:g} sharp>={cfg.enroll_min_sharpness:g} "
          f"luma {cfg.enroll_luma_min:g}-{cfg.enroll_luma_max:g} need>={cfg.enroll_min_frames}\n")

    failures = 0
    accepted = 0
    for kind, expect in CASES.items():
        img, face = _make(kind)
        q = enroll_qc.frame_quality(img, face)
        reasons = enroll_qc.qc_reasons(q, cfg)
        got = ",".join(reasons) if reasons else "ok"
        print(f"{kind:8} det={q.det:.2f} sharp={q.sharpness:9.1f} luma={q.luma:6.1f} "
              f"lo={q.dark_frac * 100:4.0f}% hi={q.bright_frac * 100:4.0f}%  -> {got}")
        if not expect:
            if reasons:
                print(f"  FAIL: clean frame should pass, got {reasons}")
                failures += 1
            else:
                accepted += 1
        else:
            if not reasons:
                print("  FAIL: bad frame should have been dropped")
                failures += 1
            elif not all(any(tok in r for r in reasons) for tok in expect):
                print(f"  FAIL: expected a reason containing each of {expect}, got {reasons}")
                failures += 1

    n_bad = sum(1 for e in CASES.values() if e)
    print(f"\naccepted {accepted}/{len(CASES)} synthetic frames; a set of the {n_bad} bad "
          f"frames yields 0 accepted < need {cfg.enroll_min_frames} -> enroll_from_dir "
          f"would raise its clear 'too few usable frames' error.")

    summary = enroll_qc.summarize_rejections(
        [("a.jpg", "blur<80,dark<55"), ("b.jpg", "no-face"), ("c.jpg", "blur<80")]
    )
    print(f"summarize_rejections demo: {summary}")
    if "blur<80 x2" not in summary:
        print("  FAIL: summarize_rejections did not aggregate the blur count")
        failures += 1

    # --- G6: aligned_crop uses the keypoint (norm_crop) path when kps are present ---
    print("\n[G6] keypoint path of aligned_crop (norm_crop), not just the bbox fallback")
    try:
        from insightface.utils import face_align as _fa  # noqa: F401
    except Exception as e:   # pragma: no cover - insightface not installed in this context
        print(f"  skip  insightface unavailable ({e.__class__.__name__}); G6 skipped")
    else:
        class _FaceKps:
            def __init__(self, det, bbox, kps):
                self.det_score = float(det)
                self.bbox = bbox
                self.kps = kps
        # DEGENERATE bbox on purpose: the bbox fallback would return None (empty crop), so a
        # non-None crop PROVES norm_crop (the kps path) ran instead of the fallback.
        kps = np.array([[40, 45], [72, 45], [56, 64], [44, 82], [68, 82]], dtype=np.float32)
        img = np.random.default_rng(1).integers(40, 215, (112, 112, 3), dtype=np.uint8)
        face = _FaceKps(0.88, [10, 10, 10, 10], kps)   # degenerate bbox: fallback would fail
        crop = enroll_qc.aligned_crop(img, face)
        ok_c = crop is not None and crop.shape == (112, 112, 3)
        print(("  ok  " if ok_c else "  FAIL") + "  norm_crop returns a 112x112 crop despite a degenerate bbox")
        failures += 0 if ok_c else 1
        ok_q = enroll_qc.frame_quality(img, face) is not None
        print(("  ok  " if ok_q else "  FAIL") + "  frame_quality via the kps path returns metrics")
        failures += 0 if ok_q else 1

    # --- G7: enroll_from_dir end-to-end min_frames gate (stubbed engine, temp paths) ---
    print("\n[G7] enroll_from_dir end-to-end (min_frames gate)")
    import tempfile
    import cv2
    from pathlib import Path
    from face_service.recognizer import Recognizer
    import face_service.recognizer as _RC
    from face_service.adaptive import AdaptiveStore

    class _FakeFace:
        def __init__(self, det, bbox, emb):
            self.det_score = float(det)
            self.bbox = bbox
            self.kps = None
            self.normed_embedding = emb

    class _FakeApp:
        def get(self, img):
            e = np.zeros(512, np.float32)
            e[0] = 1.0
            return [_FakeFace(0.88, [20, 20, 180, 180], e)]

    def _write_img(path, kind):
        rng = np.random.default_rng(abs(hash((kind, path.name))) % (2 ** 32))
        if kind == "good":
            im = rng.integers(40, 215, (200, 200, 3), dtype=np.uint8)   # sharp + mid exposure
        else:  # dark
            im = np.full((200, 200, 3), 10, np.uint8)                   # luma ~10 -> dropped
        cv2.imwrite(str(path), im)

    orig_embed = _RC.EMBED_PATH
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        endir = tdp / "enroll"
        endir.mkdir()
        try:
            _RC.EMBED_PATH = tdp / "embeddings.npz"
            # (A) 3 good + 2 dark -> success, exactly 3 accepted, 2 rejected
            for i in range(3):
                _write_img(endir / f"g{i}.png", "good")
            for i in range(2):
                _write_img(endir / f"d{i}.png", "dark")
            rec = Recognizer(cfg)
            rec._app = _FakeApp()                          # bypass the GPU engine
            rec._adaptive = AdaptiveStore(tdp / "adaptive.npz")   # keep off the real path
            rep = rec.enroll_from_dir(endir, report=True)
            ok_a = rep.count == 3 and len(rep.rejected) == 2 and _RC.EMBED_PATH.exists()
            print(("  ok  " if ok_a else "  FAIL") +
                  f"  3 good + 2 dark -> {rep.count} accepted, {len(rep.rejected)} rejected (need 3)")
            failures += 0 if ok_a else 1
            # (B) 2 good + 3 dark -> RuntimeError below min_frames=3
            for p in endir.iterdir():
                p.unlink()
            for i in range(2):
                _write_img(endir / f"g{i}.png", "good")
            for i in range(3):
                _write_img(endir / f"d{i}.png", "dark")
            rec = Recognizer(cfg)
            rec._app = _FakeApp()
            rec._adaptive = AdaptiveStore(tdp / "adaptive2.npz")
            raised = ""
            try:
                rec.enroll_from_dir(endir)
            except RuntimeError as e:
                raised = str(e)
            ok_b = ("only 2" in raised) and ("need >= 3" in raised)
            print(("  ok  " if ok_b else "  FAIL") +
                  f"  2 good + 3 dark -> RuntimeError ({'message ok' if ok_b else raised[:70]!r})")
            failures += 0 if ok_b else 1
        finally:
            _RC.EMBED_PATH = orig_embed

    # --- G8: luma gate boundary is strict (</>) and crop-unusable -> None -> 'crop-failed' ---
    print("\n[G8] luma boundary (strict) + crop-failed")
    FQ = enroll_qc.FrameQuality

    def _reasons(luma, sharp=100.0, det=0.88):
        return ",".join(enroll_qc.qc_reasons(FQ(det, 160, sharp, luma, 0.0, 0.0), cfg))

    g8 = [
        ("luma == 55.0 passes the dark gate", "dark" not in _reasons(55.0)),
        ("luma == 210.0 passes the bright gate", "bright" not in _reasons(210.0)),
        ("luma 54.999 -> dark", "dark" in _reasons(54.999)),
        ("luma 210.001 -> bright", "bright" in _reasons(210.001)),
        ("degenerate bbox (no kps) -> frame_quality None => 'crop-failed'",
         enroll_qc.frame_quality(np.zeros((50, 50, 3), np.uint8), _Face(0.88, [0, 0, 0, 0])) is None),
    ]
    for msg, cond in g8:
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        failures += 0 if cond else 1

    if failures:
        print(f"\nSELFTEST FAILED: {failures} check(s) failed.")
        return 1
    print("\nSELFTEST OK: QC drops bad frames, accepts clean ones, refuses a bad set.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
