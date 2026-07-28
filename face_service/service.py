"""Named pipe server exposing face verification + credential retrieval.

Protocol: line-delimited JSON.  Client sends {"cmd": "...", ...}, server replies
with a single JSON line and closes the connection.

Commands:
  {"cmd":"ping"}
      -> {"ok":true,"pong":true}
  {"cmd":"verify"}
      -> {"ok":true,"match":bool,"distance":float,"real":bool,"verdict":str}
                               # "verdict" is the liveness verdict name: PASS | NEEDS_GESTURE |
                               # NOT_LIVE (from the burst), or SKIPPED (camera leased / busy).
                               # It exists because match:false alone is ambiguous -- in paranoid
                               # EVERY recognized face verdicts NEEDS_GESTURE, so a genuine user
                               # and a stranger both report match:false. Diagnostic/telemetry
                               # only: it grants nothing and gates nothing.
  {"cmd":"unlock"}             # verify + return credentials on success
                               # UNCHANGED by the verify addition above: unlock keeps its own,
                               # older discriminator (reason "needs-gesture" + gesture/prompt/
                               # token below), and no verdict field was added to any reply here.
      -> {"ok":true,"username":"...","password":"...","domain":"..."}  (on match)
      -> {"ok":false,"reason":"needs-gesture","gesture":"blink|turn_left|turn_right|nod",
          "prompt":str,"token":"<32 hex>","ttl_s":float,"distance":float,"real":bool}
                               # Stage 7-i phase 1: recognized, but liveness wants an ACTIVE
                               # gesture -> answer with `unlock_gesture` carrying the token.
      -> {"ok":false,"reason":"..."}   # "no-match" | "no-credentials" | "locked-out" (+retry_after_s)
                                       # | "not-authorized" | "camera-busy" | "too-dark"
  {"cmd":"unlock_gesture","token":"<32 hex>"}   # Stage 7-i phase 2: run the gesture phase 1
                               # asked for; credentials only if it is performed BY THE FACE
                               # THAT MATCHED (non-matching frames are dropped, not fed).
      -> {"ok":true,"username":"...","password":"...","domain":"..."}
      -> {"ok":false,"reason":"gesture-failed","challenge":str,"state":str,"identity_frames":int}
      -> {"ok":false,"reason":"gesture-token-invalid"}   # absent / wrong / expired / already used
      -> {"ok":false,"reason":"..."}   # "not-authorized" | "locked-out" (+retry_after_s)
                                       # | "camera-busy" | "no-credentials"
  {"cmd":"reset_lockout"}      # clear the face-auth lockout early (admin / tray / test)
      -> {"ok":true,"lockout":{...}}
  {"cmd":"presence"}           # single-frame presence probe
      -> {"ok":true,"present":bool,"real":bool,"mode":"recognition|detection"}
  {"cmd":"challenge","kind":"blink|turn_left|turn_right|nod"}   # active-gesture probe
                               # ("kind" optional -> random). Dev/diagnostic only: NOT wired to
                               # unlock and NOT identity-bound -- the authenticating gesture
                               # round is `unlock_gesture` above.
      -> {"ok":true,"challenge":str,"prompt":str,"passed":bool,"state":str}
  {"cmd":"status"}             # service metadata for GUI
      -> {"ok":true,"uptime_s":float,"config":{...},"enrollment":bool,
          "lockout":{...},"audit":{...}}
  {"cmd":"reload_config"}      # re-read config.toml from disk
      -> {"ok":true,"config":{...}}
  {"cmd":"pause_camera","seconds":120}   # release webcam for N seconds so
                                          # the enrollment GUI can own it
      -> {"ok":true,"paused_until":float}
  {"cmd":"resume_camera"}                 # clear the camera lease early
      -> {"ok":true}
  {"cmd":"build_enrollment"}              # (re)compute embeddings from ENROLL_DIR
      -> {"ok":true,"count":int} | {"ok":false,"reason":str}
  {"cmd":"shutdown"}           # stop the service cleanly (tray Quit uses this)
      -> {"ok":true,"shutting_down":true}
"""
from __future__ import annotations
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict
from typing import Callable, NamedTuple

import pywintypes  # type: ignore
import win32api  # type: ignore
import win32con  # type: ignore
import win32event  # type: ignore
import win32file  # type: ignore
import win32pipe  # type: ignore
import win32security  # type: ignore
import winerror  # type: ignore


def win32api_get_last_error() -> int:
    return win32api.GetLastError()

from .camera import Camera
from .config import Config, LOG_PATH, LOCKOUT_PATH, AUDIT_PATH, PIPE_NAME
from .credentials import load_password
from .detector import FaceDetector
from .audit import AuditLog
from .liveness import BlinkDetector, SCREEN_DOUBT_FRAC, Verdict, verdict
from .lockout import Lockout
from .lowlight import evaluate_low_light, scene_luma
from .camera_boost import try_exposure_boost
from .camera_open import open_with_retry
from .recognizer import Recognizer

log = logging.getLogger(__name__)

# Pause between bounded camera-open attempts (Stage 3.4). The attempt count / total budget are
# config (camera_open_retries / camera_open_timeout_s); this small inter-attempt pause is fixed.
CAMERA_OPEN_PAUSE_S = 0.3


# Lifetime of a phase-1 gesture token (Stage 7-i). PROTOCOL knob, not a liveness threshold: it
# bounds how long the lockscreen has to come back with `unlock_gesture` after being told which
# gesture to perform. Long enough for the user to read the prompt and react, short enough that a
# token left behind on an abandoned lockscreen is dead within seconds. Checked at REQUEST time
# only -- the round itself is then bounded by its own wall-clock cap in _run_challenge, so a
# gesture that starts in time is never cut short by this.
GESTURE_TOKEN_TTL_S = 15.0

# Well-known SID for the lockscreen Credential Provider: LogonUI loads the CP DLL as SYSTEM.
SYSTEM_SID_STRING = "S-1-5-18"

# CreateNamedPipe openMode flag (anti-squatting, Stage 4 Step 4): CreateNamedPipe fails if an
# instance of the name already exists. pywin32 312 does not export it, so define the literal.
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000


def _current_user_sid_string() -> str:
    """String SID (S-1-5-21-...) of the account this process runs as (SELF).

    OpenProcessToken(current, TOKEN_QUERY) -> GetTokenInformation(TokenUser) -> ConvertSidToStringSid.
    Raises on failure so a missing/broken SID fails loud at pipe creation rather than silently
    dropping back to an unusable descriptor.
    """
    th = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(th)
    return win32security.ConvertSidToStringSid(sid)


def _pipe_client_sid_string(handle) -> "str | None":
    """String SID of the process on the CLIENT end of a connected pipe handle, or None on any
    failure (client already gone, cannot impersonate, ...).

    Uses ImpersonateNamedPipeClient: the pipe subsystem hands the server the client's token
    directly, so the caller's SID is read WITHOUT OpenProcess. That matters because the service runs
    as a Limited user, and OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) on a SYSTEM client (the
    lockscreen CP inside LogonUI) is denied -- the old GetNamedPipeClientProcessId->OpenProcess chain
    therefore resolved a genuine SYSTEM caller to None and wrongly failed the SID-gate. Reading the
    SID needs no privilege (SecurityIdentification suffices; the CP opens the pipe without
    SECURITY_SQOS_PRESENT, so the default SecurityImpersonation is offered). The unlock handler has
    already read the request off this handle before the gate runs, so the impersonation precondition
    (a message must have been read) is met, and impersonation happens on that same serve thread.

    RevertToSelf is MANDATORY on every exit path: otherwise this serve thread would keep running
    under the client's token and the subsequent WriteFile / next connection would use it."""
    impersonated = False
    try:
        win32security.ImpersonateNamedPipeClient(handle)
        impersonated = True
        th = win32security.OpenThreadToken(win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True)
        try:
            sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(th)
        return win32security.ConvertSidToStringSid(sid)
    except Exception:
        return None
    finally:
        if impersonated:
            try:
                win32security.RevertToSelf()
            except Exception:
                pass


