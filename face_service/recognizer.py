from __future__ import annotations
import logging
import sys
import time
import warnings
from pathlib import Path
from typing import NamedTuple
import numpy as np

from .config import Config, EMBED_PATH, ENROLL_DIR, ADAPTIVE_PATH
from .adaptive import (AdaptDecision, AdaptiveStore, EMBED_DIM, ENGINE_TAG, finite_rows,
                       write_npz_atomic, evaluate as _adapt_evaluate)
from .enroll_qc import FrameQuality, frame_quality, qc_reasons, summarize_rejections
from .liveness import ScreenDetector, compute_ear
from . import imio
from .model_pins import BUFFALO_FILES, PACK_NAME, check_pack

log = logging.getLogger(__name__)

# Silence InsightFace's skimage `estimate` FutureWarning (cosmetic only).
warnings.filterwarnings(
    "ignore",
    message=r".*`estimate` is deprecated.*",
    category=FutureWarning,
)

# --- engine identity (used for embeddings.npz versioning) ---
# ENGINE_TAG / EMBED_DIM come from adaptive.py (one definition, D-79).
DET_SIZE = 640           # detector input square; tune 320/640 for speed vs range
MODEL_NAME = PACK_NAME

# Modules loaded from buffalo_l. detection + recognition give bbox/embedding; landmark_2d_106
# feeds blink (EAR), landmark_3d_68 gives head pose for gesture challenges. One app.get() now
# yields all of them, so active liveness costs ZERO extra detections (MiniFASNet/DeepFace gone).
ALLOWED_MODULES = ["detection", "recognition", "landmark_2d_106", "landmark_3d_68"]

# --- model provisioning (Stage 7d-F; Stage 9, act 9b R7) -------------------------------------
# The five files of buffalo_l, pinned in face_service/model_pins.py (the one source of the pins).
# FOUR are load-bearing (ALLOWED_MODULES above); genderage.onnx is discarded by that filter but must
# still be PRESENT, because FaceAnalysis globs every *.onnx in the directory and builds a full ORT
# session for each one BEFORE the filter runs.
MODEL_FILES = tuple(BUFFALO_FILES)

# How long to sit out after a failed engine build before trying again. Without this a machine with
# no model pack re-entered _lazy_app for EVERY captured frame.
_MODEL_RETRY_COOLDOWN_S = 60.0


class ModelsUnavailable(RuntimeError):
    """The pinned buffalo_l pack is not in place (missing, extra or altered files). The service
    turns this into its "no-models" refusal; nothing is downloaded at run time (decision 9-02)."""


