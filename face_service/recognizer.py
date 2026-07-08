from __future__ import annotations
import logging
import time
import warnings
from pathlib import Path
from typing import NamedTuple
import numpy as np

from .config import Config, EMBED_PATH, ENROLL_DIR, ADAPTIVE_PATH
from .adaptive import AdaptDecision, AdaptiveStore, evaluate as _adapt_evaluate
from .enroll_qc import FrameQuality, frame_quality, qc_reasons, summarize_rejections
from .liveness import ScreenDetector

log = logging.getLogger(__name__)

# Silence InsightFace's skimage `estimate` FutureWarning (cosmetic only).
warnings.filterwarnings(
    "ignore",
    message=r".*`estimate` is deprecated.*",
    category=FutureWarning,
)

# --- engine identity (used for embeddings.npz versioning) ---
ENGINE_TAG = "insightface-buffalo_l"
EMBED_DIM = 512          # InsightFace ArcFace (w600k_r50) output dimension
DET_SIZE = 640           # detector input square; tune 320/640 for speed vs range
MODEL_NAME = "buffalo_l"

# Modules loaded from buffalo_l. detection + recognition give bbox/embedding; landmark_2d_106
# feeds blink (EAR), landmark_3d_68 gives head pose for gesture challenges. One app.get() now
# yields all of them, so active liveness costs ZERO extra detections (MiniFASNet/DeepFace gone).
ALLOWED_MODULES = ["detection", "recognition", "landmark_2d_106", "landmark_3d_68"]

_CUDA_DLLS_READY = False


def _prep_cuda_dlls() -> None:
    """Make cuDNN 9 sublibraries loadable before any ORT CUDA session is created.

    onnxruntime-gpu's own preload_dlls() loads the top-level CUDA/cuDNN DLLs, but
    cuDNN 9 pulls some sublibraries (e.g. cudnn_engines_tensor_ir64_9.dll) by *name*
    at runtime. For a venv pip install those dirs are not on the Windows DLL search
    path, so the CUDA provider silently falls back to CPU at the first Conv. We add
    every site-packages/nvidia/*/bin to the search path and ctypes-preload all cuDNN
    DLLs by full path (two passes for dependency ordering). Verified required on this
    setup: without it verify_frame runs on CPU despite CUDAExecutionProvider listed.
    """
    global _CUDA_DLLS_READY
    if _CUDA_DLLS_READY:
        return
    import os
    import ctypes

    try:
        import nvidia
    except ImportError:
        log.info("nvidia CUDA pip packages not found; using CPU-only path.")
        _CUDA_DLLS_READY = True
        return

    roots = [Path(p) for p in nvidia.__path__]
    for root in roots:
        for sub in sorted(root.iterdir()):
            b = sub / "bin"
            if b.is_dir():
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(str(b))
                    except OSError:
                        pass
                os.environ["PATH"] = str(b) + os.pathsep + os.environ.get("PATH", "")

    win_dll = getattr(ctypes, "WinDLL", None)
    cudnn_bin = next((r / "cudnn" / "bin" for r in roots if (r / "cudnn" / "bin").is_dir()), None)
    if win_dll is not None and cudnn_bin is not None:
        dlls = sorted(cudnn_bin.glob("*.dll"))
        loaded: set[str] = set()
        for _ in range(2):
            for d in dlls:
                if d.name in loaded:
                    continue
                try:
                    win_dll(str(d))
                    loaded.add(d.name)
                except OSError:
                    pass
        missing = [d.name for d in dlls if d.name not in loaded]
        if missing:
            log.warning("cuDNN preload incomplete: %s (CUDA may fall back to CPU)", missing)
        else:
            log.debug("cuDNN DLLs preloaded: %d", len(loaded))
    _CUDA_DLLS_READY = True


