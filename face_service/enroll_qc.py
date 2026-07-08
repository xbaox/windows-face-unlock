"""Enrollment-frame quality control (Stage 3 / Step 1).

Pure, engine-agnostic helpers. Given an already-detected InsightFace ``face`` and
the source BGR image, compute per-frame quality metrics and decide whether the
frame is good enough to contribute an embedding. Kept separate from Recognizer so
the gates are unit-testable without a camera or the GPU engine (see
``tools.enroll_qc_selftest``), and so ``tools.enroll_qc_probe`` and
``Recognizer.enroll_from_dir`` share ONE source of truth for the thresholds.

All metrics are computed on the canonical 112x112 aligned crop that ArcFace
embeds (``insightface.utils.face_align.norm_crop`` on the 5 keypoints), so
"sharpness" and "exposure" describe exactly what the recognizer sees:

  det          detector confidence (``face.det_score``)
  face_px      face bbox short side in px, at source resolution      -- telemetry
  sharpness    variance of the Laplacian on the crop (higher = sharper)
  luma         mean grayscale intensity of the crop (0..255)
  dark_frac    fraction of near-black pixels (gray < 16)             -- telemetry
  bright_frac  fraction of clipped-white pixels (gray > 240)         -- telemetry

Enforced gates come from ``Config`` (locked to this webcam's measured enroll set,
det_score 0.83-0.89 / sharpness 151-321 / luma 82-131 / clip 0%):

  det >= enroll_min_det_score
  sharpness >= enroll_min_sharpness
  enroll_luma_min <= luma <= enroll_luma_max

``face_px`` and the clip fractions are reported for the quality log but are not
hard gates (measured 0% on the good set; the sharpness + luma gates already cover
the realistic bad frames). If a clip/size gate ever proves necessary it gets
added on numbers, not guesses.
"""
from __future__ import annotations

from collections import Counter
from typing import NamedTuple

ALIGN_SIZE = 112  # ArcFace canonical crop; the sharpness floor is calibrated to this size.


class FrameQuality(NamedTuple):
    det: float
    face_px: int
    sharpness: float
    luma: float
    dark_frac: float
    bright_frac: float


def aligned_crop(img, face, size: int = ALIGN_SIZE):
    """Return the canonical aligned crop ArcFace embeds, or None if unusable.

    Uses norm_crop on the 5 detected keypoints (identical to the recognition
    pipeline). Falls back to a resized bbox crop when keypoints/face_align are
    unavailable, so callers still get a number rather than crashing.
    """
    try:
        from insightface.utils import face_align
        kps = getattr(face, "kps", None)
        if kps is not None:
            return face_align.norm_crop(img, landmark=kps, image_size=size)
    except Exception:
        pass
    import cv2
    x1, y1, x2, y2 = (int(v) for v in face.bbox)
    x1, y1 = max(0, x1), max(0, y1)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (size, size))


def frame_quality(img, face) -> "FrameQuality | None":
    """Compute FrameQuality for a detected face, or None if the crop is unusable."""
    import cv2
    crop = aligned_crop(img, face)
    if crop is None:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    luma = float(gray.mean())
    dark_frac = float((gray < 16).mean())
    bright_frac = float((gray > 240).mean())
    x1, y1, x2, y2 = (float(v) for v in face.bbox)
    face_px = int(min(x2 - x1, y2 - y1))
    return FrameQuality(float(face.det_score), face_px, sharpness, luma, dark_frac, bright_frac)


def qc_reasons(q: FrameQuality, cfg) -> list[str]:
    """Reasons ``q`` fails QC under ``cfg`` (empty list == the frame passes).

    Tokens are short and stable (e.g. ``blur<80``) so they aggregate cleanly in
    ``summarize_rejections`` and read well in the audit/service log.
    """
    reasons: list[str] = []
    if q.det < cfg.enroll_min_det_score:
        reasons.append(f"det<{cfg.enroll_min_det_score:g}")
    if q.sharpness < cfg.enroll_min_sharpness:
        reasons.append(f"blur<{cfg.enroll_min_sharpness:g}")
    if q.luma < cfg.enroll_luma_min:
        reasons.append(f"dark<{cfg.enroll_luma_min:g}")
    if q.luma > cfg.enroll_luma_max:
        reasons.append(f"bright>{cfg.enroll_luma_max:g}")
    return reasons


def passes(q: FrameQuality, cfg) -> bool:
    return not qc_reasons(q, cfg)


def summarize_rejections(rejected) -> str:
    """Aggregate a list of (name, reason) drops into a compact 'tok xN, ...' string.

    ``reason`` may hold several comma-joined tokens (e.g. ``blur<80,dark<55``);
    each token is counted independently so the message reads like
    ``blur<80 x2, no-face x1``.
    """
    counts: Counter[str] = Counter()
    for _name, reason in rejected:
        for tok in str(reason).split(","):
            tok = tok.strip()
            if tok:
                counts[tok] += 1
    if not counts:
        return "none"
    return ", ".join(f"{tok} x{n}" for tok, n in counts.most_common())