def _client_image_name(pid) -> str:
    """Base image name (e.g. 'LogonUI.exe') for a PID via a Toolhelp snapshot. Needs NO OpenProcess,
    so it resolves SYSTEM processes a Limited-user service cannot open (which is the whole point:
    a rejected SYSTEM caller still shows its image). '?' on any failure; never raises."""
    try:
        import ctypes
        from ctypes import wintypes
        TH32CS_SNAPPROCESS = 0x00000002

        class _PE32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        k = ctypes.windll.kernel32
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == -1:
            return "?"
        try:
            e = _PE32W()
            e.dwSize = ctypes.sizeof(_PE32W)
            if k.Process32FirstW(snap, ctypes.byref(e)):
                while True:
                    if e.th32ProcessID == pid:
                        return e.szExeFile or "?"
                    if not k.Process32NextW(snap, ctypes.byref(e)):
                        break
        finally:
            k.CloseHandle(snap)
    except Exception:
        pass
    return "?"


def _integrity_label(token) -> str:
    """Map a token's mandatory-integrity RID to a label ('Medium'/'System'/...). '?' on failure."""
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)[0]
        rid = sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
        return {0x0000: "Untrusted", 0x1000: "Low", 0x2000: "Medium", 0x2100: "MediumPlus",
                0x3000: "High", 0x4000: "System", 0x5000: "Protected"}.get(rid, hex(rid))
    except Exception:
        return "?"


def _pipe_client_diag(handle) -> str:
    """Non-sensitive description of the CLIENT process for the unlock-rejection log:
    'pid=<n> image=<exe> integrity=<level> session=<n> openable=<yes|no>'. Diagnostic only -- it
    logs no password or request content, and never raises (every field degrades to '?').

    Only 'image' resolves WITHOUT OpenProcess (Toolhelp snapshot), so it is the one field that
    survives a SYSTEM client (LogonUI) that a Limited-user service cannot OpenProcess. 'session' is
    NOT free: ProcessIdToSessionId needs the same PROCESS_QUERY_LIMITED_INFORMATION access, so on
    such a client it degrades to '?' (a live log confirmed this -- an earlier version of this
    docstring wrongly grouped session with image).
    'openable=no' on such a client is itself the tell that the caller outranks the service -- i.e.
    image=LogonUI.exe + sid=None + openable=no == the real lockscreen CP, whereas
    image=unlock_harness.exe + sid=<user> + openable=yes == the SELF test harness."""
    try:
        pid = win32pipe.GetNamedPipeClientProcessId(handle)
    except Exception:
        return "pid=? (client-pid-unavailable)"
    image = _client_image_name(pid)
    session = "?"
    try:
        import win32ts  # type: ignore
        session = str(win32ts.ProcessIdToSessionId(pid))
    except Exception:
        pass
    integrity = "?"
    openable = "no"
    try:
        ph = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        openable = "yes"
        try:
            th = win32security.OpenProcessToken(ph, win32con.TOKEN_QUERY)
            try:
                integrity = _integrity_label(th)
            finally:
                win32api.CloseHandle(th)
        finally:
            win32api.CloseHandle(ph)
    except Exception:
        pass
    return f"pid={pid} image={image} integrity={integrity} session={session} openable={openable}"


def _build_sa_everyone_legacy() -> win32security.SECURITY_ATTRIBUTES:
    """LEGACY (rollback path, cfg.pipe_hardened_sd=False): NULL DACL = allow ALL (Everyone).

    Kept verbatim so the hardened default in _build_pipe_sa can be turned off without a code change.
    """
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, None, 0)  # NULL DACL = allow all (OK for named pipe on localhost)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


def _build_pipe_sa(cfg: Config) -> win32security.SECURITY_ATTRIBUTES:
    """Security attributes for the named pipe.

    cfg.pipe_hardened_sd=False -> legacy NULL DACL (Everyone), for rollback.
    True (default) -> an explicit descriptor built from an SDDL string:
      * DACL: SELF (this user) = GENERIC_ALL -- owner/server; GA is needed so the per-connection
              re-create of the pipe instance works under the SELF token (FILE_CREATE_PIPE_INSTANCE).
              SYSTEM = GENERIC_READ|GENERIC_WRITE -- the lockscreen CP (LogonUI) connects as SYSTEM
              (not bare GR: a duplex client needs read+write+SYNCHRONIZE, which GRGW maps in).
              NO Everyone ACE -> every OTHER non-admin user is denied by the implicit deny.
      * SACL: a Medium mandatory label with NoReadUp+NoWriteUp -> a same-user LOW-integrity process
              cannot read/write the pipe; SYSTEM and our Medium clients sit at/above Medium so they
              pass. ME (Medium) is deliberate -- labelling LW (Low) would defeat the point.
    SetEntriesInAcl is not exported by pywin32, so the descriptor is assembled from SDDL via
    ConvertStringSecurityDescriptorToSecurityDescriptor (present in pywin32 312).
    """
    if not cfg.pipe_hardened_sd:
        return _build_sa_everyone_legacy()
    self_sid = _current_user_sid_string()
    sddl = (
        f"D:(A;;GA;;;{self_sid})(A;;GRGW;;;{SYSTEM_SID_STRING})"
        "S:(ML;;NRNW;;;ME)"
    )
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


class VerifyOutcome(NamedTuple):
    """Result of one capture burst: the legacy 3-tuple plus a detail dict for the audit log."""
    match: bool
    distance: float
    real: bool
    detail: dict
    embedding: "np.ndarray | None" = None   # best-matching frame's 512-D embedding (adaptive gallery)
    scene_luma: "float | None" = None        # brightest scene luma over the burst (Stage 3.2 low-light gate)
    camera_busy: bool = False                # Stage 3.4: open failed -- device held by another process