def _bundle_dir() -> Path:
    """Directory that holds bundled resources: the PyInstaller temp root when frozen, else repo."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent.parent


def model_root() -> "Path | None":
    """The ``root=`` to hand FaceAnalysis, or None to accept its own default.

    insightface resolves the pack as ``<root>/models/<name>``. Stage 9 (R7): an installed product
    keeps the pack the INSTALLER downloaded and verified in ``{app}\\models\\buffalo_l`` -- the
    directory of the executables -- and never in the bundle (buffalo_l is not redistributed,
    decision 9-02). A dev checkout keeps the library default (``~/.insightface``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def model_dir() -> Path:
    """Absolute directory the five model files must live in, for the current layout."""
    root = model_root()
    if root is None:
        root = Path.home() / ".insightface"   # the library default, made explicit for the check
    return root / "models" / MODEL_NAME


def missing_model_files() -> "list[str]":
    """Names of the required buffalo_l files that are not on disk. Empty list = all present."""
    here = model_dir()
    return [name for name in MODEL_FILES if not (here / name).is_file()]


def model_problems(hashes: bool = True) -> "list[str]":
    """Why the pack cannot be used; [] when it is exactly the pinned pack (R7)."""
    return check_pack(model_dir(), hashes=hashes)


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


class EngineError(Exception):
    """Stage 8b (F-22): the engine could not judge a frame (empty input, or InsightFace/ONNX
    raised). Deliberately NOT a RuntimeError: RuntimeError is what analyze_frame raises for "no
    enrollment", which the gesture round treats as an engine refusal of the whole round, whereas
    this is a per-frame fault that every caller skips."""


class FrameAnalysis(NamedTuple):
    """Everything one detect yields, for the service loop to build a liveness verdict."""
    face: bool                     # a face was detected
    is_match: bool                 # embedding distance <= threshold
    distance: float                # best cosine distance to enrolled refs
    screen: bool | None            # anti-screen: True=screen-like, False=live-like, None=n/a
    landmark: np.ndarray | None    # 2d106 landmarks (blink), or None
    pose: np.ndarray | None        # [pitch, yaw, roll] deg (gesture), or None
    embedding: np.ndarray | None = None  # 512-D ArcFace embedding of the matched face, or None
    screen_features: "object | None" = None  # ScreenFeatures (hf, peak, lap) -- telemetry (R6)


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
        self._app_failed_at = 0.0                       # monotonic; 0.0 = never failed to build
        self._app_error = ""                            # last build failure, replayed in cooldown
        self._screen = ScreenDetector()                 # anti-screen (hf-only, conservative)
        self.last_enroll_pose = None                    # 8b D-17: median pose of the last build
        self.last_enroll_info: dict = {}                # Stage 9 (R6): build telemetry
        self._incompatible: str | None = None           # F-125: a gallery that needs re-enroll
        self._screen_error_logged = False               # F-124: once per episode

    def _refresh_refs(self) -> None:
        """Rebuild the matching set from the enrollment baseline + the adaptive ring.

        Stage 9 (D-40): the ring rows join the matching set only while ``adaptive_gallery`` is
        on. The ring itself stays loaded either way, so switching the toggle at run time (reload)
        adds or drops them at once, and a false -> true cycle no longer overwrites the stored
        ring with a one-row file."""
        parts = []
        if self._enroll_refs is not None and self._enroll_refs.size:
            parts.append(self._enroll_refs)
        if self._adaptive.count and getattr(self.cfg, "adaptive_gallery", False):
            parts.append(self._adaptive.embeddings)
        self._refs = np.vstack(parts) if parts else None

    # ---------- engine ----------

    def _lazy_app(self):
        if self._app is not None:
            return self._app

        # Rate-limit the REBUILD, not just the download. self._app stays None after a failure, and
        # analyze_frame calls this once per captured frame, so before 7d-F every frame of every
        # verify burst re-ran the whole construction -- including insightface's "directory absent
        # => fetch ~290 MB over HTTP", with no timeout and no backoff. The cooldown replays the
        # same error instead, so the caller's behaviour is unchanged and the machine stops
        # hammering the network.
        if self._app_failed_at:
            waited = time.monotonic() - self._app_failed_at
            if waited < _MODEL_RETRY_COOLDOWN_S:
                # Stage 9 (D-81): replay the SAME exception type and message (an insightface
                # assert has an empty str -- repr keeps it readable), chained to the original.
                err = self._app_error
                cls = type(err) if isinstance(err, RuntimeError) else RuntimeError
                raise cls(str(err) or repr(err)) from err

        try:
            return self._build_app()
        except Exception as e:
            self._app_failed_at = time.monotonic()
            self._app_error = e
            raise

    def _build_app(self):
        # Pre-flight the model pack BEFORE constructing FaceAnalysis. Without this an incomplete
        # or absent pack becomes an HTTP download attempt deep inside the library, and the user
        # sees only "verify error" in a log file. face_service/detector.py has done exactly this
        # for YuNet since Stage 3; buffalo_l is the one engine input that never got the same
        # treatment, which is also why an offline first run has no honest failure message.
        # Stage 9 (R7, F-122): exactly the five pinned files with their pinned hashes, or a clear
        # refusal. Nothing is downloaded at run time: the old text sent the user to "run the
        # service once online", which could never work (this check ran before any download).
        problems = model_problems(hashes=True)
        if problems:
            raise ModelsUnavailable(
                f"InsightFace model pack '{MODEL_NAME}' is not usable ({'; '.join(problems[:3])}). "
                "Run the Face Unlock installer again: its model step downloads and checks the "
                "pack. PIN and password sign-in are unaffected."
            )

        _prep_cuda_dlls()  # must run before any CUDA session is created
        import onnxruntime as ort
        try:
            ort.preload_dlls()
        except Exception as e:  # pragma: no cover - defensive
            log.debug("ort.preload_dlls() skipped: %s", e)

        from insightface.app import FaceAnalysis

        providers, ctx_id = _select_providers(ort)
        # root= only when frozen: insightface has NO environment override (checked against the
        # pinned 1.0.1 -- root is a plain constructor default), so this kwarg is the only way to
        # point it at the bundled copy. Dev omits it and keeps ~/.insightface exactly as before.
        root = model_root()
        app = FaceAnalysis(
            name=MODEL_NAME,
            allowed_modules=ALLOWED_MODULES,
            providers=providers,
            **({"root": str(root)} if root is not None else {}),
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
        embeds, rep = self.build_gallery(directory)
        self.commit_gallery(embeds)
        if report:
            return rep
        return rep.count

    def build_gallery(self, directory: Path = ENROLL_DIR):
        """Compute a gallery from the images in ``directory`` WITHOUT touching the live one.

        Returns ``(embeddings, EnrollReport)``. Quality control as before (detector confidence,
        aligned-crop sharpness, exposure), and since Stage 9 (R6, F-120) one person only: every
        accepted vector must lie within ``cfg.threshold`` of the set's medoid; the others are
        dropped and reported ("other-person"). Fewer than ``cfg.enroll_min_frames`` left raises
        RuntimeError with the reasons. Telemetry for the build lands in ``last_enroll_info``:
        the baseline open-eye EAR (median over accepted frames) and the pose medians.
        """
        app = self._lazy_app()
        directory.mkdir(parents=True, exist_ok=True)
        images = [p for p in directory.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
        if not images:
            raise RuntimeError(f"No enroll images in {directory.name}")   # F-113: no profile path

        vecs: list[np.ndarray] = []
        accepted: list[tuple[str, FrameQuality]] = []
        rejected: list[tuple[str, str]] = []
        poses: list[tuple[float, float]] = []   # (pitch, yaw) of accepted frames (8b D-17)
        ears: list[float] = []                   # open-eye EAR of accepted frames (R6)
        names: list[str] = []
        for p in sorted(images):
            img = imio.imread(p)          # Stage 9 (R8, F-116): Unicode-safe
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
            names.append(p.name)
            try:
                lm = face.get("landmark_2d_106") if hasattr(face, "get") else None
                if lm is not None:
                    ears.append(float(compute_ear(lm)))
            except Exception:
                pass
            # Stage 8b (D-17): telemetry only -- the pose of each ACCEPTED frame, from the same
            # landmark_3d_68 pose the gesture round uses. Nothing here changes what is accepted.
            try:
                pose = face.get("pose") if hasattr(face, "get") else None
                if pose is not None:
                    from .liveness import POSE_PITCH, POSE_YAW
                    poses.append((float(pose[POSE_PITCH]), float(pose[POSE_YAW])))
            except Exception:
                pass
            log.info("enroll OK %s: det=%.3f sharp=%.1f luma=%.1f facepx=%d",
                     p.name, q.det, q.sharpness, q.luma, q.face_px)

        # Stage 9 (R6, F-120): one person. Each vector must be within the match threshold of the
        # medoid (the accepted vector with the smallest summed distance to all the others); the
        # rest are another face -- someone leaning in, a photo held up -- and are dropped.
        others: list[str] = []
        if len(vecs) >= 2:
            m = np.stack(vecs, axis=0)
            dist = 1.0 - np.clip(m @ m.T, -1.0, 1.0)
            medoid = int(np.argmin(dist.sum(axis=1)))
            keep = [i for i in range(len(vecs)) if dist[i, medoid] <= float(self.cfg.threshold)]
            if len(keep) < len(vecs):
                for i in range(len(vecs)):
                    if i not in keep:
                        others.append(names[i])
                        rejected.append((names[i], "other-person"))
                        log.warning("enroll DROP %s: other-person (d to the set's medoid %.3f > %.2f)",
                                    names[i], float(dist[i, medoid]), float(self.cfg.threshold))
                vecs = [vecs[i] for i in keep]
                accepted = [accepted[i] for i in keep]
                ears = [e for i, e in enumerate(ears) if i in keep] if len(ears) == len(names) else ears
                poses = [pz for i, pz in enumerate(poses) if i in keep] if len(poses) == len(names) else poses

        min_frames = int(self.cfg.enroll_min_frames)
        if len(vecs) < min_frames:
            extra = (f" {len(others)} image(s) showed a different person than the rest."
                     if others else "")
            raise RuntimeError(
                f"enrollment rejected: only {len(vecs)} of {len(images)} image(s) passed "
                f"quality control (need >= {min_frames}). Dropped: "
                f"{summarize_rejections(rejected)}.{extra} Re-capture alone in front of the "
                f"camera, with steady focus, the face filling the frame, and even lighting "
                f"(avoid strong backlight)."
            )

        embeds = np.stack(vecs, axis=0)
        # Stage 8b (D-17): the session's pose, for the wizard's warning (medians of the accepted
        # frames; None when the engine reported no pose).
        self.last_enroll_pose = ({"pitch": float(np.median([a for a, _b in poses])),
                                  "yaw": float(np.median([b for _a, b in poses])),
                                  "n": len(poses)} if poses else None)
        # Stage 9 (R6): build telemetry -- counts, and the user's open-eye EAR (the blink
        # threshold is absolute, B4-06: this is the evidence for revisiting it). No image data.
        self.last_enroll_info = {
            "accepted": len(vecs), "rejected": len(rejected), "images": len(images),
            "other_person": len(others),
            "ear_open_median": round(float(np.median(ears)), 4) if ears else None,
        }
        log.info("enrollment computed: %d accepted, %d rejected of %d image(s) "
                 "(other-person %d; open-eye EAR median %s)", len(vecs), len(rejected),
                 len(images), len(others), self.last_enroll_info["ear_open_median"])
        return embeds, EnrollReport(count=len(vecs), accepted=accepted, rejected=rejected)

    def commit_gallery(self, embeds: np.ndarray) -> None:
        """Make ``embeds`` the live gallery: embeddings.npz atomically (fsync'ed, F-128), then the
        in-memory refs, then a reset of the adaptive ring (a fresh baseline invalidates prior
        drift adaptation)."""
        EMBED_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Stage 8b (F-45) / Stage 9 (F-128): write-then-rename, data on disk before the rename.
        write_npz_atomic(EMBED_PATH, embeddings=embeds, engine=np.array(ENGINE_TAG),
                         dim=np.array(EMBED_DIM, dtype=np.int64))
        self._enroll_refs = embeds
        self._incompatible = None
        self._adaptive.clear()
        self._refresh_refs()
        log.info("enrollment committed: %d vector(s); adaptive gallery reset", int(embeds.shape[0]))

    def _mark_incompatible(self, why: str) -> bool:
        """Stage 9 (F-125): an unusable embeddings.npz is logged ONCE and remembered, instead of
        being re-read and re-logged for every analysed frame."""
        if self._incompatible != why:
            log.warning("%s: %s -- re-enroll required.", EMBED_PATH.name, why)
        self._incompatible = why
        return False

    def load(self) -> bool:
        if not EMBED_PATH.exists():
            # No enrollment baseline -> don't leave an orphan adaptive ring on disk (it is
            # meaningless without an anchor and would confuse a later inspection). F3.
            self._adaptive.clear()
            self._incompatible = None
            return False
        try:
            data = np.load(EMBED_PATH, allow_pickle=False)
            files = set(data.files)
        except Exception as e:
            return self._mark_incompatible(f"unreadable ({e!r})")
        if "engine" not in files or "dim" not in files or "embeddings" not in files:
            return self._mark_incompatible("no engine tag (an old DeepFace enrollment)")
        engine = str(data["engine"])
        dim = int(data["dim"])
        if engine != ENGINE_TAG or dim != EMBED_DIM:
            return self._mark_incompatible(
                f"built by engine={engine!r} dim={dim}; this engine is {ENGINE_TAG!r} dim={EMBED_DIM}")
        refs = data["embeddings"]
        if refs.ndim != 2 or refs.shape[1] != EMBED_DIM or refs.shape[0] == 0:
            return self._mark_incompatible(f"unexpected shape {tuple(refs.shape)}")
        if not finite_rows(refs):
            # Stage 9 (F-126): one NaN row made every min() NaN -- a permanent no-match.
            return self._mark_incompatible("non-finite values")
        self._enroll_refs = refs.astype(np.float32)
        self._incompatible = None
        # Stage 9 (D-40): the ring is ALWAYS loaded into memory, whatever the toggle says;
        # _refresh_refs decides whether its rows join the matching set. The file stays the only
        # store, so a later re-enable finds every stored sample. clear_adaptive() (or a
        # re-enroll) is the hard rollback.
        try:
            if self._adaptive.load():
                log.info("adaptive gallery loaded: %d embedding(s) (%s)", self._adaptive.count,
                         "in use" if self.cfg.adaptive_gallery else "kept, toggle off")
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
            if self._incompatible:
                raise RuntimeError(f"Enrollment needs to be redone ({self._incompatible}).")
            raise RuntimeError("No enrollment found. Run enroll first.")

        app = self._lazy_app()
        # Stage 8b (F-22 / D-07). Defect: an engine failure here was logged at DEBUG and returned
        # as "no face". Consequence: a broken model / CUDA / input looked exactly like an empty
        # room -- the presence probe spent absence strikes on it, and the log never said why.
        # Fix: an empty frame and an engine exception both raise EngineError (chained to the
        # cause). Every caller already catches per frame: the unlock burst and the gesture round
        # skip the frame exactly as they skipped a "no face" before (EngineError is deliberately
        # NOT a RuntimeError, so the gesture round's engine-refusal branch is not reached), and the
        # presence probe now classifies it as an error rather than an absence.
        if bgr is None or getattr(bgr, "size", 0) == 0:
            raise EngineError("empty frame")
        try:
            faces = app.get(bgr)
        except Exception as e:
            raise EngineError(f"insightface get failed: {e!r}") from e
        if not faces:
            return FrameAnalysis(False, False, 1.0, None, None, None)

        face = self._largest_face(faces)
        emb = np.asarray(face.normed_embedding, dtype=np.float32)
        best = min(self._cosine(emb, r) for r in self._refs)  # type: ignore[union-attr]
        is_match = best <= self.cfg.threshold

        # Passive anti-screen on the face crop (conservative hf gate; None if crop unusable).
        # Stage 9 (F-124): a check that RAISES now counts as screen-like (fail closed) and is
        # logged at WARNING once per episode -- it used to be DEBUG and "unchecked", so a broken
        # check silently meant "clean".
        screen = None
        feats = None
        if getattr(self.cfg, "anti_screen", True):
            try:
                screen = self._screen.check(bgr, face.bbox)
                feats = self._screen.last if screen is not None else None
                self._screen_error_logged = False
            except Exception as e:
                screen = True
                if not self._screen_error_logged:
                    log.warning("anti-screen check failed (%r); the frame counts as screen-like "
                                "(logged once per episode)", e)
                    self._screen_error_logged = True

        landmark = face.get("landmark_2d_106") if hasattr(face, "get") else None
        pose = face.get("pose") if hasattr(face, "get") else None
        return FrameAnalysis(True, is_match, best, screen, landmark, pose, emb, feats)

    def pose_of(self, bgr: np.ndarray) -> "tuple[float, float] | None":
        """(pitch, yaw) of the largest face -- the same 1k3d68 estimator the gesture round uses,
        for the turn-sign calibration (Stage 9, R6). No identity involved; None without a face."""
        from .liveness import POSE_PITCH, POSE_YAW
        if bgr is None or getattr(bgr, "size", 0) == 0:
            return None
        app = self._lazy_app()
        faces = app.get(bgr)
        if not faces:
            return None
        pose = self._largest_face(faces).get("pose")
        if pose is None:
            return None
        p = np.asarray(pose, dtype=np.float32).ravel()
        return float(p[POSE_PITCH]), float(p[POSE_YAW])

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

    def clear_enrollment(self) -> "str | None":
        """Stage 8b (F-06): forget the gallery IN MEMORY -- enrollment refs and the adaptive ring
        (whose file is deleted too). The caller deletes embeddings.npz and the images; this makes
        the running service stop matching immediately instead of at its next restart.
        Stage 9 (F-121): memory first, whatever happens to the ring file; returns its problem."""
        self._enroll_refs = None
        self._refs = None
        self._incompatible = None
        problem = self._adaptive.clear()
        log.info("enrollment cleared in memory (refs + adaptive ring)")
        return problem

    def clear_adaptive(self) -> "str | None":
        """Roll back all drift adaptation (delete the adaptive file); enrollment stays."""
        problem = self._adaptive.clear()
        self._refresh_refs()
        log.info("adaptive gallery cleared")
        return problem
