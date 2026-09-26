from __future__ import annotations
import logging
import math
import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

try:
    import tomli_w  # type: ignore
except ImportError:  # pragma: no cover - optional for read-only usage
    tomli_w = None  # type: ignore

def _app_dir() -> Path:
    """The data directory. Stage 9 (F-106): an INSTALLED (frozen) copy always uses
    %USERPROFILE%\\.face-unlock -- FACE_UNLOCK_HOME is honoured only in a source checkout (the
    selftests' isolated homes), so no environment variable can point the product's secrets and
    face data at an arbitrary, possibly shared, folder."""
    import sys
    env = os.environ.get("FACE_UNLOCK_HOME")
    if env and not getattr(sys, "frozen", False):
        return Path(env)
    return Path.home() / ".face-unlock"


APP_DIR = _app_dir()
CONFIG_PATH = APP_DIR / "config.toml"
ENROLL_DIR = APP_DIR / "enroll"
# Stage 8b (F-12): a "Replace" enrollment session is captured here and only promoted into
# ENROLL_DIR after it built successfully, so an interrupted re-enroll never leaves the user without
# a gallery. A module path, not a Config field.
ENROLL_PENDING_DIR = ENROLL_DIR / ".pending"
EMBED_PATH = APP_DIR / "embeddings.npz"
CREDS_PATH = APP_DIR / "credentials.bin"
# Stage 9 (act 9b R12, F-156): the logs live in their own folder, so "Open log folder" never opens
# the data directory with the face images, templates and the sealed password beside them. Files
# from before are moved there by logging_setup.migrate_logs.
LOG_DIR = APP_DIR / "logs"
LOG_PATH = LOG_DIR / "service.log"
LOCKOUT_PATH = APP_DIR / "lockout.json"
AUDIT_PATH = APP_DIR / "audit.jsonl"
ADAPTIVE_PATH = APP_DIR / "adaptive.npz"   # Stage 3: adaptive gallery (separate from embeddings.npz)
WATCHDOG_PAUSE_PATH = APP_DIR / "watchdog.pause"   # Stage 3 Step 5: deliberate-stop pause (self-expiring)
# Stage 9 (act 9b R6): the per-camera turn-sign calibration, and the folder the wizard hands its
# calibration frames over in (deleted by the service right after measuring them).
CALIBRATION_PATH = APP_DIR / "calibration.json"
CALIBRATION_DIR = APP_DIR / "calibration"

PIPE_NAME = r"\\.\pipe\FaceUnlock"

PRESENCE_MODES = ("recognition", "detection")
LIVENESS_MODES = ("fast", "paranoid")
# (Stage 9, D-70 / F-69: distance_metric -- validated, read by nothing -- and blink_timeout_s --
# only ever a term of the gesture round's wall cap, which is a constant since R4 -- are gone. An
# old config.toml with them loads with an "unknown key" warning.)

log = logging.getLogger(__name__)


# Stage 8b (F-32, act A-5): upper bound of the match threshold, in validate() and in the Settings
# window. Defect: validate() allowed (0, 2) and Settings offered up to 1.5 -- cosine distances at
# which a stranger (~0.97 measured) matches. Fix: a ceiling well above the measured genuine band
# (self <= ~0.12) and far below any impostor. The 0.32 default is untouched.
THRESHOLD_MAX = 0.5


def _default_language() -> str:
    # Imported here to avoid a circular import (i18n → config is fine,
    # but we only need the detection helper at runtime).
    from .i18n import detect_system_language
    return detect_system_language()