def _select_providers(ort):
    """Return (providers, ctx_id). CUDA if available, else CPU with a warning."""
    avail = ort.get_available_providers()
    if "CUDAExecutionProvider" in avail:
        return ["CUDAExecutionProvider", "CPUExecutionProvider"], 0
    log.warning(
        "CUDAExecutionProvider not available; running InsightFace on CPU (slower). "
        "Available providers: %s", avail,
    )
    return ["CPUExecutionProvider"], -1


class FrameAnalysis(NamedTuple):
    """Everything one detect yields, for the service loop to build a liveness verdict."""
    face: bool                     # a face was detected
    is_match: bool                 # embedding distance <= threshold
    distance: float                # best cosine distance to enrolled refs
    screen: bool | None            # anti-screen: True=screen-like, False=live-like, None=n/a
    landmark: np.ndarray | None    # 2d106 landmarks (blink), or None
    pose: np.ndarray | None        # [pitch, yaw, roll] deg (gesture), or None
    embedding: np.ndarray | None = None  # 512-D ArcFace embedding of the matched face, or None


class EnrollReport(NamedTuple):
    """Result of enroll_from_dir(report=True): the accepted count plus the
    per-frame quality log, for tooling / a future enrollment wizard to display.
    """
    count: int                     # accepted frames (== enroll_from_dir's int return)
    accepted: list                 # list[tuple[str, FrameQuality]] (filename, metrics)
    rejected: list                 # list[tuple[str, str]]          (filename, reason tokens)


