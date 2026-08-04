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
WATCHDOG_PAUSE_PATH = APP_DIR / "watchdog.pause"   # Stage 3 Step 5: deliberate-stop pause (self-expiring)

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
    distance_metric: str = "cosine"
    threshold: float = 0.32          # ArcFace cosine. Set from Stage-9 measurements on this
                                     # webcam: genuine (self) max ~0.12 over 40 varied frames,
                                     # impostor min ~0.97 -> huge gap; 0.32 keeps self headroom
                                     # (~2.6x) while staying far below any impostor.
    camera_index: int = 0
    camera_warmup_frames: int = 10   # discard N frames after opening for auto-exposure
    persistent_camera: bool = True   # keep VideoCapture open between requests
    verify_frames: int = 5            # số frame cần đạt ngưỡng
    verify_required: int = 2          # trong đó cần ≥ N khớp (giảm từ 3 để nhanh hơn)
    presence_interval_s: int = 60
    presence_absent_strikes: int = 2  # vắng mặt liên tiếp trước khi lock
    # Absent strikes required while a FULLSCREEN app or presentation mode owns the screen (a game,
    # a film, slides). The ordinary threshold is tuned for "walked away from the desk"; the same two
    # ticks during a match or a movie is a FALSE lock, and the user is demonstrably at the machine.
    # 10 at the default 60s interval is ~10 minutes. 0 = never lock while fullscreen is active
    # (strikes still accrue and show in Status; only LockWorkStation is withheld). The signal is
    # SHQueryUserNotificationState -- see presence_monitor/monitor.py::_fullscreen_active.
    presence_fullscreen_strikes: int = 10
    # --- Stage 7c-6: three-level presence verdict (probe-only; unlock is NOT affected) ---
    # The probe used to answer a bare yes/no on a single frame passing the SAME bar as unlock, and
    # two live incidents showed both ways that fails a user sitting at the desk: a working pose puts
    # the distance just past the threshold, and anti-screen suppresses a match outright at close
    # range. These three knobs add a middle state instead of loosening any recognition number.
    #
    # Soft band ABOVE threshold that still counts as "seen", for the probe ONLY. A frame in
    # (threshold, threshold + this] with anti-screen happy is a WEAK sighting and keeps the session
    # alive; the same frame is still not good enough to unlock anything. Deliberately small: at the
    # default 0.32 this reaches 0.37, well below the ~0.97 impostor floor measured in Stage 1.
    presence_soft_margin: float = 0.05
    # A frame inside the soft band that anti-screen flagged is SUSPECT, and a probe made only of
    # suspect frames is "uncertain" rather than absent -- it never locks on its own. This many
    # consecutive uncertain probes turn into one absence strike, so a genuinely hostile signal still
    # converges; 3 at the default 60s interval is ~3 minutes of tolerance.
    presence_uncertain_streak: int = 3
    # Before spending an absence strike the monitor waits this long and probes ONCE more: a lock is
    # expensive and a single bad burst is cheap to double-check. 0 disables the confirmation and
    # makes an absent probe count immediately (the pre-7c-6 behaviour).
    presence_confirm_delay_s: float = 2.0
    # --- Stage 7c-8: live input counts as presence (monitor-side; the service never sees this) ---
    # The camera stopped being the only witness. Looking at a phone, reading something on the desk
    # or just tilting away takes the face out of frame while the user is plainly still there, and
    # the probe called that absence. Keyboard or mouse activity newer than this many seconds ends
    # the tick as present WITHOUT opening the camera at all -- input is a stronger presence signal
    # than a frame, and it costs nothing. Beyond it the camera votes exactly as before. 0 disables
    # the fusion and restores the camera-only behaviour.
    presence_input_idle_s: float = 45.0
    # "recognition" = InsightFace/ONNX ArcFace embedding must match enrolled face (stronger; walk-away + strangers)
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
    # --- Stage 3: low-light gate (Step 3.2; honest "too-dark" refusal, no camera control here) ---
    # Below this SCENE luma (mean gray of the whole frame, not the crop) an unlock is refused with
    # reason "too-dark" even if recognition would match: in the dark the passive anti-spoof and the
    # match margin are not trustworthy (Step 3.1: dist climbs past threshold ~scene 10, anti-screen
    # hf false-flags ~88%; clean pass only from ~scene 48+). 45.0 sits conservatively between those.
    # It is an ENVIRONMENT refusal, so the service keeps it lockout-neutral. 0 disables the gate.
    low_light_luma_min: float = 45.0
    # --- Stage 3: gated exposure boost (Step 3.3) ---
    # When an unlock burst is below the floor above, try to raise webcam EXPOSURE and re-capture
    # BEFORE the too-dark refusal, to pull a genuine user out of the dark. STRICTLY gated (only
    # below the floor): an unconditional boost blows out a normally-lit face (Step 3.1: the same
    # boost drove scene 98->230 and lost the face 100%->0%). Only EXPOSURE is touched -- the 3.1
    # roundtrip showed this webcam's driver ignores GAIN and AUTO_EXPOSURE sets but honors EXPOSURE.
    # The boost is transient: exposure is always restored after the attempt. Default True:
    # confirmed on the live smoke -- scene 44 -> 115 (exposure -6 -> -4), restore verified, and the
    # extra burst adds only ~200-400ms. Set False to disable (then the path is identical to 3.2).
    low_light_boost: bool = True
    # Exposure step (EV) added to the current CAP_PROP_EXPOSURE when boosting. Units are
    # driver-defined; on this webcam less-negative == brighter, so a POSITIVE step brightens
    # (Step 3.1: set -6 -> -4 honored, scene 10 -> 50, distance 0.363 -> 0.249). One +2 step is
    # enough on this cam; widen only on evidence.
    low_light_exposure_step: float = 2.0
    # --- Stage 3: busy-camera handling (Step 4) ---
    # When the webcam is held by ANOTHER process (not our own enrollment lease), opening it fails.
    # Instead of a multi-second RuntimeError bubbling up as reason "exception: ...", the service
    # bounds the open with a short retry loop (Camera.open_fast) and reports reason "camera-busy"
    # -- lockout-NEUTRAL, since a busy device is environment, not a failed match. These bound that
    # loop; open() itself keeps its robust 3x3 zombie recovery for enrollment / warmup.
    camera_open_retries: int = 2         # extra open attempts after the first before declaring busy
    camera_open_timeout_s: float = 3.0   # wall-clock budget for the whole open-retry loop (seconds)
    # Hard ceiling on how long we WAIT for ONE open attempt (7b-2). camera_open_timeout_s above
    # only decides whether another attempt may START; it cannot bound an attempt already running,
    # and a wedged webcam driver can leave VideoCapture()/read() inside a native call that no
    # timeout property on this hardware interrupts. Each attempt therefore runs on its own thread
    # and is waited on for at most this long. Blowing the ceiling is treated as "the device is
    # stuck": we stop waiting AND stop retrying, because another attempt is just more waiting on
    # the same stuck device. The native call cannot be cancelled -- it may still be running, and
    # whatever capture it eventually produces is closed by that thread (see camera_open.py).
    camera_open_attempt_cap_s: float = 5.0
    # --- Stage 7b: persistent-camera self-heal (post-hoc; the KNOWN_ISSUES #1 signature) ---
    # A wedged owner elsewhere on the machine can hand the device back in a state where our
    # capture opens "successfully" and then reads nothing, or reads all-black frames. With
    # persistent_camera=True that capture is CACHED and reused for the process lifetime, because
    # the reuse gate only checks that a handle exists. The service deliberately does NOT probe-read
    # to detect this up front: a probe read can itself wedge on this hardware, and the pipe server
    # is sequential, so a hanging probe would take the whole service down. Instead it judges the
    # reads that ALREADY happened and drops the cache afterwards, so the next request opens fresh.
    # camera_black_luma is a BLACK-FRAME floor, NOT a darkness gate: a genuinely dark room measures
    # ~10 scene luma (Step 3.1) and must not look broken, so this sits far below
    # low_light_luma_min and only catches the ~0 of a capture that is no longer seeing anything.
    camera_black_luma: float = 2.0          # burst scene luma <= this counts as a black capture
    camera_reopen_cooldown_s: float = 30.0  # min seconds between two self-heals (anti-thrash)
    # --- Stage 3: watchdog (Step 5; external Scheduled-Task supervisor pings the pipe) ---
    # tools.watchdog pings the existing `ping` command every watchdog_interval_s; after
    # watchdog_fail_threshold consecutive failures (each bounded by watchdog_ping_timeout_s -- a
    # hung server that never answers counts as a fail) it restarts the service KILL-THEN-START
    # (a hung-but-alive process still holds the single-instance mutex). A deliberate `shutdown`
    # drops a self-expiring pause (watchdog_pause_ttl_s) so the watchdog does not resurrect an
    # intentional stop; an EXPIRED pause is ignored + deleted, so a stale pause can never silence
    # the watchdog forever. A permanent disable = stop the FaceUnlock-Watchdog task itself.
    watchdog_ping_timeout_s: float = 2.0   # per-ping wall-clock budget (a hung server -> a fail)
    watchdog_fail_threshold: int = 3        # consecutive ping failures before a restart
    watchdog_interval_s: float = 30.0       # seconds between pings in the watchdog self-loop
    watchdog_pause_ttl_s: float = 300.0     # deliberate-stop pause lifetime; self-heals after this
    # --- Stage 4: named-pipe perimeter hardening (Batch 1; channel boundary = DACL + SID-gate) ---
    # Explicit pipe security descriptor (SELF=GA, SYSTEM=GRGW, no Everyone ACE, + a Medium mandatory
    # label NoReadUp/NoWriteUp) instead of the legacy NULL DACL that granted Everyone. Default on;
    # set False only to roll back to the legacy NULL-DACL pipe (_build_sa_everyone_legacy).
    pipe_hardened_sd: bool = True
    # FILE_FLAG_FIRST_PIPE_INSTANCE on the server (refuse to start if the pipe name is already taken
    # -- a squatter) PLUS the client-side server-SID check in tools/pipe_client.py (verify the server
    # runs as SELF or SYSTEM before sending). Both live under this one toggle. Default on.
    pipe_first_instance: bool = True
    # SID-gate on the unlock command (Stage 4 Step 5): when True, only a caller whose token SID is
    # SYSTEM (S-1-5-18) -- the lockscreen Credential Provider -- may invoke unlock; any other caller
    # gets {"ok":false,"reason":"not-authorized"} before load_password. Default TRUE since the Stage 5
    # closeout: the lockscreen CP (SYSTEM) is the only legitimate unlock caller, so the gate ships on
    # -- secure-by-default; dev harnesses connecting as SELF opt out via config. Only unlock is gated
    # (other commands are scoped by the Batch-1 pipe DACL).
    pipe_unlock_require_system: bool = True
    # UI language code (see face_service.i18n.LANGUAGES). Auto-detected
    # from the system locale on first run if the config file is missing.
    language: str = field(default_factory=_default_language)
    # --- Stage 6: event-notification gates (tray toasts, presence_monitor UI) ---
    notify_enroll: bool = True          # toast on enrollment build success/failure
    notify_lockout: bool = True         # toast when a face-lockout episode starts
    notify_service_state: bool = False  # toast on service reachable<->unreachable transitions
    # False = observe-only presence: strikes are counted and visible in Status,
    # but LockWorkStation is never called. Face sign-in works either way.
    auto_lock: bool = True

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
        # Fullscreen threshold: an integer (bool rejected, like camera_open_retries) and >= 0,
        # where 0 carries the meaning "never lock while fullscreen" rather than "lock immediately".
        if isinstance(self.presence_fullscreen_strikes, bool) or \
                not isinstance(self.presence_fullscreen_strikes, int):
            raise ValueError("presence_fullscreen_strikes must be an integer")
        if self.presence_fullscreen_strikes < 0:
            raise ValueError(
                "presence_fullscreen_strikes must be >= 0 (0 = never lock while fullscreen)")
        # 7c-6 probe verdict knobs. The margin is bounded well below any impostor distance -- it
        # widens what counts as "still here", never what counts as a match.
        if not (0.0 <= self.presence_soft_margin <= 0.2):
            raise ValueError("presence_soft_margin must be in [0, 0.2]")
        if isinstance(self.presence_uncertain_streak, bool) or \
                not isinstance(self.presence_uncertain_streak, int):
            raise ValueError("presence_uncertain_streak must be an integer")
        if self.presence_uncertain_streak < 1:
            raise ValueError("presence_uncertain_streak must be >= 1")
        if self.presence_confirm_delay_s < 0.0:
            raise ValueError(
                "presence_confirm_delay_s must be >= 0 (0 = no confirmation re-probe)")
        if self.presence_input_idle_s < 0.0:
            raise ValueError(
                "presence_input_idle_s must be >= 0 (0 = ignore input, camera only)")
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
        # Low-light gate floor: a scene-luma value in [0, 255]. 0 disables the gate. Fail loud
        # rather than silently clamp an out-of-range floor (same spirit as the adaptive checks).
        if not (0.0 <= self.low_light_luma_min <= 255.0):
            raise ValueError("low_light_luma_min must be in [0, 255] (0 disables the low-light gate)")
        if not isinstance(self.low_light_boost, bool):
            raise ValueError("low_light_boost must be a boolean")
        # Exposure step in driver-defined EV units; must brighten (>0) and stay sane (<= 16 stops).
        if not (0.0 < self.low_light_exposure_step <= 16.0):
            raise ValueError("low_light_exposure_step must be in (0, 16]")
        # Busy-camera open loop: a non-negative integer retry count (bool rejected) and a positive,
        # bounded wall-clock budget. Fail loud rather than silently clamp.
        if isinstance(self.camera_open_retries, bool) or not isinstance(self.camera_open_retries, int):
            raise ValueError("camera_open_retries must be an integer")
        if not (0 <= self.camera_open_retries <= 10):
            raise ValueError("camera_open_retries must be in [0, 10]")
        if not (0.0 < self.camera_open_timeout_s <= 30.0):
            raise ValueError("camera_open_timeout_s must be in (0, 30]")
        # Per-attempt open ceiling (7b-2): positive and bounded. Deliberately NOT constrained
        # against camera_open_timeout_s -- the two measure different things (one attempt vs the
        # whole retry loop), and a cap above the loop budget just means the loop stops first.
        if not (0.0 < self.camera_open_attempt_cap_s <= 60.0):
            raise ValueError("camera_open_attempt_cap_s must be in (0, 60]")
        # Persistent-camera self-heal (7b): a black-frame floor that must stay a floor (not a
        # darkness gate -- the upper bound keeps it well under low_light_luma_min), and a positive,
        # bounded anti-thrash cooldown. Fail loud rather than silently clamp, like the checks above.
        if not (0.0 < self.camera_black_luma <= 50.0):
            raise ValueError("camera_black_luma must be in (0, 50]")
        if not (0.0 < self.camera_reopen_cooldown_s <= 600.0):
            raise ValueError("camera_reopen_cooldown_s must be in (0, 600]")
        # Watchdog bounds (Step 5): positive/bounded timeouts, an integer failure threshold >= 1.
        if not (0.0 < self.watchdog_ping_timeout_s <= 30.0):
            raise ValueError("watchdog_ping_timeout_s must be in (0, 30]")
        if isinstance(self.watchdog_fail_threshold, bool) or not isinstance(self.watchdog_fail_threshold, int):
            raise ValueError("watchdog_fail_threshold must be an integer")
        if not (1 <= self.watchdog_fail_threshold <= 100):
            raise ValueError("watchdog_fail_threshold must be in [1, 100]")
        if not (0.0 < self.watchdog_interval_s <= 3600.0):
            raise ValueError("watchdog_interval_s must be in (0, 3600]")
        if not (0.0 < self.watchdog_pause_ttl_s <= 3600.0):
            raise ValueError("watchdog_pause_ttl_s must be in (0, 3600]")
        # Stage 4: the pipe perimeter toggles must be real booleans (a stray int/str would silently
        # take a truthy branch and pick the wrong descriptor / flag). Fail loud, like the checks above.
        if not isinstance(self.pipe_hardened_sd, bool):
            raise ValueError("pipe_hardened_sd must be a boolean")
        if not isinstance(self.pipe_first_instance, bool):
            raise ValueError("pipe_first_instance must be a boolean")
        if not isinstance(self.pipe_unlock_require_system, bool):
            raise ValueError("pipe_unlock_require_system must be a boolean")
        # Stage 6: notification gates must be real booleans (same rationale).
        if not isinstance(self.notify_enroll, bool):
            raise ValueError("notify_enroll must be a boolean")
        if not isinstance(self.notify_lockout, bool):
            raise ValueError("notify_lockout must be a boolean")
        if not isinstance(self.notify_service_state, bool):
            raise ValueError("notify_service_state must be a boolean")
        if not isinstance(self.auto_lock, bool):
            raise ValueError("auto_lock must be a boolean")
        # If the hardened descriptor is requested, the current user's SID MUST resolve -- the DACL is
        # built from it (SELF=GA). Fail loud here rather than fall through to a pipe nobody can use.
        # Lazy pywin32 import so importing config on a stripped interpreter stays cheap when off.
        if self.pipe_hardened_sd:
            try:
                import win32api, win32con, win32security  # noqa: PLC0415
                _th = win32security.OpenProcessToken(
                    win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
                _sid = win32security.GetTokenInformation(_th, win32security.TokenUser)[0]
                if not win32security.ConvertSidToStringSid(_sid):
                    raise ValueError("current-user SID resolved empty")
            except Exception as e:
                raise ValueError(
                    f"pipe_hardened_sd=True but the current-user SID did not resolve: {e}")
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
