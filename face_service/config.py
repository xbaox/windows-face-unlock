from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w  # type: ignore
except ImportError:  # pragma: no cover - optional for read-only usage
    tomli_w = None  # type: ignore

APP_DIR = Path(os.environ.get("FACE_UNLOCK_HOME", Path.home() / ".face-unlock"))
CONFIG_PATH = APP_DIR / "config.toml"
ENROLL_DIR = APP_DIR / "enroll"
EMBED_PATH = APP_DIR / "embeddings.npz"
CREDS_PATH = APP_DIR / "credentials.bin"
LOG_PATH = APP_DIR / "service.log"
LOCKOUT_PATH = APP_DIR / "lockout.json"
AUDIT_PATH = APP_DIR / "audit.jsonl"
ADAPTIVE_PATH = APP_DIR / "adaptive.npz"   # Stage 3: adaptive gallery (separate from embeddings.npz)

PIPE_NAME = r"\\.\pipe\FaceUnlock"

PRESENCE_MODES = ("recognition", "detection")
LIVENESS_MODES = ("fast", "paranoid")


def _default_language() -> str:
    # Imported here to avoid a circular import (i18n → config is fine,
    # but we only need the detection helper at runtime).
    from .i18n import detect_system_language
    return detect_system_language()


@dataclass
class Config:
    model_name: str = "ArcFace"
    detector_backend: str = "yunet"  # yunet is fast + robust. Alternatives: opencv, retinaface
    distance_metric: str = "cosine"
    threshold: float = 0.32          # ArcFace cosine. Set from Stage-9 measurements on this
                                     # webcam: genuine (self) max ~0.12 over 40 varied frames,
                                     # impostor min ~0.97 -> huge gap; 0.32 keeps self headroom
                                     # (~2.6x) while staying far below any impostor.
    anti_spoofing: bool = True
    camera_index: int = 0
    camera_warmup_frames: int = 10   # discard N frames after opening for auto-exposure
    persistent_camera: bool = True   # keep VideoCapture open between requests
    verify_frames: int = 5            # số frame cần đạt ngưỡng
    verify_required: int = 2          # trong đó cần ≥ N khớp (giảm từ 3 để nhanh hơn)
    presence_interval_s: int = 60
    presence_absent_strikes: int = 2  # vắng mặt liên tiếp trước khi lock
    # "recognition" = DeepFace ArcFace must match enrolled face (stronger; walk-away + strangers)
    # "detection"   = YuNet any-face-in-frame is enough (weaker; mimics old AutoFaceLock)
    presence_mode: str = "recognition"
    warmup_on_start: bool = True
    # --- Stage 2: liveness (active challenge / anti-screen / rate-limit) ---
    # fast    = challenge only on doubt (subsecond when confident + clean)
    # paranoid = require an active gesture on every valid match
    liveness_mode: str = "fast"
    blink_timeout_s: float = 4.0       # window to observe a spontaneous blink
    challenge_on_doubt: bool = True    # fast mode: on doubt escalate to a gesture (else deny)
    anti_screen: bool = True           # passive anti-screen (texture/moire) doubt trigger
    max_face_attempts: int = 5         # consecutive face failures before a temporary face lockout
    lockout_seconds: int = 300         # face-lockout cooldown; PIN/password stays available
    audit_log: bool = True             # write a JSONL audit record per verify/unlock/challenge
    audit_max_mb: float = 5.0          # rotate the audit file past this size (keeps 2 backups)
    # --- Stage 3: enrollment quality control (locked to this webcam's real enroll set) ---
    # Good enroll frames measured on this cam: det_score 0.83-0.89, sharpness (variance of
    # Laplacian on the aligned 112 crop) 151-321, luma 82-131, clipping 0%. Gates sit with
    # headroom below/above those so a good frame never drops, while blurry / dark / weak-
    # detection frames do. face_px and clip fractions are logged as telemetry, not hard gates.
    enroll_min_det_score: float = 0.65  # drop faces the detector is unsure about (< this)
    enroll_min_sharpness: float = 80.0  # min variance-of-Laplacian on the aligned crop (blur floor)
    enroll_luma_min: float = 55.0       # drop under-exposed crops (mean luma below this)
    enroll_luma_max: float = 210.0      # drop blown-out crops (mean luma above this)
    enroll_min_frames: int = 3          # need >= this many QC-passing frames or enroll fails clearly
    # --- Stage 3: adaptive gallery (opt-in; adapts to gradual drift, guarded vs poisoning) ---
    # Off by default: this mutates the gallery. When on, a just-verified frame is added ONLY if it
    # is within (threshold - adaptive_margin) COSINE OF THE ENROLLMENT BASELINE (not the adaptive-
    # augmented set -> no drift-hopping), liveness passed, no screen flag, and (in paranoid) a
    # gesture passed. Measured refs: self <=0.124, replay-of-self ~0.155, impostor ~0.97. Default
    # margin 0.17 -> ceiling 0.15 at threshold 0.32: above self-max, BELOW replay -> spoof/other
    # cannot inject. Widening the margin lowers the ceiling toward replay distance -- keep it tight.
    adaptive_gallery: bool = False      # master toggle (mutates the gallery; opt in explicitly)
    adaptive_margin: float = 0.17       # add only if enroll-distance <= threshold - this
    adaptive_max_size: int = 10         # cap on stored adaptive embeddings (FIFO ring; excludes enroll)
    adaptive_cooldown_s: float = 1800.0 # min seconds between two adaptive additions (rate-limit)
    # UI language code (see face_service.i18n.LANGUAGES). Auto-detected
    # from the system locale on first run if the config file is missing.
    language: str = field(default_factory=_default_language)

    @classmethod
    def load(cls) -> "Config":
        if not CONFIG_PATH.exists():
            APP_DIR.mkdir(parents=True, exist_ok=True)
            return cls()
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        if tomli_w is None:
            raise RuntimeError("tomli-w is required to save config (pip install tomli-w)")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            tomli_w.dumps(asdict(self)),
            encoding="utf-8",
        )

    def validate(self) -> None:
        from .i18n import LANG_CODES
        if self.presence_mode not in PRESENCE_MODES:
            raise ValueError(
                f"presence_mode must be one of {PRESENCE_MODES}, got {self.presence_mode!r}"
            )
        if self.presence_interval_s < 5:
            raise ValueError("presence_interval_s must be >= 5")
        if self.presence_absent_strikes < 1:
            raise ValueError("presence_absent_strikes must be >= 1")
        if not (0.0 < self.threshold < 2.0):
            raise ValueError("threshold must be in (0, 2)")
        if self.language not in LANG_CODES:
            raise ValueError(
                f"language must be one of {LANG_CODES}, got {self.language!r}"
            )
        if self.liveness_mode not in LIVENESS_MODES:
            raise ValueError(
                f"liveness_mode must be one of {LIVENESS_MODES}, got {self.liveness_mode!r}"
            )
        if self.blink_timeout_s <= 0:
            raise ValueError("blink_timeout_s must be > 0")
        if self.max_face_attempts < 1:
            raise ValueError("max_face_attempts must be >= 1")
        if self.lockout_seconds < 0:
            raise ValueError("lockout_seconds must be >= 0")
        if self.audit_max_mb <= 0:
            raise ValueError("audit_max_mb must be > 0")
        if not (0.0 <= self.enroll_min_det_score <= 1.0):
            raise ValueError("enroll_min_det_score must be in [0, 1]")
        if self.enroll_min_sharpness < 0:
            raise ValueError("enroll_min_sharpness must be >= 0")
        if not (0.0 <= self.enroll_luma_min < self.enroll_luma_max <= 255.0):
            raise ValueError("require 0 <= enroll_luma_min < enroll_luma_max <= 255")
        if self.enroll_min_frames < 1:
            raise ValueError("enroll_min_frames must be >= 1")
        if not (0.0 < self.adaptive_margin <= 1.0):
            raise ValueError("adaptive_margin must be in (0, 1]")
        if self.adaptive_max_size < 1:
            raise ValueError("adaptive_max_size must be >= 1")
        if self.adaptive_cooldown_s < 0:
            raise ValueError("adaptive_cooldown_s must be >= 0")
        if self.adaptive_gallery:
            # Anti-screen is the PRIMARY replay defense for adaptation; the distance ceiling
            # alone leaves only ~0.005 cosine below replay-of-self (~0.155 vs ceiling ~0.15),
            # so refuse to run adaptation with it off. F4.
            if not self.anti_screen:
                raise ValueError(
                    "adaptive_gallery requires anti_screen=True (anti-screen is the primary "
                    "replay defense for adaptation; the distance ceiling alone is too thin)"
                )
            # margin >= threshold => ceiling (threshold - margin) <= 0, which silently disables
            # every adaptation. Fail loudly rather than pretend the feature is on. N2.
            if self.adaptive_margin >= self.threshold:
                raise ValueError(
                    "adaptive_margin must be < threshold when adaptive_gallery is on "
                    f"(got margin={self.adaptive_margin} >= threshold={self.threshold} "
                    "=> ceiling <= 0, no frame could ever adapt)"
                )