@dataclass
class Config:
    threshold: float = 0.32          # ArcFace cosine. Set from the Stage-1/3 measurements on the
                                     # reference webcam (D-71): genuine (self) max ~0.12 over 40
                                     # varied frames, impostor min ~0.97 -> huge gap; 0.32 keeps
                                     # self headroom (~2.6x) while staying far below any impostor.
    camera_index: int = 0            # used ONLY when camera_name is empty (old configs)
    # Stage 9 (act 9b R10, F-141): the camera's DirectShow friendly name, resolved to its index at
    # every open. A name that is not present is a clear refusal -- never a fallback to index 0.
    camera_name: str = ""
    camera_warmup_frames: int = 10   # discard N frames after opening for auto-exposure
    # Stage 9 (R10, F-139): on demand by default. The service opens the camera per request, and
    # keeps it warm only from a session lock until the unlock (60 s at most); a persistent capture
    # would hold the device away from Teams, Zoom and the Camera app for the service's lifetime.
    persistent_camera: bool = False
    verify_frames: int = 5            # frames in one unlock burst (D-72)
    verify_required: int = 2          # of those, this many must match the enrolled face
    presence_interval_s: int = 60
    presence_absent_strikes: int = 2  # consecutive absent probes before the PC locks
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
    # fast    = challenge only on doubt (subsecond when confident + clean) -- LESS SECURE: a clean
    #           replay with a strong margin passes without any head movement (F-114)
    # paranoid = require the two-movement head gesture on every valid match (Stage 9, R4:
    #           the default for new installs; an existing config.toml keeps its own value)
    liveness_mode: str = "paranoid"
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
    # cannot inject. The ceiling FOLLOWS the threshold (D-80): raising the threshold, or narrowing
    # the margin, raises the ceiling toward (and past) the replay distance -- at threshold 0.33 it
    # is already 0.16. Widening the margin moves it away. Keep the pair as it is.
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
    # --- Stage 4 pipe perimeter: no knobs since Stage 9 (R2) ---
    # pipe_hardened_sd, pipe_first_instance and pipe_unlock_require_system are gone. The pipe is
    # always hardened (owner + SYSTEM only, NETWORK denied, remote clients rejected), the name is
    # always claimed first-instance, clients always check the server, and unlock / unlock_gesture
    # always require SYSTEM. An old config.toml that still carries the keys loads with an
    # "unknown key" warning; nothing else changes.
    # UI language code (see face_service.i18n.LANGUAGES). Auto-detected
    # from the system locale on first run if the config file is missing.
    language: str = field(default_factory=_default_language)
    # --- Stage 6: event-notification gates (tray toasts, presence_monitor UI) ---
    # (Stage 9, D-96: notify_enroll is gone -- the wizard is its own process and never had a tray
    # icon to toast through; it shows the result itself. An old file with the key loads with an
    # "unknown key" warning.)
    notify_lockout: bool = True         # toast when a face-lockout episode starts
    notify_service_state: bool = False  # toast on service reachable<->unreachable transitions
    # Stage 9 (act 9b R15): the background check for a newer release -- at most once per 24 h,
    # silent on "nothing published" and network errors. Off = only the manual menu item asks.
    update_check: bool = True
    # False = presence off: no camera probe at all (the monitor only polls status for its
    # notifications). True = walk-away lock. Stage 9 (R10): off by default. Face sign-in works
    # either way.
    auto_lock: bool = False
    # --- Stage 7h: frame dump (observability; diagnostics only, OFF by default) ---
    # Write every frame the verify burst and the presence probe are ABOUT TO analyse into
    # APP_DIR/debug_frames -- a .npy with the raw array plus a .png beside it to look at,
    # ring-pruned so the directory stays bounded. It exists because a service can fail while
    # SEEING NOTHING (KNOWN_ISSUES #5): "no face" in the log cannot tell a blind capture apart
    # from an empty room, and every other signal on that path is derived from the same frame.
    # The dump is a pure OBSERVER -- it is handed the frame before the engine runs, never reads
    # a result, and can never change an authentication answer.
    # Leave it off unless a diagnosis needs it: the files are RAW IMAGES OF A FACE, i.e.
    # biometric data at rest (tools/uninstall.ps1 classifies the directory as such).
    debug_dump_frames: bool = False

    @classmethod
    def _degraded(cls, reason: str, strict: bool) -> "Config":
        """Answer a broken config file: raise under ``strict``, else log and return defaults."""
        if strict:
            raise ValueError(reason)
        log.error("%s -- falling back to built-in defaults", reason)
        return cls()

    @classmethod
    def _coerce(cls, values: dict) -> dict:
        """An integral float in an int field (lockout_seconds = 300.0) is that int (B3-04)."""
        types = {f.name: f.type for f in fields(cls)}
        out = {}
        for k, v in values.items():
            if (types.get(k) == "int" and isinstance(v, float) and not isinstance(v, bool)
                    and math.isfinite(v) and v.is_integer()):
                v = int(v)
            out[k] = v
        return out

    @classmethod
    def from_values(cls, values: dict) -> "tuple[Config, dict]":
        """Build a valid Config from ``values``, key by key (Stage 9, act 9b R9 / F-102).

        The whole set is tried first. When it does not validate, each key is taken over on its
        own and kept only if the result still validates; a key that fails keeps its built-in
        default. Two passes, so a pair that only validates together (verify_frames raised with
        verify_required) is not lost to the order. Returns (config, {key: reason}) for the keys
        that fell back. The defaults are the safe values: liveness_mode "paranoid", anti_screen
        on, the frozen threshold and lockout numbers."""
        values = cls._coerce(values)
        try:
            cfg = cls(**values)
            cfg.validate()
            return cfg, {}
        except (ValueError, TypeError):
            pass
        base = cls()
        accepted: dict = {}
        pending = dict(values)
        errors: dict = {}
        for _ in range(2):
            for k in list(pending):
                try:
                    trial = replace(base, **accepted, **{k: pending[k]})
                    trial.validate()
                except (ValueError, TypeError) as e:
                    errors[k] = str(e)
                    continue
                accepted[k] = pending.pop(k)
                errors.pop(k, None)
        cfg = replace(base, **accepted)
        cfg.validate()
        return cfg, {k: errors.get(k, "invalid") for k in pending}

    @classmethod
    def load(cls, strict: bool = False) -> "Config":
        """Read ``~/.face-unlock/config.toml``, or fall back to built-in defaults.

        DEFAULT (``strict=False``) NEVER RAISES. Every failure mode -- unreadable file, non-UTF-8
        bytes, malformed TOML, a value that fails ``validate()`` -- is logged at ERROR and
        answered with a full default ``Config``. The previous behaviour let the exception out,
        and ``main()`` calls this before the pipe exists, so one stray character in a hand-edited
        file killed the service at startup and the watchdog then restarted it into the same
        failure forever. The watchdog already reasoned its way to this fallback for itself
        (tools/watchdog.py::_load_config); the service now does the same. It is deliberately
        LOUD: a machine silently running on defaults while the user believes their config is
        live is its own defect.

        ``strict=True`` raises ``ValueError`` instead of degrading. That is what the live
        ``reload_config`` path wants: there IS a good config already in effect, so the right
        answer to a bad file is to reject it and keep the old one, not to swap the running
        service onto defaults behind the user's back.

        Unknown keys are dropped in both modes (a knob removed by an upgrade must not break the
        load) but they are now NAMED in a WARNING -- a typo used to be indistinguishable from
        not setting the key at all.

        Stage 9 (act 9b R9 / F-102): an invalid VALUE no longer throws the whole file away. Only
        that key falls back to its built-in default, with an ERROR naming it (from_values); the
        user's other choices -- paranoid mode, a lower threshold, auto_lock -- stay in force. A
        file that is not TOML at all still degrades to the defaults as a whole.

        Stage 9 (F-137): under ``strict`` a MISSING file is a broken file too: the live reload
        keeps the running config instead of swapping the service onto defaults.
        Stage 9 (F-107): "never raises" includes a data directory that cannot be created.
        """
        if not CONFIG_PATH.exists():
            if strict:
                raise ValueError(f"{CONFIG_PATH.name} is missing")
            try:
                APP_DIR.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.error("data directory %s could not be created (%s)", APP_DIR, e)
            return cls()

        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            return cls._degraded(f"{CONFIG_PATH} is not valid UTF-8 ({e})", strict)
        except OSError as e:
            return cls._degraded(f"{CONFIG_PATH} could not be read ({e})", strict)

        try:
            data = tomllib.loads(raw)
        except Exception as e:
            return cls._degraded(f"{CONFIG_PATH} is not valid TOML ({e})", strict)

        known = cls.__dataclass_fields__
        unknown = sorted(k for k in data if k not in known)
        if unknown:
            log.warning("%s: %d unknown key(s) ignored: %s",
                        CONFIG_PATH, len(unknown), ", ".join(unknown))

        values = {k: v for k, v in data.items() if k in known}
        if strict:
            try:
                cfg = cls(**cls._coerce(values))
                cfg.validate()
            except (ValueError, TypeError) as e:
                return cls._degraded(f"{CONFIG_PATH} failed validation ({e})", strict)
            return cfg
        cfg, rejected = cls.from_values(values)
        for k, why in rejected.items():
            log.error("%s: %s = %r is invalid (%s) -- using the built-in default %r for this key "
                      "only", CONFIG_PATH.name, k, values.get(k), why, getattr(cfg, k))
        return cfg

    def save(self, keys=None) -> None:
        """Write this config into config.toml -- as a MERGE (Stage 9, act 9b R9; F-111).

        Only keys that change are written: ``keys`` when given (the tray's language switch passes
        ["language"]), else every key whose value differs from the file -- or, for a key the file
        does not have, from the built-in default. Each is replaced on its own line; the user's
        comments, ordering and unknown keys stay, and defaults are not pinned into the file, so a
        later release's safer default still reaches the user.

        A file that is not valid TOML is NEVER overwritten: it is copied to config.toml.bad and
        ConfigSaveRefused explains what to do (B3-04: the tray's language switch used to turn a
        typo into a file full of defaults).
        """
        if tomli_w is None:
            raise RuntimeError("tomli-w is required to save config (pip install tomli-w)")
        APP_DIR.mkdir(parents=True, exist_ok=True)
        raw = ""
        existing: dict = {}
        if CONFIG_PATH.exists():
            try:
                raw = CONFIG_PATH.read_text(encoding="utf-8")
                existing = tomllib.loads(raw)
            except Exception as e:
                bad = CONFIG_PATH.with_name(CONFIG_PATH.name + ".bad")
                try:
                    bad.write_bytes(CONFIG_PATH.read_bytes())
                except OSError:
                    pass
                raise ConfigSaveRefused(
                    f"{CONFIG_PATH.name} could not be read ({e}); it was NOT overwritten. A copy "
                    f"is in {bad.name}. Fix or delete {CONFIG_PATH.name}, then save again.") from e
        defaults = type(self)()
        names = [f.name for f in fields(self)]
        if keys is None:
            keys = [k for k in names
                    if (k in existing and existing[k] != getattr(self, k))
                    or (k not in existing and getattr(self, k) != getattr(defaults, k))]
        text = _merge_toml(raw, {k: getattr(self, k) for k in keys if k in names})
        # Stage 8b (F-45): a sibling, then a rename -- never a half-written config.toml.
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, CONFIG_PATH)

    def _validate_types(self) -> None:
        """Stage 8b (F-15). Defect: the range checks below compare with < and >, and every
        comparison with NaN is False -- so NaN (and inf, where only one side is bounded) passed
        validation for several knobs, and an int field accepted 2.5 or "3". Consequence: a knob
        such as blink_timeout_s = nan broke the gesture round, and inf where a bound was open
        meant "forever". Fix: one pass by DECLARED type first -- float fields must be real,
        finite numbers (an int is fine), int fields must be ints; bool is neither. The range
        checks after it are unchanged."""
        for f in fields(self):
            v = getattr(self, f.name)
            if f.type == "float":
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ValueError(f"{f.name} must be a finite number, got {v!r}")
            elif f.type == "int":
                if isinstance(v, bool) or not isinstance(v, int):
                    raise ValueError(f"{f.name} must be an integer, got {v!r}")

    def validate(self) -> None:
        from .i18n import LANG_CODES
        self._validate_types()
        # Stage 9 (F-105): upper bounds for the knobs that had none -- the Settings spinboxes
        # enforce them, a hand-edited file did not (a huge max_face_attempts turned the face
        # lockout off). The defaults and the frozen numbers are untouched.
        for name, hi in (("max_face_attempts", 20), ("lockout_seconds", 3600),
                         ("verify_frames", 30), ("audit_max_mb", 100.0),
                         ("presence_interval_s", 3600), ("presence_absent_strikes", 20),
                         ("presence_fullscreen_strikes", 120), ("presence_uncertain_streak", 60),
                         ("presence_confirm_delay_s", 30.0), ("presence_input_idle_s", 3600.0),
                         ("enroll_min_frames", 50), ("enroll_min_sharpness", 5000.0),
                         ("adaptive_max_size", 100), ("adaptive_cooldown_s", 30 * 86400.0),
                         ("camera_warmup_frames", 60)):
            if getattr(self, name) > hi:
                raise ValueError(f"{name} must be <= {hi}")
        # Camera selection and warmup: non-negative integers (bool rejected, as everywhere else).
        # No upper bound on either -- an exotic multi-camera host is legitimate, and a too-large
        # warmup only costs time on open, it cannot mis-verify anyone.
        if isinstance(self.camera_index, bool) or not isinstance(self.camera_index, int):
            raise ValueError("camera_index must be an integer")
        if self.camera_index < 0:
            raise ValueError("camera_index must be >= 0")
        if isinstance(self.camera_warmup_frames, bool) or \
                not isinstance(self.camera_warmup_frames, int):
            raise ValueError("camera_warmup_frames must be an integer")
        if self.camera_warmup_frames < 0:
            raise ValueError("camera_warmup_frames must be >= 0")
        if not isinstance(self.persistent_camera, bool):
            raise ValueError("persistent_camera must be a boolean")
        if not isinstance(self.camera_name, str):
            raise ValueError("camera_name must be a string")
        if len(self.camera_name) > 256 or any(ord(c) < 32 for c in self.camera_name):
            raise ValueError("camera_name must be at most 256 printable characters")
        # Verify burst: the service captures verify_frames frames and needs verify_required of them
        # to match (service.py::_verify). required > frames is not a tuning choice, it is an unlock
        # that can never succeed -- and it used to be accepted in silence.
        if isinstance(self.verify_frames, bool) or not isinstance(self.verify_frames, int):
            raise ValueError("verify_frames must be an integer")
        if self.verify_frames < 1:
            raise ValueError("verify_frames must be >= 1")
        if isinstance(self.verify_required, bool) or not isinstance(self.verify_required, int):
            raise ValueError("verify_required must be an integer")
        if self.verify_required < 1:
            raise ValueError("verify_required must be >= 1")
        if self.verify_required > self.verify_frames:
            raise ValueError(
                "verify_required must be <= verify_frames "
                f"(got required={self.verify_required} > frames={self.verify_frames} "
                "=> no unlock could ever succeed)"
            )
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
        if not (0.0 < self.threshold <= THRESHOLD_MAX):
            raise ValueError(f"threshold must be in (0, {THRESHOLD_MAX}]")
        if self.language not in LANG_CODES:
            raise ValueError(
                f"language must be one of {LANG_CODES}, got {self.language!r}"
            )
        if self.liveness_mode not in LIVENESS_MODES:
            raise ValueError(
                f"liveness_mode must be one of {LIVENESS_MODES}, got {self.liveness_mode!r}"
            )
        # Warmup / liveness / audit toggles: real booleans, same rationale as the pipe and notify
        # gates below -- a stray int or string silently takes a truthy branch and picks the wrong
        # behaviour. anti_screen in particular gates the primary replay defense.
        if not isinstance(self.warmup_on_start, bool):
            raise ValueError("warmup_on_start must be a boolean")
        if not isinstance(self.challenge_on_doubt, bool):
            raise ValueError("challenge_on_doubt must be a boolean")
        if not isinstance(self.anti_screen, bool):
            raise ValueError("anti_screen must be a boolean")
        if self.max_face_attempts < 1:
            raise ValueError("max_face_attempts must be >= 1")
        if self.lockout_seconds < 0:
            raise ValueError("lockout_seconds must be >= 0")
        if not isinstance(self.audit_log, bool):
            raise ValueError("audit_log must be a boolean")
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
        # Checked OUTSIDE the `if self.adaptive_gallery:` branch below on purpose: a non-bool would
        # otherwise decide that branch by truthiness and never be examined at all.
        if not isinstance(self.adaptive_gallery, bool):
            raise ValueError("adaptive_gallery must be a boolean")
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
        # Stage 9 (F-110): the comment always said the black floor sits "well under"
        # low_light_luma_min; now it is checked whenever the low-light gate is on.
        if self.low_light_luma_min > 0 and not (self.camera_black_luma < self.low_light_luma_min):
            raise ValueError("camera_black_luma must be below low_light_luma_min "
                             "(a dark but working camera would count as black)")
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
        # Stage 6: notification gates must be real booleans (same rationale).
        if not isinstance(self.notify_lockout, bool):
            raise ValueError("notify_lockout must be a boolean")
        if not isinstance(self.update_check, bool):
            raise ValueError("update_check must be a boolean")
        if not isinstance(self.notify_service_state, bool):
            raise ValueError("notify_service_state must be a boolean")
        if not isinstance(self.auto_lock, bool):
            raise ValueError("auto_lock must be a boolean")
        # Stage 7h: the diagnostics gate is a real boolean for a sharper reason than the rest --
        # a truthy string or a stray 1 would silently start writing FACE IMAGERY to disk on a
        # machine whose owner never asked for it. Fail loud instead.
        if not isinstance(self.debug_dump_frames, bool):
            raise ValueError("debug_dump_frames must be a boolean")
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


class ConfigSaveRefused(RuntimeError):
    """config.toml could not be parsed, so it was not overwritten (a copy is in .bad)."""


_KEY_LINE = r"(?m)^[ \t]*{key}[ \t]*=.*$"


def _merge_toml(raw: str, updates: dict) -> str:
    """Put ``updates`` into the TOML text ``raw``: replace each key's line in place (keeping a
    trailing comment when the old value was not a string), append keys the file does not have.
    The config is flat (no tables), which is what makes a line-level merge exact."""
    text = raw
    appended = []
    for k, v in updates.items():
        line = tomli_w.dumps({k: v}).strip()
        pat = re.compile(_KEY_LINE.format(key=re.escape(k)))
        m = pat.search(text)
        if m is None:
            appended.append(line)
            continue
        old = m.group(0)
        comment = ""
        rhs = old.split("=", 1)[1]
        if '"' not in rhs and "'" not in rhs and "#" in rhs:
            comment = "   " + rhs[rhs.index("#"):].strip()
        indent = old[:len(old) - len(old.lstrip())]
        text = text[:m.start()] + indent + line + comment + text[m.end():]
    if appended:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "\n".join(appended) + "\n"
    return text
