"""Lightweight YuNet face *detector* (not recognizer).

Used by presence-mode ``detection``: the old AutoFaceLock behaviour where any
human face in front of the camera is enough to stay unlocked — no DeepFace,
no enrollment match. Cheap and fast; no TensorFlow/torch needed.

Stage 9 (R7): the model is pinned (face_service/model_pins.py) and ships only in this project's
``models/`` directory (MIT, models/LICENSE-yunet). The fallback into an upstream product's
install directory (facewinunlock-tauri) is gone: a model found there was unverified.
"""
from __future__ import annotations
import logging
from pathlib import Path

import cv2
import numpy as np

from .model_pins import YUNET_FILE, check_yunet

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUNDLED_MODEL = _REPO_ROOT / "models" / YUNET_FILE


class DetectorUnavailable(RuntimeError):
    """YuNet cannot be used: the model is missing, not the pinned one, or OpenCV refused it."""


def yunet_model_path() -> Path:
    """The pinned YuNet model, or DetectorUnavailable with the reason."""
    problems = check_yunet(_BUNDLED_MODEL)
    if problems:
        raise DetectorUnavailable(f"YuNet model unusable ({'; '.join(problems)}); "
                                  f"expected at {_BUNDLED_MODEL.parent.name}/{YUNET_FILE}")
    return _BUNDLED_MODEL


class FaceDetector:
    """Any-face detector backed by OpenCV's YuNet."""

    def __init__(
        self,
        score_threshold: float = 0.7,
        nms_threshold: float = 0.3,
        top_k: int = 50,
    ):
        self.score_threshold = score_threshold
        self.nms_threshold = nms_threshold
        self.top_k = top_k
        self._impl: cv2.FaceDetectorYN | None = None
        self._size: tuple[int, int] | None = None
        self._failed: str | None = None     # first failure, logged ONCE at ERROR (F-191)

    def _ensure(self, width: int, height: int) -> cv2.FaceDetectorYN:
        if self._impl is None:
            try:
                self._impl = cv2.FaceDetectorYN.create(
                    str(yunet_model_path()),
                    "",
                    (width, height),
                    self.score_threshold,
                    self.nms_threshold,
                    self.top_k,
                )
            except Exception as e:
                # Stage 9 (F-191): a broken detector used to be swallowed at DEBUG by its callers,
                # so the wizard said "no face" forever. Say it once, loudly, and let the caller
                # show "face detector unavailable".
                why = str(e) or repr(e)
                if self._failed is None:
                    log.error("face detector unavailable: %s", why)
                self._failed = why
                raise DetectorUnavailable(why) from e
            self._size = (width, height)
        elif self._size != (width, height):
            self._impl.setInputSize((width, height))
            self._size = (width, height)
        return self._impl

    @property
    def unavailable(self) -> "str | None":
        """The reason the detector could not be built, or None."""
        return self._failed

    def has_face(self, bgr: np.ndarray) -> bool:
        h, w = bgr.shape[:2]
        det = self._ensure(w, h)
        _, faces = det.detect(bgr)
        return faces is not None and len(faces) > 0