class Recognizer:
    """Face recognizer backed by InsightFace (ONNX / onnxruntime-GPU).

    Detection + alignment + 512-D ArcFace embedding + 2d106/3d68 landmarks come from buffalo_l
    on the GPU (graceful CPU fallback). Passive liveness is now the anti-screen check (frequency
    on the face crop); active blink/gesture liveness lives in face_service.liveness and is driven
    by the service loop across frames. DeepFace/MiniFASNet (and TensorFlow/torch) are gone.
    Public interface (enroll_from_dir / load / verify_frame) is unchanged; analyze_frame is new.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._enroll_refs: np.ndarray | None = None    # enrollment baseline, shape (N, 512)
        self._refs: np.ndarray | None = None           # matching set = enrollment + adaptive
        self._adaptive = AdaptiveStore(ADAPTIVE_PATH)  # opt-in drift adaptation (separate persist)
        self._app = None                                # cached insightface FaceAnalysis
        self._screen = ScreenDetector()                 # anti-screen (hf-only, conservative)

    def _refresh_refs(self) -> None:
        """Rebuild the matching set from the enrollment baseline + the adaptive ring."""
        parts = []
        if self._enroll_refs is not None and self._enroll_refs.size:
            parts.append(self._enroll_refs)
        if self._adaptive.count:
            parts.append(self._adaptive.embeddings)
        self._refs = np.vstack(parts) if parts else None

    # ---------- engine ----------

    def _lazy_app(self):
        if self._app is not None:
            return self._app

        _prep_cuda_dlls()  # must run before any CUDA session is created
        import onnxruntime as ort
        try:
            ort.preload_dlls()
        except Exception as e:  # pragma: no cover - defensive
            log.debug("ort.preload_dlls() skipped: %s", e)

        from insightface.app import FaceAnalysis

        providers, ctx_id = _select_providers(ort)
        app = FaceAnalysis(
            name=MODEL_NAME,
            allowed_modules=ALLOWED_MODULES,
            providers=providers,
        )
        app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))

        # Warmup so the first real verify_frame doesn't pay CUDA kernel init: a black-frame
        # get() exercises detection + both landmark models; recognition gets a dummy crop.
        # (No DeepFace/MiniFASNet warmup anymore -> ~5.6s and the TF import are gone.)
        try:
            app.get(np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.uint8))
            rec = app.models.get("recognition")
            if rec is not None:
                rec.get_feat(np.zeros((112, 112, 3), dtype=np.uint8))
        except Exception as e:  # pragma: no cover - warmup is best-effort
            log.debug("engine warmup skipped: %s", e)

        try:
            eff = app.det_model.session.get_providers()
            log.info("InsightFace ready: providers=%s ctx_id=%d det_size=%d modules=%s",
                     eff, ctx_id, DET_SIZE, ALLOWED_MODULES)
        except Exception:
            log.info("InsightFace ready: ctx_id=%d det_size=%d", ctx_id, DET_SIZE)

        self._app = app
        return app

    @staticmethod
    def _largest_face(faces):
        def area(f):
            x1, y1, x2, y2 = f.bbox
            return max(0.0, float(x2 - x1)) * max(0.0, float(y2 - y1))
        return max(faces, key=area)

    # ---------- enrollment ----------

    def enroll_from_dir(self, directory: Path = ENROLL_DIR, *, report: bool = False):
        """Build the enrollment gallery from images in ``directory``.

        Multi-frame: one 512-D embedding per accepted image is stacked, and the
        matcher (analyze_frame) takes the min cosine over all rows. New in Stage 3:
        each frame passes quality control first -- detector confidence, aligned-crop
        sharpness (variance of Laplacian) and exposure -- and every frame's metrics
        are logged. If fewer than ``cfg.enroll_min_frames`` frames pass, this raises
        RuntimeError with an actionable message instead of silently building a weak
        gallery from bad frames.

        Public contract is unchanged: returns the accepted count (int). Pass
        ``report=True`` to get an EnrollReport (count + per-frame quality log) for
        tooling / the future enrollment wizard.
        """
        import cv2
        app = self._lazy_app()
        directory.mkdir(parents=True, exist_ok=True)
        images = [p for p in directory.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
        if not images:
            raise RuntimeError(f"No enroll images in {directory}")

        vecs: list[np.ndarray] = []
        accepted: list[tuple[str, FrameQuality]] = []
        rejected: list[tuple[str, str]] = []
        for p in sorted(images):
            img = cv2.imread(str(p))
            if img is None:
                log.warning("enroll skip %s: cannot read image", p.name)
                rejected.append((p.name, "unreadable"))
                continue
            faces = app.get(img)
            if not faces:
                log.warning("enroll skip %s: no face detected", p.name)
                rejected.append((p.name, "no-face"))
                continue
            face = self._largest_face(faces)
            q = frame_quality(img, face)
            if q is None:
                log.warning("enroll skip %s: face crop unusable", p.name)
                rejected.append((p.name, "crop-failed"))
                continue
            reasons = qc_reasons(q, self.cfg)
            if reasons:
                why = ",".join(reasons)
                log.info("enroll DROP %s: %s (det=%.3f sharp=%.1f luma=%.1f facepx=%d)",
                         p.name, why, q.det, q.sharpness, q.luma, q.face_px)
                rejected.append((p.name, why))
                continue
            vecs.append(np.asarray(face.normed_embedding, dtype=np.float32))
            accepted.append((p.name, q))
            log.info("enroll OK %s: det=%.3f sharp=%.1f luma=%.1f facepx=%d",
                     p.name, q.det, q.sharpness, q.luma, q.face_px)

        min_frames = int(self.cfg.enroll_min_frames)
        if len(vecs) < min_frames:
            raise RuntimeError(
                f"enrollment rejected: only {len(vecs)} of {len(images)} image(s) passed "
                f"quality control (need >= {min_frames}). Dropped: "
                f"{summarize_rejections(rejected)}. Re-capture with steady focus, the face "
                f"filling the frame, and even lighting (avoid strong backlight)."
            )

        embeds = np.stack(vecs, axis=0)
        EMBED_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            EMBED_PATH,
            embeddings=embeds,
            engine=np.array(ENGINE_TAG),
            dim=np.array(EMBED_DIM, dtype=np.int64),
        )
        self._enroll_refs = embeds
        # A fresh baseline invalidates prior drift adaptation -> reset it (enrollment is the anchor).
        self._adaptive.clear()
        self._refresh_refs()
        log.info("enrollment built: %d accepted, %d rejected of %d image(s); adaptive gallery reset",
                 len(vecs), len(rejected), len(images))
        if report:
            return EnrollReport(count=len(vecs), accepted=accepted, rejected=rejected)
        return len(vecs)

    def load(self) -> bool:
        if not EMBED_PATH.exists():
            # No enrollment baseline -> don't leave an orphan adaptive ring on disk (it is
            # meaningless without an anchor and would confuse a later inspection). F3.
            self._adaptive.clear()
            return False
        data = np.load(EMBED_PATH, allow_pickle=False)
        if "engine" not in data.files or "dim" not in data.files:
            log.warning(
                "%s has no engine tag (old DeepFace enrollment) and is incompatible "
                "with InsightFace. Re-enroll required.", EMBED_PATH.name,
            )
            return False
        engine = str(data["engine"])
        dim = int(data["dim"])
        if engine != ENGINE_TAG or dim != EMBED_DIM:
            log.warning(
                "%s built by engine=%r dim=%d; current engine=%r dim=%d. Incompatible, "
                "re-enroll required.", EMBED_PATH.name, engine, dim, ENGINE_TAG, EMBED_DIM,
            )
            return False
        refs = data["embeddings"]
        if refs.ndim != 2 or refs.shape[1] != EMBED_DIM:
            log.warning("%s has unexpected shape %s; re-enroll required.",
                        EMBED_PATH.name, tuple(refs.shape))
            return False
        self._enroll_refs = refs.astype(np.float32)
        # Load the opt-in adaptive ring ONLY when the feature is enabled, so turning the
        # toggle off reverts matching to the pure enrollment baseline. The file stays on
        # disk for a later re-enable; clear_adaptive() (or re-enroll) is the hard rollback. F2.
        if self.cfg.adaptive_gallery:
            try:
                if self._adaptive.load():
                    log.info("adaptive gallery loaded: %d embedding(s)", self._adaptive.count)
            except Exception as e:  # pragma: no cover - defensive
                log.warning("adaptive gallery load skipped: %s", e)
        self._refresh_refs()
        return True

    # ---------- verification ----------

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = a / (np.linalg.norm(a) + 1e-9)
        nb = b / (np.linalg.norm(b) + 1e-9)
        return float(1.0 - np.dot(na, nb))  # cosine *distance*

    def analyze_frame(self, bgr: np.ndarray) -> FrameAnalysis:
        """One detect -> match/distance + landmarks + pose + anti-screen. For the service loop
        to accumulate a multi-frame liveness verdict (blink/gesture/anti-screen). Single detect;
        landmarks and pose ride along on the same face object.
        """
        if self._refs is None and not self.load():
            raise RuntimeError("No enrollment found. Run enroll first.")

        app = self._lazy_app()
        try:
            faces = app.get(bgr)
        except Exception as e:
            log.debug("insightface get failed: %s", e)
            return FrameAnalysis(False, False, 1.0, None, None, None)
        if not faces:
            return FrameAnalysis(False, False, 1.0, None, None, None)

        face = self._largest_face(faces)
        emb = np.asarray(face.normed_embedding, dtype=np.float32)
        best = min(self._cosine(emb, r) for r in self._refs)  # type: ignore[union-attr]
        is_match = best <= self.cfg.threshold

        # Passive anti-screen on the face crop (conservative hf gate; None if crop unusable).
        screen = None
        if getattr(self.cfg, "anti_screen", True):
            try:
                screen = self._screen.check(bgr, face.bbox)
            except Exception as e:
                log.debug("anti-screen check failed: %s", e)

        landmark = face.get("landmark_2d_106") if hasattr(face, "get") else None
        pose = face.get("pose") if hasattr(face, "get") else None
        return FrameAnalysis(True, is_match, best, screen, landmark, pose, emb)

    def verify_frame(self, bgr: np.ndarray) -> tuple[bool, float, bool]:
        """Return (is_match, best_distance, is_real). Compat wrapper over analyze_frame.

        is_real = passive per-frame liveness (anti-screen: not screen-like). As in the stock
        engine, a liveness failure forces match=False so callers that gate on the match alone
        (unlock) still refuse spoofs. Active blink/gesture liveness is applied by the service
        loop across frames, not here (single frame can't observe a blink).
        """
        a = self.analyze_frame(bgr)
        if not a.face:
            return False, 1.0, False
        is_real = a.screen is not True   # True=screen -> not real; False/None -> real
        if not is_real:
            return False, a.distance, False
        return a.is_match, a.distance, True

    # ---------- adaptive gallery ----------

    def maybe_adapt(self, embedding, *, liveness_passed, is_screen, mode,
                    gesture_passed: bool = False, now: float | None = None,
                    union_distance: float | None = None) -> AdaptDecision:
        """Offer a just-verified embedding to the adaptive gallery.

        The anti-poisoning gate is anchored to the ENROLLMENT BASELINE: the ceiling
        is checked against this frame's distance to enrollment only (recomputed
        here), NOT the adaptive-augmented distance the unlock used -- so a stored
        embedding can never hop outward from the original enrollment via earlier
        adaptive rows. On acceptance the embedding is appended (FIFO-capped),
        persisted to the SEPARATE adaptive file, and the matching set is refreshed.
        Best-effort and side-effect-safe: always returns the decision (with a reason
        token) for the audit log.
        """
        if now is None:
            now = time.time()
        ceiling = float(self.cfg.threshold) - float(self.cfg.adaptive_margin)
        if self._enroll_refs is None or self._enroll_refs.size == 0 or embedding is None:
            return AdaptDecision(False, "no-enroll", ceiling)
        emb = np.asarray(embedding, dtype=np.float32)
        dist_to_enroll = min(self._cosine(emb, r) for r in self._enroll_refs)
        dec = _adapt_evaluate(
            dist_to_enroll, self.cfg.threshold, self.cfg,
            liveness_passed=liveness_passed, is_screen=is_screen, mode=mode,
            gesture_passed=gesture_passed, now=now,
            last_adapt_ts=self._adaptive.last_adapt_ts, adaptive_count=self._adaptive.count,
        )
        if dec.accept:
            # Snapshot so a failed save() rolls the in-memory append back: memory must stay
            # == disk, and a phantom in-RAM add would also wrongly advance the cooldown. F5.
            prev_emb, prev_ts = self._adaptive.embeddings, self._adaptive.ts
            self._adaptive.add(emb, now, self.cfg.adaptive_max_size)
            try:
                self._adaptive.save()
            except Exception as e:  # pragma: no cover - defensive (disk full / perms)
                self._adaptive.embeddings, self._adaptive.ts = prev_emb, prev_ts
                log.warning("adaptive save failed (%s); rolled back in-memory add", e)
                return AdaptDecision(False, "save-failed", dec.ceiling)
            self._refresh_refs()
            log.info("adaptive: +1 (enroll-dist=%.3f <= ceil=%.3f, unlock-dist=%s); store=%d/%d",
                     dist_to_enroll, dec.ceiling,
                     ("%.3f" % union_distance) if union_distance is not None else "?",
                     self._adaptive.count, int(self.cfg.adaptive_max_size))
        else:
            log.debug("adaptive: skip (%s) enroll-dist=%.3f ceil=%.3f",
                      dec.reason, dist_to_enroll, dec.ceiling)
        return dec

    def clear_adaptive(self) -> None:
        """Roll back all drift adaptation (delete the adaptive file); enrollment stays."""
        self._adaptive.clear()
        self._refresh_refs()
        log.info("adaptive gallery cleared")
