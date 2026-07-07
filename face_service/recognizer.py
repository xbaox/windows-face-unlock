from __future__ import annotations
import logging
import warnings
from pathlib import Path
import numpy as np

from .config import Config, EMBED_PATH, ENROLL_DIR

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


def _lazy_deepface():
    # Imported lazily because TensorFlow import is slow and heavy.
    from deepface import DeepFace  # type: ignore
    return DeepFace


class Recognizer:
    """Face recognizer backed by InsightFace (ONNX / onnxruntime-GPU).

    Detection + alignment + 512-D ArcFace embedding come from buffalo_l on the GPU
    (graceful CPU fallback). Passive liveness still runs through DeepFace/MiniFASNet
    on this stage; active liveness is stage 2. Public interface
    (enroll_from_dir / load / verify_frame) is unchanged from the DeepFace version.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._refs: np.ndarray | None = None  # shape (N, 512)
        self._app = None                       # cached insightface FaceAnalysis

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
            allowed_modules=["detection", "recognition"],
            providers=providers,
        )
        app.prepare(ctx_id=ctx_id, det_size=(DET_SIZE, DET_SIZE))

        # Warmup so the first real verify_frame doesn't pay CUDA kernel init:
        # detection on a black frame + recognition on a dummy aligned crop.
        try:
            app.get(np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.uint8))
            rec = app.models.get("recognition")
            if rec is not None:
                rec.get_feat(np.zeros((112, 112, 3), dtype=np.uint8))
        except Exception as e:  # pragma: no cover - warmup is best-effort
            log.debug("engine warmup skipped: %s", e)

        # Warm up passive liveness too (DeepFace/MiniFASNet + TF load lazily on
        # the first real frame otherwise, adding a ~6s spike to verify #1).
        if self.cfg.anti_spoofing:
            try:
                DeepFace = _lazy_deepface()
                DeepFace.extract_faces(
                    img_path=np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.uint8),
                    detector_backend=self.cfg.detector_backend,
                    anti_spoofing=True,
                    enforce_detection=False,
                )
            except Exception as e:  # pragma: no cover - warmup is best-effort
                log.debug("liveness warmup skipped: %s", e)

        try:
            eff = app.det_model.session.get_providers()
            log.info("InsightFace ready: providers=%s ctx_id=%d det_size=%d", eff, ctx_id, DET_SIZE)
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

    def enroll_from_dir(self, directory: Path = ENROLL_DIR) -> int:
        import cv2
        app = self._lazy_app()
        directory.mkdir(parents=True, exist_ok=True)
        images = [p for p in directory.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
        if not images:
            raise RuntimeError(f"No enroll images in {directory}")

        vecs: list[np.ndarray] = []
        for p in sorted(images):
            img = cv2.imread(str(p))
            if img is None:
                log.warning("skip %s: cannot read image", p.name)
                continue
            faces = app.get(img)
            if not faces:
                log.warning("skip %s: no face detected", p.name)
                continue
            face = self._largest_face(faces)
            vecs.append(np.asarray(face.normed_embedding, dtype=np.float32))
            log.info("enrolled %s (det_score=%.3f)", p.name, float(face.det_score))

        if not vecs:
            raise RuntimeError("No face found in enroll images")
        embeds = np.stack(vecs, axis=0)
        EMBED_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.savez(
            EMBED_PATH,
            embeddings=embeds,
            engine=np.array(ENGINE_TAG),
            dim=np.array(EMBED_DIM, dtype=np.int64),
        )
        self._refs = embeds
        return len(vecs)

    def load(self) -> bool:
        if not EMBED_PATH.exists():
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
        self._refs = refs.astype(np.float32)
        return True

    # ---------- verification ----------

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = a / (np.linalg.norm(a) + 1e-9)
        nb = b / (np.linalg.norm(b) + 1e-9)
        return float(1.0 - np.dot(na, nb))  # cosine *distance*

    def verify_frame(self, bgr: np.ndarray) -> tuple[bool, float, bool]:
        """Return (is_match, best_distance, is_real). is_real False if anti-spoofing flagged."""
        if self._refs is None and not self.load():
            raise RuntimeError("No enrollment found. Run enroll first.")

        # 1) liveness (unchanged: DeepFace MiniFASNet). Early-exit preserved.
        is_real = True
        if self.cfg.anti_spoofing:
            DeepFace = _lazy_deepface()
            try:
                faces = DeepFace.extract_faces(
                    img_path=bgr,
                    detector_backend=self.cfg.detector_backend,
                    anti_spoofing=True,
                    enforce_detection=True,
                )
                if not faces:
                    return False, 1.0, False
                is_real = bool(faces[0].get("is_real", True))
                if not is_real:
                    return False, 1.0, False
            except Exception as e:
                log.warning("liveness check failed: %s", e)
                return False, 1.0, False

        # 2) embedding via InsightFace (GPU), cosine distance to enrolled refs.
        app = self._lazy_app()
        try:
            faces = app.get(bgr)
        except Exception as e:
            log.debug("insightface get failed: %s", e)
            return False, 1.0, is_real
        if not faces:
            return False, 1.0, is_real

        face = self._largest_face(faces)
        emb = np.asarray(face.normed_embedding, dtype=np.float32)
        dists = [self._cosine(emb, r) for r in self._refs]  # type: ignore[union-attr]
        best = min(dists)
        return best <= self.cfg.threshold, best, is_real