class FaceService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.recog = Recognizer(cfg)
        self.detector = FaceDetector()
        # Persistent consecutive-failure lockout for the face path (PIN stays available).
        self._lockout = Lockout(LOCKOUT_PATH, cfg.max_face_attempts, cfg.lockout_seconds)
        # Structured JSONL audit trail (verify/unlock/challenge; never stores the password).
        self._audit = AuditLog(AUDIT_PATH, cfg.audit_max_mb, enabled=cfg.audit_log)
        self._stop = threading.Event()
        # win32-level stop signal (parity with _stop); set alongside it so a future win32 wait can
        # observe the stop too. The loop itself is driven by _stop; ConnectNamedPipe is unblocked by
        # the self-connect in stop().
        self._stop_event = win32event.CreateEvent(None, True, False, None)
        self._ctrl_handler = None   # keep a ref so SetConsoleCtrlHandler's callback isn't GC'd
        self._cam_lock = threading.Lock()
        self._cam: Camera | None = None  # kept open when persistent_camera=True
        self._started_at = time.time()
        # When the enrollment wizard is running it needs exclusive access to
        # the webcam. ``_camera_paused_until`` holds an epoch timestamp: probes
        # and verify calls short-circuit until that time passes. The service
        # also releases the persistent camera so the tray can open it.
        self._camera_paused_until: float = 0.0
        # Stage 7-i gesture round: the single outstanding phase-1 token, or None. ONE slot is
        # enough because the pipe server is strictly sequential (_serve_one handles one request
        # at a time), so two unlocks can never be in flight together; a newer unlock simply
        # replaces the older token. Shape: {"token": str, "kind": str, "expires": monotonic}.
        self._gesture_slot: dict | None = None

    def _camera_leased_out(self) -> bool:
        return time.time() < self._camera_paused_until

    def _acquire_camera(self):
        """Open the webcam with a bounded, non-hanging retry (Stage 3.4).

        Returns ``(camera, busy)``: ``(Camera, False)`` on success, ``(None, True)`` when the device
        is busy (held by ANOTHER process -- distinct from our own enrollment lease, which the callers
        check first). Never raises, never hangs (Camera.open_fast + open_with_retry). For the
        persistent camera ``self._cam`` is set ONLY on success, so a failed open leaves it None and
        the next call retries cleanly instead of returning a stuck half-open handle. The caller must
        already hold ``self._cam_lock``.
        """
        if self.cfg.persistent_camera and self._cam is not None:
            return self._cam, False
        cam = Camera(self.cfg.camera_index, self.cfg.camera_warmup_frames)
        ok = open_with_retry(
            cam.open_fast,
            retries=self.cfg.camera_open_retries,
            pause_s=CAMERA_OPEN_PAUSE_S,
            timeout_s=self.cfg.camera_open_timeout_s,
            clock=time.monotonic,
            sleep=time.sleep,
        )
        if not ok:
            return None, True
        if self.cfg.persistent_camera:
            self._cam = cam   # persist ONLY on success -> no stuck half-open handle
        return cam, False

    def _release_camera(self) -> None:
        """Drop the persistent camera (idempotent, never raises).

        Swap-then-release like ``Camera.close``: ``self._cam`` is nulled BEFORE
        the close runs, so a raising close cannot leave a dead handle behind for
        ``_acquire_camera``'s reuse gate to hand back with ``busy=False``.
        """
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                log.exception("persistent camera release failed")

    # ---------- core ops ----------

    def _capture_and_verify(self) -> VerifyOutcome:
        """Capture a short burst, fold recognition + passive liveness into a verdict.

        One detect per frame via ``analyze_frame`` -> match/distance + 2d106 landmarks (blink)
        + an anti-screen vote. After the burst ``liveness.verdict`` combines them (mode-aware).
        Returns a ``VerifyOutcome``: the legacy ``(match, distance, real)`` plus a ``detail`` dict
        for the audit log. Only a ``PASS`` verdict yields ``match=True``; ``NEEDS_GESTURE`` and
        ``NOT_LIVE`` both map to a deny here (the Stage-2 lockscreen is PASSIVE and cannot run a
        gesture yet, so anything that needs one falls back to PIN). ``real`` = the passive
        anti-screen did NOT suspect a screen. Active gesture escalation is exposed separately via
        the ``challenge`` command for the Stage-5 Credential Provider. Runs the full burst (no
        early-exit) so every frame gets a chance to flag a screen and to catch a spontaneous
        blink; that burst is still subsecond.
        """
        if self._camera_leased_out():
            log.info("verify skipped: camera leased out to enrollment")
            return VerifyOutcome(False, 1.0, False,
                                 {"verdict": "SKIPPED", "reason": "camera-leased"})

        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("verify skipped: camera busy (held by another process)")
                return VerifyOutcome(False, 1.0, False,
                                     {"verdict": "SKIPPED", "reason": "camera-busy"},
                                     camera_busy=True)
            try:
                return self._analyze_burst(cam)
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()

    def _analyze_burst(self, cam) -> VerifyOutcome:
        """Run ONE analysis burst over an already-open ``cam``. The caller owns the camera lock and
        the open/close lifecycle. Extracted verbatim from ``_capture_and_verify`` so the Step-3.3
        low-light boost can re-run the SAME burst under boosted exposure with zero duplication.
        """
        t0 = time.monotonic()
        matches = 0
        best = 1.0
        best_emb = None
        screen_flagged = 0
        screen_checked = 0
        scene_luma_max = None   # brightest scene luma seen this burst (Stage 3.2); None if no frame read
        blink = BlinkDetector()

        # drain stale buffered frames
        for _ in range(2):
            cam.read()
        for _ in range(self.cfg.verify_frames):
            frame = cam.read()
            if frame is None:
                continue
            # Scene brightness is face-INDEPENDENT: compute it for every captured frame
            # (incl. no-face ones) and keep the MAX, so a transient dip or a frame where
            # the face was briefly lost can't trip the low-light gate on its own.
            sl = scene_luma(frame)
            scene_luma_max = sl if scene_luma_max is None else max(scene_luma_max, sl)
            try:
                a = self.recog.analyze_frame(frame)
            except Exception as e:
                log.warning("verify error: %s", e)
                continue
            if not a.face:
                continue
            if a.distance < best:
                best = a.distance
                best_emb = a.embedding
            if a.is_match:
                matches += 1
            if a.screen is not None:
                screen_checked += 1
                if a.screen:
                    screen_flagged += 1
            if a.landmark is not None:
                blink.update(a.landmark)   # accumulate spontaneous blinks over the burst

        screen_frac = (screen_flagged / screen_checked) if screen_checked else 0.0
        margin = self.cfg.threshold - best
        v = verdict(
            matches=matches,
            blinked=blink.blinks > 0,
            screen_frac=screen_frac,
            margin=margin,
            mode=self.cfg.liveness_mode,
            required_matches=self.cfg.verify_required,
            challenge_on_doubt=self.cfg.challenge_on_doubt,
        )
        is_real = screen_frac < SCREEN_DOUBT_FRAC
        latency_ms = (time.monotonic() - t0) * 1000.0
        log.info(
            "verify verdict=%s matches=%d/%d best=%.3f margin=%.3f blink=%d screen=%d/%d "
            "sceneL=%s mode=%s",
            v.name, matches, self.cfg.verify_required, best, margin,
            blink.blinks, screen_flagged, screen_checked,
            ("%.1f" % scene_luma_max) if scene_luma_max is not None else "n/a",
            self.cfg.liveness_mode,
        )
        detail = {
            "verdict": v.name,
            "match": v == Verdict.PASS,
            "distance": round(best, 4),
            "margin": round(margin, 4),
            "matches": matches,
            "required": self.cfg.verify_required,
            "blink": blink.blinks,
            "screen_flagged": screen_flagged,
            "screen_checked": screen_checked,
            "scene_luma": round(scene_luma_max, 2) if scene_luma_max is not None else None,
            "mode": self.cfg.liveness_mode,
            "latency_ms": round(latency_ms, 1),
        }
        return VerifyOutcome(v == Verdict.PASS, best, is_real, detail, best_emb, scene_luma_max)

    def _maybe_boost(self, r_dark: "VerifyOutcome"):
        """Gated low-light exposure boost (Step 3.3). Called ONLY from unlock, ONLY when the first
        burst was below the floor and ``cfg.low_light_boost`` is on. Raises webcam EXPOSURE, re-runs
        the SAME analysis burst, and ALWAYS restores exposure (camera_boost.try_exposure_boost's
        finally) so the long-lived camera is never left boosted for the next unlock / presence loop.

        Returns ``(outcome, boost_audit)``: the boosted re-capture when it was applied, else the
        original dark outcome; ``boost_audit`` is additive telemetry (applied / honored / exposure
        + scene before/after) for the audit log. Best-effort -- never raises.
        """
        boost_audit = {"boost_applied": False, "boost_honored": False,
                       "scene_luma_before": round(r_dark.scene_luma, 2)}
        try:
            with self._cam_lock:
                cam, busy = self._acquire_camera()
                if busy:
                    return r_dark, boost_audit   # device grabbed by another process; skip boost
                try:
                    cap = getattr(cam, "_cap", None)
                    if cap is None:
                        return r_dark, boost_audit
                    out = try_exposure_boost(
                        cap, self.cfg.low_light_exposure_step,
                        lambda: self._analyze_burst(cam),
                    )
                finally:
                    if not self.cfg.persistent_camera:
                        cam.close()
        except Exception as e:   # pragma: no cover - defensive; boost must never break unlock
            log.warning("low-light boost skipped: %s", e)
            return r_dark, boost_audit

        boost_audit.update(out.audit())
        if out.applied and out.recapture is not None:
            rc = out.recapture
            boost_audit["scene_luma_after"] = (
                round(rc.scene_luma, 2) if rc.scene_luma is not None else None)
            log.info("low-light boost: sceneL %.1f -> %s match=%s (exposure %.1f->%.1f honored=%s)",
                     r_dark.scene_luma,
                     ("%.1f" % rc.scene_luma) if rc.scene_luma is not None else "n/a",
                     rc.match, out.exposure_before, out.exposure_readback, out.honored)
            return rc, boost_audit
        log.info("low-light boost not applied (honored=%s); keeping dark capture (sceneL=%.1f)",
                 out.honored, r_dark.scene_luma)
        return r_dark, boost_audit

    def _maybe_adapt_gallery(self, r: "VerifyOutcome") -> None:
        """Opt-in adaptive gallery: on a genuine, live, non-screen unlock, offer the
        verifying embedding to the recognizer, which applies the anti-poisoning gates
        (distance-to-enrollment ceiling, cooldown, size cap) and persists it separately.
        Best-effort: never let adaptation break an unlock."""
        if not self.cfg.adaptive_gallery:
            return
        try:
            dec = self.recog.maybe_adapt(
                r.embedding,
                liveness_passed=True,                             # r.match == verdict PASS => live for the mode
                is_screen=r.detail.get("screen_flagged", 0) > 0,  # ANY screen flag blocks adaptation
                mode=self.cfg.liveness_mode,
                gesture_passed=False,                             # passive unlock path runs no gesture
                union_distance=r.distance,
            )
            self._audit.write("adapt", {"accept": dec.accept, "reason": dec.reason,
                                        "ceiling": round(dec.ceiling, 4)})
        except Exception as e:
            log.warning("adaptive update skipped: %s", e)

    def _release_credentials(self) -> "dict | None":
        """THE single point at which stored credentials leave this service.

        Both grant paths (`unlock` on a PASS verdict, `unlock_gesture` on a passed identity-bound
        gesture) funnel through here, so there is exactly one place to audit when asking "where
        can the password get out?". Returns the ready success payload, or None when there is no
        usable stored blob -- the caller maps that to reason "no-credentials" and owns its own
        audit/lockout bookkeeping (the two commands write different audit events).

        Deliberately does NOT gate on anything itself: every precondition (SYSTEM caller, lockout,
        camera, verdict / gesture outcome) is enforced by the caller BEFORE this is reached.
        """
        creds = load_password()
        if not creds:
            return None
        return {
            "ok": True,
            "username": creds["u"],
            "password": creds["p"],
            "domain": creds.get("d", "."),
        }

    def _run_challenge(self, kind_name: str | None = None, *, identity: bool = False) -> dict:
        """Server-side active-gesture loop.

        Issues one challenge (random, or the requested ``kind``: blink|turn_left|turn_right|nod)
        and drives it to PASS/FAIL over camera frames using ``analyze_frame`` landmarks/pose.
        The pipe server is sequential, so this holds the camera for the duration of the gesture.

        ``identity=False`` (the `challenge` command) is the original diagnostic behaviour, byte
        for byte: every analyzed frame is fed to the task and the reply carries no extra keys.

        ``identity=True`` (Stage 7-i `unlock_gesture`) binds the gesture to the RECOGNIZED face:
        a frame whose embedding does not match is dropped outright -- not fed to the task at all.
        Without that, the two signals are independent and separable: a photo of the enrolled user
        supplies the matching frames while a live impostor beside it supplies the motion, and the
        round passes. Dropping the frame closes the seam, and because the task's deadline runs on
        wall-clock, non-matching frames still burn the budget -- which IS the binding. Adds
        ``identity_frames`` (matching frames actually fed) and ``distance_best`` to the reply.
        """
        from .liveness import Challenge, GESTURE_TIMEOUT_S, LivenessChallenge

        if self._camera_leased_out():
            return {"ok": False, "reason": "camera-busy"}

        kind = None
        if kind_name:
            try:
                kind = Challenge[str(kind_name).upper()]
            except KeyError:
                return {"ok": False, "reason": f"unknown-kind: {kind_name}"}

        ch = LivenessChallenge()
        issued = ch.issue(kind)
        # Wall-clock safety cap: each task also self-times-out on its own deadline when fed, but
        # if the camera stalls we must not block the pipe forever.
        wall_deadline = time.monotonic() + self.cfg.blink_timeout_s + GESTURE_TIMEOUT_S + 2.0

        identity_frames = 0            # matching frames actually fed to the task (identity mode)
        distance_best: float | None = None   # best distance over every frame that HAD a face

        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                return {"ok": False, "reason": "camera-busy"}
            try:
                for _ in range(2):
                    cam.read()
                while not ch.done and time.monotonic() < wall_deadline:
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        a = self.recog.analyze_frame(frame)
                    except RuntimeError as e:      # e.g. no enrollment
                        return {"ok": False, "reason": str(e)}
                    except Exception as e:
                        log.warning("challenge analyze error: %s", e)
                        continue
                    if a.face and (distance_best is None or a.distance < distance_best):
                        distance_best = a.distance
                    if identity:
                        # Identity binding: a non-matching frame is NOT the enrolled user, so it
                        # does not exist as far as the gesture task is concerned. Skipping the
                        # feed (rather than merely not counting it) is the point -- it stops an
                        # impostor's motion from ever reaching the task.
                        if not a.is_match:
                            continue
                        identity_frames += 1
                    ch.feed(a.landmark, a.pose)
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()

        log.info("challenge kind=%s passed=%s state=%s%s",
                 issued.name.lower(), ch.passed, ch.state.name,
                 (" identity_frames=%d best=%s" %
                  (identity_frames,
                   "n/a" if distance_best is None else "%.3f" % distance_best)) if identity else "")
        resp = {
            "ok": True,
            "challenge": issued.name.lower(),
            "prompt": ch.prompt,
            "passed": ch.passed,
            "state": ch.state.name.lower(),
        }
        if identity:
            # Extra keys ONLY in identity mode, so the `challenge` command's reply is unchanged.
            resp["identity_frames"] = identity_frames
            resp["distance_best"] = (None if distance_best is None else round(distance_best, 4))
        return resp

    def _prompt_for(self, kind_name: str) -> str:
        """Localized gesture prompt for the CURRENT cfg.language.

        Mirrors i18n.t()'s fallback chain (language -> English -> raw key) but resolves the
        language from cfg on every call instead of i18n's process-global _current_lang: this
        service never calls set_language (only the tray process does), and reload_config can
        change cfg.language at runtime, so anything cached at startup would go stale.
        """
        from .i18n import DEFAULT_LANG, TRANSLATIONS
        key = f"gesture.prompt.{kind_name}"
        table = TRANSLATIONS.get(self.cfg.language) or TRANSLATIONS[DEFAULT_LANG]
        return table.get(key) or TRANSLATIONS[DEFAULT_LANG].get(key, key)

    def _issue_gesture_token(self) -> tuple[str, str, str]:
        """Pick a random gesture, arm the one-shot token slot, return (kind, prompt, token).

        The kind is drawn with `secrets` (not `random`) so an observer cannot predict which
        gesture the next lockscreen attempt will demand. Overwrites any previous slot: the newest
        phase-1 reply is the only one that can be answered.
        """
        from .liveness import ALL_KINDS
        kind = secrets.choice(ALL_KINDS).name.lower()
        token = secrets.token_hex(16)          # 32 hex chars
        self._gesture_slot = {"token": token, "kind": kind,
                              "expires": time.monotonic() + GESTURE_TOKEN_TTL_S}
        return kind, self._prompt_for(kind), token

    def _take_gesture_token(self, token) -> "dict | None":
        """Validate AND consume the one-shot gesture token; returns the slot, or None.

        Burning the slot before the round runs is what makes the token one-shot: a FAILED round
        cannot be retried on the same token, it costs a fresh phase 1. A wrong token deliberately
        does NOT clear a live slot, so a bogus request cannot cancel a legitimate pending gesture.
        compare_digest keeps the comparison constant-time -- this is a bearer token.
        """
        slot = self._gesture_slot
        if slot is None:
            return None
        if time.monotonic() >= slot["expires"]:
            self._gesture_slot = None          # expired: drop the dead slot
            return None
        # isascii() before compare_digest: on str inputs compare_digest REJECTS non-ASCII with a
        # TypeError, and this value comes straight off the wire -- without the guard a token of
        # "é" would raise out of the handler and answer "exception: ...". Our tokens are hex.
        if (not isinstance(token, str) or not token.isascii()
                or not secrets.compare_digest(token, slot["token"])):
            return None
        self._gesture_slot = None              # one-shot: burn BEFORE the round runs
        return slot

    def _audit_gesture(self, *, challenge=None, passed=None, identity_frames=None,
                       distance_best=None, reason=None) -> None:
        """One `unlock_gesture` audit record, with a STABLE key set.

        Every branch writes all five fields (absent ones as null) so the JSONL stays greppable
        without per-branch shape guessing. The token is NEVER recorded.
        """
        self._audit.write("unlock_gesture", {
            "challenge": challenge,
            "passed": passed,
            "identity_frames": identity_frames,
            "distance_best": distance_best,
            "reason": reason,
        })

    def _presence_probe(self) -> tuple[bool, bool]:
        """(present, real) — semantics depend on config.presence_mode.

        recognition: ``present`` = enrolled face detected AND passes anti-spoofing.
        detection:   ``present`` = *any* face detected by YuNet. ``real`` is
                     reported as True (anti-spoofing not evaluated).
        """
        if self._camera_leased_out():
            log.info("presence probe skipped: camera leased out to enrollment")
            # Return (True, True) so the monitor doesn't rack up strikes
            # while the user is enrolling their face.
            return True, True
        if self.cfg.presence_mode == "detection":
            return self._presence_probe_detection()
        return self._presence_probe_recognition()

    def _presence_probe_recognition(self) -> tuple[bool, bool]:
        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("presence probe skipped: camera busy (held by another process)")
                return True, True   # like leased: don't rack up absence strikes when we can't see
            try:
                for _ in range(2):
                    cam.read()
                for _ in range(3):
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        ok, _dist, real = self.recog.verify_frame(frame)
                    except Exception:
                        continue
                    if ok:
                        return True, real
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()
        return False, False

    def _presence_probe_detection(self) -> tuple[bool, bool]:
        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("presence probe skipped: camera busy (held by another process)")
                return True, True   # like leased: don't rack up absence strikes when we can't see
            try:
                for _ in range(2):
                    cam.read()
                for _ in range(5):
                    frame = cam.read()
                    if frame is None:
                        continue
                    try:
                        if self.detector.has_face(frame):
                            return True, True
                    except Exception as e:
                        log.warning("detector error: %s", e)
                        continue
            finally:
                if not self.cfg.persistent_camera:
                    cam.close()
        return False, True

    # ---------- pipe ----------

    def _status(self) -> dict:
        from .config import EMBED_PATH
        return {
            "ok": True,
            "uptime_s": time.time() - self._started_at,
            "config": asdict(self.cfg),
            "enrollment": EMBED_PATH.exists(),
            "lockout": self._lockout.status(),
            "audit": self._audit.status(),
        }

    def _reload_config(self) -> dict:
        new_cfg = Config.load()
        try:
            new_cfg.validate()
        except ValueError as e:
            return {"ok": False, "reason": f"invalid-config: {e}"}
        old_index = self.cfg.camera_index
        old_persistent = self.cfg.persistent_camera
        self.cfg = new_cfg
        self.recog.cfg = new_cfg
        self._lockout.reconfigure(new_cfg.max_face_attempts, new_cfg.lockout_seconds)
        self._audit.reconfigure(new_cfg.audit_log, new_cfg.audit_max_mb)
        # Reset camera if camera-affecting settings changed. The new cfg is already
        # applied above, so a failure here must not abort the reload and strand the
        # OLD camera open under the NEW settings -- log it and carry on.
        if new_cfg.camera_index != old_index or new_cfg.persistent_camera != old_persistent:
            try:
                with self._cam_lock:
                    self._release_camera()
            except Exception:
                log.exception("reload_config: releasing the webcam failed")
        return {"ok": True, "config": asdict(new_cfg)}

    def _handle(self, req: dict, handle=None) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True, "pong": True}

        if cmd == "status":
            return self._status()

        if cmd == "reload_config":
            return self._reload_config()

        if cmd == "shutdown":
            log.info("shutdown requested via pipe")
            # Drop a self-expiring pause so the watchdog treats this as a DELIBERATE stop and does
            # not resurrect the service. Expired pauses are ignored+deleted, so this can't silence
            # the watchdog forever (Step 5). Best-effort -- never block the shutdown on it.
            try:
                from .watchdog import write_pause
                from .config import WATCHDOG_PAUSE_PATH
                write_pause(WATCHDOG_PAUSE_PATH, time.time(), self.cfg.watchdog_pause_ttl_s)
            except Exception as e:
                log.warning("watchdog pause write skipped: %s", e)
            self._stop.set()
            try:
                win32event.SetEvent(self._stop_event)
            except Exception:
                pass
            # No self-connect needed here: this shutdown request itself unblocked ConnectNamedPipe,
            # so after this _serve_one() finishes the loop re-checks _stop and exits.
            return {"ok": True, "shutting_down": True}

        if cmd == "pause_camera":
            # Release the webcam and ignore probe/verify for the requested
            # number of seconds so the enrollment wizard can own it.
            seconds = float(req.get("seconds", 120))
            self._camera_paused_until = time.time() + max(5.0, seconds)
            # The deadline above is already live, so probes have stood down and
            # the wizard is being told the device is its. The release must
            # therefore survive any failure: escaping here would leave the webcam
            # with the service while BOTH sides believe it was handed over.
            try:
                with self._cam_lock:
                    self._release_camera()
            except Exception:
                log.exception("pause_camera: releasing the webcam failed")
            log.info("camera leased out for %.0fs (enrollment)", seconds)
            return {"ok": True, "paused_until": self._camera_paused_until}

        if cmd == "resume_camera":
            was = self._camera_paused_until
            self._camera_paused_until = 0.0
            log.info("camera lease cleared (was until %s)", was)
            return {"ok": True}

        if cmd == "build_enrollment":
            try:
                from .config import ENROLL_DIR
                n = self.recog.enroll_from_dir(ENROLL_DIR)
                return {"ok": True, "count": n}
            except Exception as e:
                log.exception("build_enrollment failed")
                return {"ok": False, "reason": str(e)}

        if cmd == "verify":
            r = self._capture_and_verify()
            self._audit.write("verify", r.detail)
            # "verdict" is additive: the four legacy keys keep their names, types and values, so
            # an older client that ignores unknown keys is unaffected. The value is read from the
            # burst detail (which already carries it) with .get(), so a detail dict that somehow
            # lacks one yields null rather than raising out of the handler. `unlock` is NOT given
            # the same field -- its needs-gesture reply already distinguishes the cases, and its
            # shape is part of the Credential Provider contract.
            return {"ok": True, "match": r.match, "distance": r.distance, "real": r.real,
                    "verdict": r.detail.get("verdict")}

        if cmd == "presence":
            present, real = self._presence_probe()
            return {"ok": True, "present": present, "real": real, "mode": self.cfg.presence_mode}

        if cmd == "challenge":
            # Active-gesture stub for the Stage-5 Credential Provider (not wired to unlock).
            resp = self._run_challenge(req.get("kind"))
            self._audit.write("challenge", {k: resp.get(k)
                              for k in ("challenge", "passed", "state", "reason") if k in resp})
            return resp

        if cmd == "unlock":
            # Stage 4 Step 5 SID-gate (default ON since Stage 5): when enabled, only a SYSTEM caller -- the
            # lockscreen Credential Provider -- may invoke unlock. Runs BEFORE lockout/verify/
            # load_password so no password is ever returned to a non-SYSTEM caller. Only unlock is
            # gated; every other command is scoped by the Batch-1 pipe DACL.
            if self.cfg.pipe_unlock_require_system:
                sid = _pipe_client_sid_string(handle)
                if sid != SYSTEM_SID_STRING:
                    log.warning("unlock rejected: caller not SYSTEM (sid=%s %s)",
                                sid, _pipe_client_diag(handle))
                    return {"ok": False, "reason": "not-authorized"}
            rem = self._lockout.remaining()
            if rem > 0:
                self._audit.write("unlock", {"verdict": "LOCKED_OUT", "match": False,
                                             "retry_after_s": round(rem, 1)})
                return {"ok": False, "reason": "locked-out", "retry_after_s": round(rem, 1)}
            r = self._capture_and_verify()
            # Stage 3.4 busy camera: the webcam is held by ANOTHER process (not our enrollment
            # lease). Refuse cleanly with reason "camera-busy" -- LOCKOUT-NEUTRAL (a busy device is
            # environment, not a failed match) -- instead of the old multi-second RuntimeError that
            # bubbled up as reason "exception: Cannot open camera...". Returns before the lockout
            # counter is touched, before boost, and before the too-dark gate.
            if r.camera_busy:
                self._audit.write("unlock", {**r.detail, "outcome": "camera-busy"})
                return {"ok": False, "reason": "camera-busy"}
            # Stage 3.3 gated exposure boost: if the burst came back below the floor, try to raise
            # EXPOSURE and re-capture BEFORE the too-dark fallback. Gated (only below the floor) so a
            # normally-lit face is never blown out; transient (exposure always restored inside
            # _maybe_boost); lockout-neutral (a still-dark result stays too-dark below, adding no
            # strike). Boost telemetry is merged into r.detail so every unlock audit below carries
            # it unchanged. low_light_boost=False (or scene >= floor / camera leased) -> no camera
            # touch and the path is identical to Step 3.2.
            boost_audit: dict = {}
            if (r.scene_luma is not None and r.scene_luma < self.cfg.low_light_luma_min
                    and self.cfg.low_light_boost and not self._camera_leased_out()):
                r, boost_audit = self._maybe_boost(r)
                r = r._replace(detail={**r.detail, **boost_audit})
            # Stage 3.2 low-light gate. If even the brightest frame of the burst is below the
            # floor, refuse honestly ("too-dark") and stay LOCKOUT-NEUTRAL: darkness is an
            # environment problem, not a failed match, so it must NOT add a lockout strike (nor
            # reset the counter) -- otherwise a user in a dim room would rack up strikes and get
            # face-locked for 300s just for being in the dark (a soft DoS). Above the floor -- or
            # when the gate is disabled (floor 0) or no scene was measured (camera leased / no
            # frame) -- behaviour is byte-for-byte unchanged. (Step 3.3 will insert an exposure
            # boost + re-capture BEFORE this gate; the gate itself does not change.)
            too_dark = False
            if r.scene_luma is not None:
                _grant, ll_reason, too_dark = evaluate_low_light(
                    r.scene_luma, self.cfg.low_light_luma_min, r.match)
            if too_dark:
                self._audit.write("unlock", {**r.detail, "outcome": "too-dark"})
                return {"ok": False, "reason": ll_reason, "distance": r.distance, "real": r.real}
            # Stage 7-i phase 1. NEEDS_GESTURE means "recognized, but liveness wants an active
            # gesture" -- until now indistinguishable from a real no-match on the wire. The
            # verdict name is read straight from the burst detail, which already carries it
            # (_analyze_burst), so VerifyOutcome keeps its shape and the verify/audit records are
            # untouched. .get() is deliberate: a detail dict WITHOUT a verdict (the camera-lease
            # skip writes "SKIPPED", a future path might write nothing) falls through to the old
            # no-match path -- fail closed, never into the gesture path.
            needs_gesture = r.detail.get("verdict") == "NEEDS_GESTURE"
            # Don't count an enrollment-lease skip as a real failed attempt. A gesture escalation
            # is not an attempt either -- it is a question we just asked, and phase 2 records its
            # own outcome; counting it here would burn the whole strike budget on paranoid, where
            # EVERY recognized face escalates.
            if not self._camera_leased_out() and not needs_gesture:
                self._lockout.record(r.match)
            if needs_gesture:
                kind, prompt, token = self._issue_gesture_token()
                # Audit records the gesture but NEVER the token.
                self._audit.write("unlock", {**r.detail, "outcome": "needs-gesture",
                                             "gesture": kind})
                return {"ok": False, "reason": "needs-gesture", "gesture": kind,
                        "prompt": prompt, "token": token, "ttl_s": GESTURE_TOKEN_TTL_S,
                        "distance": r.distance, "real": r.real}
            if not r.match:
                self._audit.write("unlock", {**r.detail, "outcome": "no-match"})
                return {"ok": False, "reason": "no-match", "distance": r.distance, "real": r.real}
            granted = self._release_credentials()
            if granted is None:
                self._audit.write("unlock", {**r.detail, "outcome": "no-credentials"})
                return {"ok": False, "reason": "no-credentials"}
            self._audit.write("unlock", {**r.detail, "outcome": "granted"})
            self._maybe_adapt_gallery(r)
            return granted

        if cmd == "unlock_gesture":
            # Stage 7-i phase 2: answer the gesture phase 1 asked for. The gate ORDER is a clone
            # of unlock's, using the SAME helpers, so this command sits behind exactly the same
            # perimeter and cannot become a way around the SYSTEM gate: identity check first,
            # then lockout, then the one-shot token, and only then the camera. _release_credentials
            # is unreachable until all four have passed.
            if self.cfg.pipe_unlock_require_system:
                sid = _pipe_client_sid_string(handle)
                if sid != SYSTEM_SID_STRING:
                    log.warning("unlock_gesture rejected: caller not SYSTEM (sid=%s %s)",
                                sid, _pipe_client_diag(handle))
                    return {"ok": False, "reason": "not-authorized"}
            rem = self._lockout.remaining()
            if rem > 0:
                self._audit_gesture(reason="locked-out")
                return {"ok": False, "reason": "locked-out", "retry_after_s": round(rem, 1)}
            slot = self._take_gesture_token(req.get("token"))
            if slot is None:
                # Absent / wrong / expired / already-burnt token. NOT a strike: this is a protocol
                # state, not a failed face attempt -- the camera never even opened.
                self._audit_gesture(reason="gesture-token-invalid")
                return {"ok": False, "reason": "gesture-token-invalid"}
            resp = self._run_challenge(slot["kind"], identity=True)
            if not resp.get("ok"):
                # The round never ran. "camera-busy" stays itself and stays lockout-NEUTRAL, like
                # everywhere else (a busy device is environment, not a failed match). Anything
                # else here is an engine-level refusal (e.g. enrollment vanished mid-session);
                # it is reported as a plain gesture-failed -- the wire deliberately does not
                # distinguish failure causes -- with the real reason kept in the log and audit.
                raw = resp.get("reason") or "gesture-failed"
                if raw == "camera-busy":
                    self._audit_gesture(challenge=slot["kind"], reason="camera-busy")
                    return {"ok": False, "reason": "camera-busy"}
                log.warning("unlock_gesture round did not run: %s", raw)
                self._lockout.record(False)
                self._audit_gesture(challenge=slot["kind"], reason=raw)
                return {"ok": False, "reason": "gesture-failed", "challenge": slot["kind"],
                        "state": "failed", "identity_frames": 0}
            frames = int(resp.get("identity_frames") or 0)
            best = resp.get("distance_best")
            # Two conditions, no new numbers: the task itself passed AND enough MATCHING frames
            # were fed (cfg.verify_required, the same bar the passive burst uses).
            passed = bool(resp.get("passed")) and frames >= self.cfg.verify_required
            if not passed:
                self._lockout.record(False)
                self._audit_gesture(challenge=resp.get("challenge"), passed=resp.get("passed"),
                                    identity_frames=frames, distance_best=best,
                                    reason="gesture-failed")
                return {"ok": False, "reason": "gesture-failed",
                        "challenge": resp.get("challenge"), "state": resp.get("state"),
                        "identity_frames": frames}
            self._lockout.record(True)
            granted = self._release_credentials()
            if granted is None:
                self._audit_gesture(challenge=resp.get("challenge"), passed=True,
                                    identity_frames=frames, distance_best=best,
                                    reason="no-credentials")
                return {"ok": False, "reason": "no-credentials"}
            self._audit_gesture(challenge=resp.get("challenge"), passed=True,
                                identity_frames=frames, distance_best=best, reason="granted")
            return granted

        if cmd == "reset_lockout":
            # Admin / tray / test: clear the face lockout early.
            self._lockout.reset()
            return {"ok": True, "lockout": self._lockout.status()}

        return {"ok": False, "reason": "unknown-command"}

    def _serve_one(self) -> None:
        sa = _build_pipe_sa(self.cfg)
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX
        if self.cfg.pipe_first_instance:
            # Refuse to bind if the name is already taken (a squatter). Safe for our per-connection
            # re-create: the serve loop CloseHandle()s the previous instance in the finally below
            # BEFORE this next CreateNamedPipe, so no instance of OURS exists at this point -- any
            # same-name instance found here is therefore foreign.
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        try:
            handle = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                open_mode,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                65536, 65536, 0, sa,
            )
        except pywintypes.error as e:
            # FIRST_PIPE_INSTANCE -> the name is occupied by someone else. Loud log + clean stop so
            # the serve loop exits (no silent tight crash-loop). The watchdog will retry the start,
            # and each refusal is logged, so a persistent squatter is visible rather than hidden.
            if self.cfg.pipe_first_instance and e.winerror in (
                    winerror.ERROR_ACCESS_DENIED, winerror.ERROR_ALREADY_EXISTS,
                    winerror.ERROR_PIPE_BUSY):
                log.error("pipe name %s occupied (winerror=%d) -- refusing to start "
                          "(possible squatter)", PIPE_NAME, e.winerror)
                self._stop.set()
                try:
                    win32event.SetEvent(self._stop_event)
                except Exception:
                    pass
                return
            raise
        try:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as e:
                # ERROR_PIPE_CONNECTED: a client connected between CreateNamedPipe and Connect -> fine.
                if e.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            # We may have been woken by stop()'s self-connect (Ctrl+C / shutdown) rather than a real
            # request -> just return so the loop re-checks _stop.
            if self._stop.is_set():
                return
            # Stage-5 5c diagnostic: record every accepted connection BEFORE the read, with the
            # client's (non-sensitive) identity from _pipe_client_diag (PID / image / IL / session /
            # openable -- no impersonation, no password/request content). A client that opens the
            # pipe then bails on its OWN pre-write check (e.g. the CP's client-side server-SID
            # verification) shows up here as "connection accepted" with NO following "request cmd=..."
            # line; image=LogonUI.exe + openable=no marks the real lockscreen CP.
            log.info("connection accepted (%s)", _pipe_client_diag(handle))
            try:
                _hr, data = win32file.ReadFile(handle, 65536)
            except pywintypes.error as e:
                # A wake-up connection that closed immediately, or a client that vanished -> no
                # request to handle; return cleanly rather than logging a pipe error.
                if e.winerror in (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_PIPE_NOT_CONNECTED,
                                  winerror.ERROR_NO_DATA):
                    # Stage-5 5c diagnostic: connected but closed before sending a request. Paired
                    # with the "connection accepted" line above and NO "request cmd=..." between
                    # them, this is the signature of a client that bailed after its own pre-write
                    # checks -- i.e. the request never left the client.
                    log.info("connection closed before request (winerror=%d)", e.winerror)
                    return
                raise
            if not data:
                return
            req = json.loads(data.decode("utf-8"))
            log.info("request cmd=%s", req.get("cmd"))
            try:
                resp = self._handle(req, handle)
            except Exception as e:
                log.exception("handler error")
                resp = {"ok": False, "reason": f"exception: {e}"}
            win32file.WriteFile(handle, (json.dumps(resp) + "\n").encode("utf-8"))
            try:
                win32file.FlushFileBuffers(handle)   # blocks until the client reads the buffered data
            except pywintypes.error:
                pass
            # Wait for the client to finish reading and close before we discard the pipe (fixes 233).
            self._drain_until_client_closes(handle)
        finally:
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)

    def _warmup(self) -> None:
        """Load enrollment, preload heavy models so the first real call is fast."""
        import numpy as np
        try:
            self.recog.load()
            log.info("enrollment loaded: %s", self.recog._refs is not None)
        except Exception as e:
            log.warning("enrollment load: %s", e)

        # Force-load the InsightFace models (detection, recognition and the two
        # landmark nets) with a dummy image. There is no separate liveness model
        # to warm any more -- active liveness rides the same landmarks.
        # Using one of our enrolled photos guarantees a face is present.
        try:
            from .config import ENROLL_DIR
            import cv2
            enroll_imgs = list(ENROLL_DIR.glob("*.jpg")) + list(ENROLL_DIR.glob("*.png"))
            if enroll_imgs:
                img = cv2.imread(str(enroll_imgs[0]))
                if img is not None:
                    t0 = time.time()
                    self.recog.verify_frame(img)
                    log.info("model warmup ok in %.2fs", time.time() - t0)
        except Exception as e:
            log.warning("model warmup failed: %s", e)

        # Also warm up the camera (open + close if not persistent). If the device is busy at
        # startup, skip gracefully -- the first real request will retry the bounded open.
        try:
            with self._cam_lock:
                cam, busy = self._acquire_camera()
                if busy:
                    log.info("camera warmup skipped: camera busy (held by another process)")
                else:
                    # close in a finally: a raising read() must not skip it, or the
                    # non-persistent path leaks the handle for the process lifetime.
                    try:
                        cam.read()
                        log.info("camera warmup ok (persistent=%s)", self.cfg.persistent_camera)
                    finally:
                        if not self.cfg.persistent_camera:
                            cam.close()
        except Exception as e:
            log.warning("camera warmup failed: %s", e)

    def serve_forever(self) -> None:
        # Single-instance guard: if another FaceService is already serving
        # this pipe, bail out cleanly instead of competing for connections.
        try:
            mutex = win32event.CreateMutex(None, False, "Local\\FaceUnlockService")
            if win32api_get_last_error() == winerror.ERROR_ALREADY_EXISTS:
                log.warning("another FaceService instance already running; exiting")
                return
        except Exception as e:
            log.warning("mutex check failed, continuing: %s", e)

        # A fresh, legitimate start clears any lingering deliberate-stop pause so the watchdog
        # resumes guarding this instance (Step 5).
        try:
            from .watchdog import clear_pause
            from .config import WATCHDOG_PAUSE_PATH
            clear_pause(WATCHDOG_PAUSE_PATH)
        except Exception as e:
            log.debug("watchdog pause clear skipped: %s", e)

        # Ctrl+C / console-close -> graceful stop, even while the main thread is blocked in
        # ConnectNamedPipe (a plain KeyboardInterrupt cannot interrupt that native wait). No-op
        # under pythonw (no console) -- production graceful stop is via the `shutdown` command.
        try:
            self._ctrl_handler = self._console_ctrl_handler
            win32api.SetConsoleCtrlHandler(self._ctrl_handler, True)
        except Exception as e:
            log.debug("console ctrl handler not installed (no console?): %s", e)

        log.info("FaceService starting; pipe=%s", PIPE_NAME)
        if self.cfg.warmup_on_start:
            self._warmup()

        while not self._stop.is_set():
            try:
                self._serve_one()
            except Exception:
                log.exception("pipe error")
                time.sleep(0.5)

        log.info("FaceService stopped")
        try:
            if self._ctrl_handler is not None:
                win32api.SetConsoleCtrlHandler(self._ctrl_handler, False)
        except Exception:
            pass
        with self._cam_lock:
            self._release_camera()

    def stop(self) -> None:
        """Signal a graceful stop and UNBLOCK a _serve_one() waiting in ConnectNamedPipe.

        Sets the loop flag + the win32 stop event, then SELF-CONNECTS to our own pipe (a throwaway
        client connect+close) so the blocking ConnectNamedPipe returns at once -- otherwise an idle
        server (e.g. on Ctrl+C) would not notice the stop until a real client happened to connect.
        Safe to call from any thread (the console-ctrl handler runs on a Windows-owned thread).
        """
        self._stop.set()
        try:
            win32event.SetEvent(self._stop_event)
        except Exception:   # pragma: no cover - defensive
            pass
        self._wake_accept()

    def _wake_accept(self) -> None:
        """Briefly connect to our own pipe to return a _serve_one() blocked in ConnectNamedPipe."""
        try:
            h = win32file.CreateFile(
                PIPE_NAME, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            win32file.CloseHandle(h)
        except pywintypes.error:
            pass   # no server instance waiting / already torn down -> nothing to wake

    def _console_ctrl_handler(self, ctrl_type) -> bool:
        """Console control handler. Windows runs this on a dedicated thread, so it stops the service
        cleanly even while the main thread is blocked in ConnectNamedPipe (which is exactly why a
        plain Ctrl+C did not work before). Returns True to handle the event and drive the graceful
        stop instead of the default KeyboardInterrupt. No-op under pythonw (no console)."""
        if ctrl_type in (win32con.CTRL_C_EVENT, win32con.CTRL_BREAK_EVENT,
                         win32con.CTRL_CLOSE_EVENT, win32con.CTRL_LOGOFF_EVENT,
                         win32con.CTRL_SHUTDOWN_EVENT):
            log.info("console control event %s -> graceful stop", ctrl_type)
            self.stop()
            return True
        return False

    def _drain_until_client_closes(self, handle, timeout_s: float = 2.0) -> None:
        """After the response is written+flushed, wait (bounded) for the client to finish reading and
        CLOSE its end -- signalled by a read returning ERROR_BROKEN_PIPE -- BEFORE DisconnectNamedPipe.

        DisconnectNamedPipe discards any unread data, handing the client ERROR_PIPE_NOT_CONNECTED
        (233) if it disconnects before the client's ReadFile completes -- the shutdown race. A
        well-behaved client (pipe_client) closes immediately, so the drain read returns at once; a
        misbehaving client is bounded by ``timeout_s`` via a worker thread so it can't wedge the
        serve loop (we Disconnect anyway, which unblocks the worker)."""
        done = threading.Event()

        def _wait():
            try:
                win32file.ReadFile(handle, 1)   # returns when the client writes (it won't) or CLOSES
            except pywintypes.error:
                pass   # ERROR_BROKEN_PIPE = the client closed after reading -> the success signal
            finally:
                done.set()

        threading.Thread(target=_wait, daemon=True).start()
        done.wait(timeout_s)


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def main() -> None:
    _setup_logging()
    cfg = Config.load()
    svc = FaceService(cfg)
    try:
        svc.serve_forever()
    except KeyboardInterrupt:
        svc.stop()


if __name__ == "__main__":
    main()
