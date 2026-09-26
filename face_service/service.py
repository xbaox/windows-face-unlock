"""Named pipe server exposing face verification + credential retrieval.

Protocol: one JSON object per message, both ways. The client sends {"cmd": "...", ...}; the server
answers with ONE JSON object and the client closes. Every reply carries "v" (the protocol version,
2) and "lang" (the UI language the lock-screen tile should use: "en" or "ru"). Transport-level
replies to any request: {"ok":false,"reason":"bad-request"} (not UTF-8 / not JSON / not an object),
{"ok":false,"reason":"unknown-command"}, {"ok":false,"reason":"internal-error"} (a handler fault;
the details go to the log only).

Who may call (Stage 9): the pipe admits the owner (SELF) and SYSTEM only, never a network client.
unlock, unlock_gesture and report_result additionally require a SYSTEM caller (the lock-screen
Credential Provider inside LogonUI) and protocol v2; verify requires SELF. No config key changes
any of this. A service that does not run as the recorded owner, or cannot secure its data
directory, is "refusing": ping says so and every face function answers the refusal reason.

Refusal reasons shared by the face functions: "not-owner" (R1) | "custody" (R9, was
"insecure-data-dir").

  {"cmd":"ping"}
      -> {"ok":true,"pong":true,"state":"serving"}
      -> {"ok":true,"pong":true,"state":"refusing","why":"not-owner|custody"}
  {"cmd":"status"}
      -> {"ok":true,"uptime_s":float,"config":{...},"enrollment":bool,"lockout":{...},
          "audit":{...},"data_dir_secure":bool,"state":str[,"why":str],
          "password_rejected":bool,"protocol":2}
  {"cmd":"reload_config"}
      -> {"ok":true,"config":{...}} | {"ok":false,"reason":"invalid-config: ...|reload-failed: ..."}
  {"cmd":"shutdown"}
      -> {"ok":true,"shutting_down":true}
  {"cmd":"pause_camera","seconds":120}   # release the webcam to the enrollment wizard
      -> {"ok":true,"paused_until":float}  # time.monotonic() deadline: PROCESS-LOCAL
      -> {"ok":false,"reason":"bad-request"}   # not a finite number (clamped to [5, 600] s)
  {"cmd":"resume_camera"}
      -> {"ok":true}
  {"cmd":"build_enrollment"[,"replace":true]}
      -> {"ok":true,"count":int[,"replaced":true][,"pose":{...}]} | {"ok":false,"reason":str}
  {"cmd":"clear_enrollment"}
      -> {"ok":true,"removed":int} | {"ok":false,"reason":"partial","removed":int}
  {"cmd":"verify"}             # SELF only; diagnostic: grants nothing, no strike, no secret
      -> {"ok":true,"match":bool,"distance":float,"real":bool,"verdict":str}
                               # verdict: PASS | NEEDS_GESTURE | NOT_LIVE | SKIPPED
  {"cmd":"presence"}           # single-burst presence probe
      -> {"ok":true,"present":bool,"real":bool,"mode":"recognition|detection",
          "state":"present|uncertain|absent"}
      -> {"ok":false,"reason":"engine-error","present":false,"real":false,"mode":str,"state":"error"}

  {"cmd":"unlock","v":2,"budget_ms":int}          # SYSTEM only. Phase 1 (passive burst).
      -> {"ok":true,"username":str,"password":str,"domain":str,"grant_id":"<32 hex>"}
      -> {"ok":false,"reason":"needs-gesture","gesture":str,"prompt":str,"token":"<32 hex>",
          "ttl_s":float,"distance":float,"real":bool}
      -> {"ok":false,"reason":"locked-out","retry_after_s":float}
      -> {"ok":false,"reason":"no-match"|"too-dark","distance":float,"real":bool}
      -> {"ok":false,"reason":"not-authorized"|"version-mismatch"|"bad-request"|"not-owner"
          |"custody"|"password-rejected"|"camera-busy"|"no-frames"|"no-enrollment"
          |"engine-error"|"deadline-exceeded"|"no-credentials"}
  {"cmd":"unlock_gesture","v":2,"token":"<32 hex>","budget_ms":int}   # SYSTEM only. Phase 2.
      -> {"ok":true,"username":str,"password":str,"domain":str,"grant_id":"<32 hex>"}
      -> {"ok":false,"reason":"gesture-failed","challenge":str,"state":str,"identity_frames":int}
      -> {"ok":false,"reason":"gesture-token-invalid"|"locked-out"(+retry_after_s)|...as unlock}
  {"cmd":"report_result","v":2,"grant_id":"<32 hex>","ok":bool}   # SYSTEM only.
      -> {"ok":true} | {"ok":false,"reason":"grant-unknown"}
         ok=true: the grant is committed (lockout reset, audit "granted", gallery adaptation).
         ok=false: Windows rejected the stored password -> a persistent flag; unlock answers
         "password-rejected" until a new password is saved. No report within 30 s: the grant is
         abandoned and nothing is committed.

Deadlines: the service counts from the moment it READ the request and fails closed after
min(11 s / 14 s, budget_ms - 500 ms) for unlock / unlock_gesture.
(reset_lockout was removed in Stage 8b, F-21; the dev-only challenge command in Stage 9, F-67.)
"""
from __future__ import annotations
import json
import logging
import math
import os
import secrets
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import NamedTuple

import numpy as np

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

from .camera import Camera, CameraReadTimeout
from .config import Config, APP_DIR, LOG_PATH, LOCKOUT_PATH, AUDIT_PATH, PIPE_NAME
from .credentials import load_password, mark_password_rejected, password_rejected
from .identity import SYSTEM_SID, current_user_sid, owner_check
from .datadir import (DUMP_NAME_RE, heal_data_dir, is_reparse, purge_debug_frames,
                      remove_tree_no_follow)
from .detector import FaceDetector
from .audit import AuditLog
from .liveness import BlinkDetector, SCREEN_DOUBT_FRAC, Verdict, verdict
from . import imio
from .lockout import Lockout
from .lowlight import evaluate_low_light, scene_luma
from .camera_boost import try_exposure_boost
from .camera_open import BoundedOpener
from .recognizer import Recognizer

log = logging.getLogger(__name__)

# Pause between camera-open attempts (Stage 3.4). Everything else about the open loop is config
# (camera_open_retries / camera_open_timeout_s bound how many attempts run, and 7b-2's
# camera_open_attempt_cap_s bounds how long each is waited for); this small inter-attempt pause is
# fixed -- it exists to let a driver settle between tries, not to shape the budget.
CAMERA_OPEN_PAUSE_S = 0.3


# Lifetime of a phase-1 gesture token (Stage 7-i). PROTOCOL knob, not a liveness threshold: it
# bounds how long the lockscreen has to come back with `unlock_gesture` after being told which
# gesture to perform. Long enough for the user to read the prompt and react, short enough that a
# token left behind on an abandoned lockscreen is dead within seconds. Checked at REQUEST time
# only -- the round itself is then bounded by its own wall-clock cap in _run_challenge, so a
# gesture that starts in time is never cut short by this.
GESTURE_TOKEN_TTL_S = 15.0

# Stage 8b (F-19, act A-2): server-side deadlines for releasing credentials, counted from the
# moment the request was READ. PROTOCOL constants, like the TTL above -- not liveness or lockout
# numbers. Defect: a slow burst (camera heal + retry, low-light boost) or a long gesture round could
# finish after the Credential Provider had already given up (12 s phase 1, 15 s phase 2), and the
# password was released -- and the grant audited, and the lockout reset -- into a pipe nobody was
# reading any more. Fix: past these deadlines the service fails closed WITHOUT releasing anything;
# each leaves ~1 s for the reply to reach the CP inside its own budget.
UNLOCK_DEADLINE_S = 11.0
# Stage 9 (act 9b R4): the phase-2 round is two movements now -- 0.4 s still start + 2 x 5 s +
# 2 s = 12.4 s from its first frame (liveness.ROUND_CAP_S). The phase-2 budgets grew with it:
# the CP waits 18 s (kGestureTimeoutMs) and the service stops at 17 s, keeping the ~1 s for the
# reply that F-19 established.
UNLOCK_GESTURE_DEADLINE_S = 17.0

# Stage 8b (F-16 / act A-5): bounds of a pause_camera lease. Not a Config field.
PAUSE_CAMERA_MIN_S = 5.0
PAUSE_CAMERA_MAX_S = 600.0

# Stage 8b (F-42): how much of an untrusted "cmd" value may reach the log.
LOG_CMD_MAX = 64

# Well-known SID for the lockscreen Credential Provider: LogonUI loads the CP DLL as SYSTEM.
SYSTEM_SID_STRING = SYSTEM_SID

# How many files the debug_frames ring keeps (7h). Files, not frames: each dump writes a .npy and
# a .png, so this is ~20 frames. Deliberately NOT a config knob -- cfg.debug_dump_frames already
# decides whether anything is written at all, and the only job left for the bound is to keep a
# knob somebody forgot to switch off from filling the disk with face imagery.
DEBUG_DUMP_RING_MAX = 40

# CreateNamedPipe openMode flag (anti-squatting, Stage 4 Step 4): CreateNamedPipe fails if an
# instance of the name already exists. pywin32 312 does not export it, so define the literal.
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
# Stage 9 (R2, F-55): pipe-mode flag -- the pipe refuses clients that connect over the network (an
# SMB client with the user's password or hash). Not exported by pywin32 either.
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
# Stage 9 (R2, F-54 / F-56): two instances at most -- the one serving a client and the next one,
# created BEFORE the served one is closed, so the name is never free between two connections. The
# old PIPE_UNLIMITED_INSTANCES let any SELF process add parallel instances at will.
PIPE_MAX_INSTANCES = 2

# Stage 9 (§2.1): the service <-> Credential Provider contract version. unlock, unlock_gesture and
# report_result must carry "v": 2; every reply carries "v" and "lang". A mismatch is refused with
# "version-mismatch" and the tile asks the user to update Face Unlock -- nothing is packed.
PROTOCOL_VERSION = 2
# The CP sends its remaining budget as "budget_ms"; the service keeps this much of it for the reply
# to travel back, i.e. it uses min(own deadline, budget_ms - 500 ms).
CLIENT_BUDGET_RESERVE_S = 0.5
CLIENT_BUDGET_MAX_MS = 120_000
# A delivered grant waits this long for the CP's report_result. Without it the grant counts as
# abandoned: no lockout reset, no gallery adaptation (F-60 / F-93).
GRANT_REPORT_TTL_S = 30.0

# Commands polled on a timer (watchdog, monitor, tray); logged at DEBUG so the log is not a ping
# counter (D-45).
ROUTINE_COMMANDS = ("ping", "status")


def _pipe_client_sid_string(handle) -> "str | None":
    """String SID of the process on the CLIENT end of a connected pipe handle, or None on any
    failure (client already gone, cannot impersonate, ...).

    Uses ImpersonateNamedPipeClient: the pipe subsystem hands the server the client's token
    directly, so the caller's SID is read WITHOUT OpenProcess. That matters because the service runs
    as a Limited user, and OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) on a SYSTEM client (the
    lockscreen CP inside LogonUI) is denied -- the old GetNamedPipeClientProcessId->OpenProcess chain
    therefore resolved a genuine SYSTEM caller to None and wrongly failed the SID-gate. Reading the
    SID needs no privilege: SecurityIdentification suffices, and that is all the CP offers -- it opens
    the pipe with SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION (8b F-26; D-49). The unlock handler has
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


def _build_pipe_sa() -> win32security.SECURITY_ATTRIBUTES:
    """Security attributes for the named pipe -- always hardened (Stage 9, R2: the legacy NULL-DACL
    rollback and its pipe_hardened_sd toggle are gone, F-68).

      * Owner: SELF, explicitly, so a client can check the pipe OBJECT's owner (F-64) and get the
              same answer whether or not the service token is elevated.
      * DACL: NETWORK denied first (F-55) -- a network logon of the owner's own account matches
              SELF, so without this ACE an SMB client holding the password could drive the pipe.
              SELF = GENERIC_ALL (the server itself: re-creating the instance needs
              FILE_CREATE_PIPE_INSTANCE). SYSTEM = GENERIC_READ|GENERIC_WRITE -- the lock-screen
              CP inside LogonUI. No Everyone ACE: every other account hits the implicit deny.
      * SACL: a Medium mandatory label with NoReadUp+NoWriteUp -> a same-user LOW-integrity process
              cannot read or write the pipe.
    Built once per process (D-45): nothing in it can change while the process runs."""
    self_sid = current_user_sid()
    sddl = (
        f"O:{self_sid}"
        f"D:(D;;GA;;;NU)(A;;GA;;;{self_sid})(A;;GRGW;;;{SYSTEM_SID_STRING})"
        "S:(ML;;NRNW;;;ME)"
    )
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1)
    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


def _scrub(text: str) -> str:
    """F-113: strip profile paths (they carry the account name) from text that leaves over the
    pipe. The data directory becomes "<data>", the profile "~"."""
    text = str(text)
    for path, token in ((str(APP_DIR), "<data>"), (os.path.expanduser("~"), "~")):
        if path and path != "~":
            text = text.replace(path, token)
    return text


# ---- presence-probe frame classes and verdict (7c-6). PROBE-ONLY: unlock never reaches here. ----
#
# verify_frame's shapes are distinguishable from its return tuple ALONE (recognizer.py:369-383):
# no face -> (False, 1.0, False); anti-screen suppression -> (False, <1.0, False); a real face that
# simply did not match -> (False, dist, True); a match -> (True, dist, True). An ``ok`` of None
# marks a frame whose analyze_frame raised.
#
# Two live incidents killed the old bare yes/no. At 19:49:46 three frames sat at d=0.286-0.306 with
# real=False -- recognised, but anti-screen suppressed the match at close range. At 19:50:17 three
# frames sat at d=0.323-0.343 with real=True -- a working pose putting the tail just past 0.32. Both
# read as "absent" and locked a user who was sitting right there. The classes below add a middle
# ground instead of moving any recognition number: SUSPECT and WEAK are probe-only concepts.
#
# Module-level on purpose: pure functions with no use for self, so a caller that borrows a probe
# body (tools/presence_guards_selftest.py does exactly that) does not have to mirror them.
def _probe_frame_class(ok, dist, real, threshold: float, soft_margin: float) -> str:
    """One frame -> strong / weak / suspect / none / error. ORDER IS LOAD-BEARING.

    ``error`` first: dist is None there and every later comparison would raise. ``strong`` next, so
    a real match can never be reclassified by the softer rules. ``none`` on dist >= 1.0 before the
    band checks, because that is verify_frame's "no face at all" sentinel and not a real distance --
    without this an empty frame would fall into the suspect branch on a wide margin.
    """
    if ok is None:
        return "error"
    if ok:
        return "strong"
    if dist >= 1.0:
        return "none"
    ceiling = float(threshold) + float(soft_margin)
    if real and threshold < dist <= ceiling:
        return "weak"
    if (not real) and dist <= ceiling:
        return "suspect"
    return "none"


def _probe_verdict(frames, threshold: float, soft_margin: float) -> str:
    """The burst -> present / uncertain / absent.

    A single strong OR weak frame is enough to say the user is here: absence has to be the absence
    of ANY sighting. Failing that, a single suspect frame downgrades to ``uncertain`` rather than
    ``absent`` -- something face-shaped and close was there, and that must not lock on its own. An
    all-error burst falls through to ``absent``, which is what it did before 7c-6.
    """
    classes = [_probe_frame_class(ok, d, r, threshold, soft_margin) for ok, d, r in frames]
    if any(c in ("strong", "weak") for c in classes):
        return "present"
    if any(c == "suspect" for c in classes):
        return "uncertain"
    return "absent"


def _fmt_probe_frames(frames, threshold: float, soft_margin: float) -> str:
    return "[" + ", ".join(
        "(%s d=%s real=%s %s)" % (
            "-" if ok is None else ("T" if ok else "F"),
            "n/a" if dist is None else "%.3f" % dist,
            "-" if real is None else ("T" if real else "F"),
            _probe_frame_class(ok, dist, real, threshold, soft_margin),
        )
        for ok, dist, real in frames
    ) + "]"


def _probe_hint(frames, threshold: float, soft_margin: float) -> str:
    """Roll the per-frame classes up into one "why" -- the counts ARE the explanation."""
    if not frames:
        return "no-frames-analysed"
    counts: dict = {}
    for ok, dist, real in frames:
        cls = _probe_frame_class(ok, dist, real, threshold, soft_margin)
        counts[cls] = counts.get(cls, 0) + 1
    return " ".join("%s=%d" % (k, counts[k]) for k in sorted(counts))


def _fmt_luma(luma_max) -> str:
    return "n/a" if luma_max is None else "%.2f" % luma_max


# Stage 9 (act 9b R10, F-139): the camera is opened on demand. When the session locks, the service
# opens it and keeps it warm for the unlock that is about to come -- until the unlock, and never
# longer than this. The lock state is polled every CAMERA_WARM_POLL_S by a small service thread.
CAMERA_WARM_HOLD_S = 60.0
CAMERA_WARM_POLL_S = 1.0
# Stage 9 (F-77): the Credential Provider's field caps (PipeClient.h kMax*Chars), in UTF-16 units.
CP_MAX_USERNAME = 256
CP_MAX_DOMAIN = 256
CP_MAX_PASSWORD = 1024
# F-138: the heal retry and the low-light re-capture each cost one more burst. They are started only
# when the request still has the last burst's duration plus this margin left in its budget.
EXTRA_BURST_MARGIN_S = 1.0


def _camera_reason(detail: dict) -> str:
    """The wire reason for a burst that never saw the camera: a read that hung or a named camera
    that is not present is "camera-error" (R10); a busy or leased device stays "camera-busy"."""
    return "camera-error" if detail.get("reason") in ("camera-error", "camera-not-found") \
        else "camera-busy"


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
    # Stage 9 (R10) camera state, also as class-level defaults so a harness that builds the service
    # with __new__ sees the same "nothing happened yet" values __init__ sets.
    _cam = None
    _warm_until = 0.0
    _boost_disabled = False
    _camera_problem: "str | None" = None
    _probe_why: "str | None" = None
    _session_locked = None

    def __init__(self, cfg: Config, custody=None):
        self.cfg = cfg
        # Stage 8b (F-01 / P-03, act A-2): the data-directory custody verdict. main() heals the
        # directory BEFORE the config is read and hands the report in; any other constructor gets
        # a heal of its own here, so no FaceService ever serves without one. Fixed for the process
        # lifetime: a failed heal refuses unlock / unlock_gesture with "insecure-data-dir" after
        # the existing gates, and nothing at runtime can flip it back.
        if custody is None:
            custody = heal_data_dir(APP_DIR)
        self._data_dir_insecure = not custody.ok
        self._custody_problem = (_scrub(custody.problems[0]) if custody.problems else None)
        # Stage 9 (R1): the product is single-user. A service that does not run as the recorded
        # owner keeps the pipe (the watchdog sees it alive) but refuses every face function; ping
        # answers {"state":"refusing","why":"not-owner"}. Fixed for the process lifetime.
        why = owner_check()
        self._not_owner = why is not None
        if self._not_owner:
            log.error("owner check failed: %s -- face functions refused (not-owner)", why)
        self.recog = Recognizer(cfg)
        self.detector = FaceDetector()
        # Stage 9 (act 9b R7): the model pack is checked ONCE, at start -- exactly the five pinned
        # files with their pinned SHA-256 (~1-2 s for 340 MB). Anything else and every face
        # function answers "no-models"; ping says {"state":"refusing","why":"no-models"}.
        self._models_problem = None
        if not self._not_owner and custody.ok:
            from .recognizer import model_problems
            problems = model_problems(hashes=True)
            if problems:
                self._models_problem = "; ".join(problems[:3])
                log.error("model pack not usable: %s -- face functions refused (no-models). "
                          "Run the Face Unlock installer again to download it.",
                          self._models_problem)
        # Stage 9 (R6): the per-camera turn-sign calibration (calibration.json).
        self._calibration = self._load_calibration() if custody.ok else {}
        # Persistent consecutive-failure lockout for the face path (PIN stays available).
        self._lockout = Lockout(LOCKOUT_PATH, cfg.max_face_attempts, cfg.lockout_seconds)
        # Structured JSONL audit trail (verify/unlock/challenge; never stores the password).
        self._audit = AuditLog(AUDIT_PATH, cfg.audit_max_mb, enabled=cfg.audit_log)
        # The serve loop is driven by _stop; ConnectNamedPipe is unblocked by the self-connect in
        # stop(). (Stage 9, D-44: the parallel win32 _stop_event nobody ever waited on is gone.)
        self._stop = threading.Event()
        self._ctrl_handler = None   # keep a ref so SetConsoleCtrlHandler's callback isn't GC'd
        self._cam_lock = threading.Lock()
        self._cam: Camera | None = None  # kept open when persistent_camera=True
        # monotonic timestamp of the last persistent-camera self-heal (_note_camera_health).
        # 0.0 means "never healed": time.monotonic() on Windows counts from boot, so the very
        # first heal is never suppressed unless the service starts within the cooldown of boot.
        self._cam_heal_at = 0.0
        # Runs each camera-open attempt on its own thread with a hard wait ceiling, and reclaims a
        # capture that lands after we stopped waiting. One instance for the process: it keeps the
        # in-flight attempt as state, and the pipe server is sequential so there is never a second
        # opener running against the same device.
        self._opener = BoundedOpener()
        self._started_at = time.time()
        # When the enrollment wizard is running it needs exclusive access to
        # the webcam. ``_camera_paused_until`` holds a MONOTONIC deadline: probes
        # and verify calls short-circuit until that time passes. The service
        # also releases the persistent camera so the tray can open it.
        # Monotonic, not wall clock (7d-C, KNOWN_ISSUES #3): a forward clock step ended the lease
        # early and handed the device back while the wizard was still shooting, and a backward one
        # held it past the requested window -- during which the presence probe reports "present"
        # and absence strikes stop accruing, so the wrong clock cost more than the camera. The
        # wizard's half of this handshake was already monotonic and said so. As with
        # ``_cam_heal_at`` above, 0.0 keeps working as "no lease" because monotonic counts from
        # boot and is therefore always positive.
        self._camera_paused_until: float = 0.0
        # (Stage 9, R2 / F-104: the 7e-2 posture ratchet is gone together with the three pipe keys
        # it guarded -- the perimeter has no knobs left to weaken.)
        # Stage 7-i gesture round: the single outstanding phase-1 token, or None. ONE slot is
        # enough because the pipe server is strictly sequential (_serve_one handles one request
        # at a time), so two unlocks can never be in flight together; a newer unlock simply
        # replaces the older token. Shape: {"token": str, "kind": str, "expires": monotonic}.
        self._gesture_slot: dict | None = None
        # Stage 9 (§2.1): the delivered grant waiting for the CP's report_result, or None.
        # Shape: {"grant_id": str, "expires": monotonic, "commit": fn(), "reject": fn(),
        # "abandon": fn()}. One slot: the server is sequential and a newer grant replaces it.
        self._report_slot: dict | None = None
        self._pipe_sa = None            # built once, on the first bind (D-45)
        # Stage 9 (R10): the warm hold -- a monotonic deadline until which the camera is kept open
        # although persistent_camera is off (session locked; see _lock_watch). 0.0 = no hold.
        self._warm_until = 0.0
        # R10 (F-129): the device did not come back to its exposure settings after a boost; the
        # boost stays off for it until the service restarts.
        self._boost_disabled = False
        # R10: why the camera could not be used at the last attempt ("camera-not-found" |
        # "camera-busy" | "camera-error"), or None. Shown in status; cleared by a good open.
        self._camera_problem: "str | None" = None
        self._probe_why: "str | None" = None
        self._session_locked = None     # the lock-state probe; set in serve_forever (tests inject)

    def _camera_leased_out(self) -> bool:
        return time.monotonic() < self._camera_paused_until

    def _keep_open(self) -> bool:
        """Whether a capture outlives the request that opened it: persistent_camera, or the warm
        hold of a locked session (R10)."""
        return bool(self.cfg.persistent_camera) or time.monotonic() < self._warm_until

    def _done_with(self, cam) -> None:
        """End of one request's use of ``cam`` (caller holds ``_cam_lock``): kept when the camera
        is to stay open, closed otherwise -- and never left cached once closed."""
        if self._keep_open() and self._cam is cam:
            return
        if self._cam is cam:
            self._cam = None
        try:
            cam.close()
        except Exception:
            log.exception("camera close failed")

    def _acquire_camera(self):
        """Open the webcam with a bounded-WAIT retry (Stage 3.4; wait ceiling added in 7b-2).

        Returns ``(camera, busy)``: ``(Camera, False)`` on success, ``(None, True)`` when the device
        is busy (held by ANOTHER process -- distinct from our own enrollment lease, which the callers
        check first). Never raises. For the persistent camera ``self._cam`` is set ONLY on success,
        so a failed open leaves it None and the next call retries cleanly instead of returning a
        stuck half-open handle. The caller must already hold ``self._cam_lock``.

        What "bounded" means here, precisely: ``camera_open_retries``/``camera_open_timeout_s``
        bound how MANY attempts are made, and ``camera_open_attempt_cap_s`` bounds how long we WAIT
        for each one (``BoundedOpener`` runs it on a thread and joins with that ceiling). It does
        not mean the driver call is cancelled -- nothing can cancel it. A wedged attempt is left
        running, the retry loop aborts rather than queueing more waiting on the same stuck device,
        and if that attempt eventually produces a capture, its own thread closes it. So this returns
        in bounded time; the DEVICE may still be occupied by the abandoned call for longer, which
        the caller sees as the usual "camera-busy".
        """
        if self._cam is not None:
            if getattr(self._cam, "_cap", True) is not None:
                return self._cam, False
            self._cam = None        # abandoned by a read that hung (R10): never hand it out again
        cam = Camera(self.cfg.camera_index, self.cfg.camera_warmup_frames,
                     name=str(getattr(self.cfg, "camera_name", "") or ""),
                     read_cap_s=float(self.cfg.camera_open_attempt_cap_s))
        ok = self._opener.open(
            open_fn=cam.open_fast,
            close_fn=cam.close,
            retries=self.cfg.camera_open_retries,
            pause_s=CAMERA_OPEN_PAUSE_S,
            timeout_s=self.cfg.camera_open_timeout_s,
            cap_s=self.cfg.camera_open_attempt_cap_s,
        )
        if not ok:
            # Stage 9 (R10): a named camera that is not connected is its own, clear refusal --
            # there is no fallback to another device. The truthy string keeps `if busy:` working.
            why = "camera-not-found" if getattr(cam, "not_found", False) else "camera-busy"
            self._camera_problem = why
            return None, why
        self._camera_problem = None
        if self._keep_open():
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

    def _burst_defect(self, frames_ok: int, luma_max: "float | None") -> "str | None":
        """The ONE definition of a defective capture burst: ``"zero-frames"``, ``"black-burst"``,
        or None when the burst looked fine.

        This is the only place ``cfg.camera_black_luma`` is read. Two callers need the same verdict
        and must never disagree about what "black" means: ``_note_camera_health`` drops the poisoned
        persistent cache afterwards, and the presence probes refuse to spend an ABSENCE STRIKE on a
        camera that handed back nothing -- a blind camera is a camera fault, not a missing user.
        Kept as a pure classifier (no side effects, no lock) so both can call it freely.
        """
        if frames_ok == 0:
            return "zero-frames"
        if luma_max is not None and luma_max <= self.cfg.camera_black_luma:
            return "black-burst"
        return None

    def _note_camera_health(self, frames_ok: int, luma_max: "float | None", where: str) -> bool:
        """Drop a poisoned persistent-camera cache AFTER the fact. Returns True if it was dropped.

        The KNOWN_ISSUES #1 signature: an owner wedged elsewhere on the machine leaves the device
        in a state where our capture opens "successfully" and then reads nothing, or reads
        all-black frames -- and with ``persistent_camera=True`` ``_acquire_camera`` caches that
        capture and reuses it for the process lifetime, since its reuse gate only checks that a
        handle exists. Nothing here probes the device to find that out BEFORE using it: a probe
        read can wedge exactly like the real ones do, and the pipe server is sequential, so a
        stuck probe would cost every later request. This judges the reads that already happened.

        Trigger: no frame arrived at all, or the brightest frame of the burst was at/below
        ``cfg.camera_black_luma``. ``cfg.camera_reopen_cooldown_s`` then rate-limits the heal, so a
        device that is simply gone is not reopened once per request.

        Dropping the cache is all this does -- reopening is left to the next ``_acquire_camera``,
        which builds a BRAND-NEW ``Camera``. That matters: ``Camera.open_fast`` short-circuits when
        the object already holds a handle, and a fresh object has none, so the dead capture cannot
        be handed back. Recovery is best-effort, not a promise: if the wedged owner still holds the
        device the fresh open just reports busy, and the caller sees the usual "camera-busy".

        Call with ``_cam_lock`` NOT held -- it takes that lock itself, and ``threading.Lock`` is
        not reentrant. Reading ``self._cam`` unlocked is safe here because the pipe server handles
        one request at a time, on this thread.
        """
        if self._cam is None:
            return False                      # nothing cached, so nothing to poison
        reason = self._burst_defect(frames_ok, luma_max)
        if reason is None:
            return False
        if where.startswith("probe") and reason == "black-burst":
            # Stage 9 (F-145): a black but streaming capture on the presence path is a covered
            # lens or a dark room far more often than a wedge -- reopening it every tick only
            # toggled the LED and cost a cold open (2 730 heals, 0 suppressed). The probe reports
            # "unknown" instead; the verify path keeps its own heal-and-retry.
            return False
        now = time.monotonic()
        since = now - self._cam_heal_at
        if since < self.cfg.camera_reopen_cooldown_s:
            log.info("camera heal suppressed (cooldown): where=%s reason=%s %.1fs since last heal "
                     "(cooldown %.1fs)", where, reason, since, self.cfg.camera_reopen_cooldown_s)
            return False
        with self._cam_lock:
            self._release_camera()
        self._cam_heal_at = now
        log.warning("camera cache dropped for self-heal: where=%s reason=%s frames_ok=%d luma=%s",
                    where, reason, frames_ok,
                    "n/a" if luma_max is None else "%.2f" % luma_max)
        return True

    def _maybe_dump_frame(self, frame, tag: str) -> None:
        """Write ONE captured frame to disk when ``cfg.debug_dump_frames`` is on. Diagnostics only.

        The gate is the first line, so a machine that never turns the knob on pays one attribute
        read per frame and nothing else. Everything after it is wrapped, because this is an
        OBSERVER: it is handed the frame the caller is ABOUT to analyse, it never sees a result,
        and a debug file that cannot be written must not be able to change an authentication
        answer. Any failure is logged at WARNING and swallowed for exactly that reason.

        Why it exists (KNOWN_ISSUES #5): every other signal on the verify path is DERIVED from
        this frame -- distance, det score, scene luma -- so when they all say "nothing", they
        cannot say whether the camera handed over an empty room or nothing at all. The frame
        itself is the only artefact that can.

        Both forms are written. The ``.npy`` is the array exactly as the camera produced it and
        goes first, because it is the one that cannot lie about dtype, range or channel order.
        The ``.png`` beside it is for a human to open, and PNG encoding is the part that may
        legitimately refuse (an odd dtype or shape), so the INFO line carrying the frame's
        statistics is emitted either way: a frame whose PNG failed is still evidence, and the
        stats alone already separate a black capture from a lit scene with nobody in it.

        The directory is a ring pruned to ``DEBUG_DUMP_RING_MAX`` files after every write, so a
        knob left on overnight costs a bounded amount of disk. Its contents are BIOMETRIC -- raw
        imagery of a face -- and tools/uninstall.ps1 classifies the whole directory as such.
        """
        if not self.cfg.debug_dump_frames:
            return
        try:
            d = APP_DIR / "debug_frames"
            # Stage 8b (F-02): never write or prune THROUGH a reparse point -- a linked directory
            # points outside the tree the heal secured, and the prune below deletes files.
            if is_reparse(d):
                log.warning("frame dump skipped: %s is a reparse point", d)
                return
            d.mkdir(parents=True, exist_ok=True)
            now = time.time()
            base = "%s-%03d_%s" % (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
                                   int((now % 1.0) * 1000), tag)
            npy, png = d / (base + ".npy"), d / (base + ".png")
            arr = np.asarray(frame)
            np.save(str(npy), arr)
            png_note = str(png)
            try:
                if not imio.imwrite(png, frame):      # Stage 9 (R8): Unicode-safe
                    png_note = "%s (imwrite returned False)" % png
            except Exception as e:
                png_note = "%s (imwrite failed: %r)" % (png, e)
            log.info("frame dump tag=%s shape=%s dtype=%s min=%s max=%s mean=%.2f npy=%s png=%s",
                     tag, arr.shape, arr.dtype, arr.min(), arr.max(), float(arr.mean()),
                     npy, png_note)
            # Stage 8b (F-02 / F-12). Defect: the ring pruned EVERY file in the directory, by
            # mtime, following links. Consequence: anything placed there -- or reached through a
            # link -- could be deleted by it. Fix: only names of the dump pattern, never a reparse
            # point; the ring bound itself is unchanged.
            dumps = [p for p in d.iterdir()
                     if DUMP_NAME_RE.match(p.name) and not is_reparse(p) and p.is_file()]
            stale = sorted(dumps, key=lambda p: p.stat().st_mtime)[:-DEBUG_DUMP_RING_MAX]
            for old in stale:
                old.unlink(missing_ok=True)
        except Exception as e:
            log.warning("frame dump failed: %r", e)

    # ---------- core ops ----------

    def _capture_and_verify(self) -> VerifyOutcome:
        """Capture a short burst, fold recognition + passive liveness into a verdict.

        One detect per frame via ``analyze_frame`` -> match/distance + 2d106 landmarks (blink)
        + an anti-screen vote. After the burst ``liveness.verdict`` combines them (mode-aware).
        Returns a ``VerifyOutcome``: the legacy ``(match, distance, real)`` plus a ``detail`` dict
        for the audit log. Only a ``PASS`` verdict yields ``match=True``. ``NEEDS_GESTURE`` is
        answered by unlock with "needs-gesture" and a token: the lock screen then runs phase 2
        (``unlock_gesture``, the two-movement GestureSequence) -- D-50: the old text still said
        the lock screen was passive. ``real`` = the passive anti-screen did NOT suspect a screen.
        Runs the full burst (no early-exit) so every frame gets a chance to flag a screen and to
        catch a spontaneous blink; that burst is still subsecond.

        Camera self-heal (KNOWN_ISSUES #1): when the burst reads nothing, or reads black, the
        cached persistent capture is dropped (``_note_camera_health``) and the WHOLE burst is
        retried ONCE against a freshly opened device. The retry is transparent -- callers see a
        single VerifyOutcome, so `verify`/`unlock` still write one audit record and touch the
        lockout counter at most once, on the final result. Exactly one retry, no recursion: if the
        second burst is dead too, that is the answer, and the cooldown keeps the next request from
        reopening again immediately.
        """
        if self._camera_leased_out():
            log.info("verify skipped: camera leased out to enrollment")
            # Stage 8b (F-46, act A-4): camera_busy=True, so unlock answers "camera-busy" through
            # its existing camera gate instead of "no-match" -- the device IS busy, with the
            # wizard. Lockout-neutral either way; verify still reports verdict SKIPPED.
            return VerifyOutcome(False, 1.0, False,
                                 {"verdict": "SKIPPED", "reason": "camera-leased"},
                                 camera_busy=True)

        r = self._locked_burst()
        if r.camera_busy:
            return r                          # device held elsewhere: nothing of ours to heal
        if not self._note_camera_health(r.detail.get("frames_ok", 0), r.scene_luma, "verify-burst"):
            return r
        if not self._room_for_burst(r, UNLOCK_DEADLINE_S):
            log.info("camera heal retry skipped: not enough of the request budget left (F-138)")
            return r
        r2 = self._locked_burst()
        # The heal dropped the cache, so the acquire above built a new Camera. If even that could
        # not open, the device is genuinely unavailable -- report the FIRST outcome rather than
        # turning a plain bad burst into "camera-busy" on the way out.
        if r2.camera_busy:
            return r
        return r2._replace(detail={**r2.detail, "healed": True})

    def _locked_burst(self) -> VerifyOutcome:
        """One acquire -> burst -> release cycle under ``_cam_lock``.

        Split out of ``_capture_and_verify`` so the self-heal can run the same cycle a second time
        without recursion, and so the health check itself runs with the lock released (it takes
        ``_cam_lock``, which is not reentrant). The busy path is unchanged from before the split.
        """
        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("verify skipped: %s", busy)
                return VerifyOutcome(False, 1.0, False,
                                     {"verdict": "SKIPPED", "reason": busy},
                                     camera_busy=True)
            try:
                return self._analyze_burst(cam)
            except CameraReadTimeout:
                # Stage 9 (R10, F-144): the read hung; the capture is already abandoned.
                self._camera_problem = "camera-error"
                return VerifyOutcome(False, 1.0, False,
                                     {"verdict": "SKIPPED", "reason": "camera-error"},
                                     camera_busy=True)
            finally:
                self._done_with(cam)

    def _room_for_burst(self, r: "VerifyOutcome", budget_s: float) -> bool:
        """F-138: whether one more burst like ``r`` still fits in this request's budget (the
        server deadline, or the client's own when smaller). No request stamp = no deadline."""
        t0 = getattr(self, "_req_started", None)
        if t0 is None:
            return True
        client = getattr(self, "_client_budget_s", None)
        limit = budget_s if client is None else min(budget_s, client)
        need = float(r.detail.get("latency_ms") or 0.0) / 1000.0 + EXTRA_BURST_MARGIN_S
        return time.monotonic() - t0 + need <= limit

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
        frames_ok = 0           # frames that actually arrived (camera-health telemetry, 7b)
        engine_errors = 0       # frames the engine could not judge (Stage 8b, F-18)
        faces = 0               # frames with a face at all (Stage 9, R5: "no-face" is no attempt)
        feats: list = []        # anti-screen features of the face frames (R6 telemetry)
        blink = BlinkDetector()

        # drain stale buffered frames
        for _ in range(2):
            cam.read()
        for i in range(self.cfg.verify_frames):
            frame = cam.read()
            if frame is None:
                continue
            frames_ok += 1
            # Scene brightness is face-INDEPENDENT: compute it for every captured frame
            # (incl. no-face ones) and keep the MAX, so a transient dip or a frame where
            # the face was briefly lost can't trip the low-light gate on its own.
            # Stage 8b (F-44): inside a try -- a frame scene_luma cannot read (odd shape or
            # dtype) used to raise out of the whole burst; now it only loses its luma sample.
            try:
                sl = scene_luma(frame)
                scene_luma_max = sl if scene_luma_max is None else max(scene_luma_max, sl)
            except Exception as e:
                log.warning("scene luma failed on a verify frame: %r", e)
            # Sideways and BEFORE the engine (7h): the dump sees the same bytes analyze_frame is
            # about to see, and returns nothing this burst reads. Off by default; see
            # _maybe_dump_frame.
            self._maybe_dump_frame(frame, f"verify{i}")
            try:
                a = self.recog.analyze_frame(frame)
            except Exception as e:
                log.warning("verify error: %s", e)
                engine_errors += 1
                continue
            if not a.face:
                continue
            faces += 1
            if getattr(a, "screen_features", None) is not None:
                feats.append(a.screen_features)
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
            # Additive camera-health telemetry (7b): how many of cfg.verify_frames reads actually
            # returned a frame. 0 with a cached persistent camera is the KNOWN_ISSUES #1 signature.
            "frames_ok": frames_ok,
            # Stage 8b (F-18), additive: frames the engine could not judge at all.
            "engine_errors": engine_errors,
            "faces": faces,
            "mode": self.cfg.liveness_mode,
            "latency_ms": round(latency_ms, 1),
            # Stage 9 (act 9b R6): per-attempt telemetry -- the anti-screen features (medians over
            # the face frames) and the burst's frame rate. Numbers only.
            "hf": round(float(np.median([f.hf for f in feats])), 4) if feats else None,
            "lap": round(float(np.median([f.lap for f in feats])), 1) if feats else None,
            "peak": round(float(np.median([f.peak for f in feats])), 2) if feats else None,
            "fps": round(frames_ok / (latency_ms / 1000.0), 1) if latency_ms > 0 else None,
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
        if self._boost_disabled:
            # F-129: this device did not return to its exposure settings once; no second try.
            return r_dark, {**boost_audit, "boost_disabled": True}
        if not self._room_for_burst(r_dark, UNLOCK_DEADLINE_S):
            log.info("low-light boost skipped: not enough of the request budget left (F-138)")
            return r_dark, {**boost_audit, "boost_skipped": "budget"}
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
                    if not out.restored:
                        # F-129: EXPOSURE / AUTO_EXPOSURE did not read back as before. Drop the
                        # capture (the next open starts from the driver's defaults) and keep the
                        # boost off for this device until the service restarts.
                        self._boost_disabled = True
                        if self._cam is cam:
                            self._cam = None
                        cam.close()
                        log.error("low-light boost: the camera did not return to its exposure "
                                  "settings -- capture dropped, boost off until restart")
                finally:
                    self._done_with(cam)
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
        """Opt-in adaptive gallery after a phase-1 PASS (see _maybe_adapt_gallery_embedding)."""
        self._maybe_adapt_gallery_embedding(
            r.embedding, r.distance, gesture_passed=False,
            is_screen=r.detail.get("screen_flagged", 0) > 0)

    def _maybe_adapt_gallery_embedding(self, embedding, distance, *, gesture_passed: bool,
                                       is_screen: bool) -> None:
        """Opt-in adaptive gallery: on a genuine, live, non-screen grant, offer the verifying
        embedding to the recognizer, which applies the anti-poisoning gates (distance-to-enrollment
        ceiling, cooldown, size cap) and persists it separately. Stage 9 (F-47): a grant after a
        passed phase 2 qualifies too (gesture_passed=True) -- paranoid mode adapted never before.
        Best-effort: never let adaptation break an unlock."""
        if not self.cfg.adaptive_gallery:
            return
        try:
            dec = self.recog.maybe_adapt(
                embedding,
                liveness_passed=True,          # a grant => live for the mode
                is_screen=bool(is_screen),     # ANY screen flag blocks adaptation
                mode=self.cfg.liveness_mode,
                gesture_passed=gesture_passed,
                union_distance=distance,
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
        # Stage 9 (F-77): the CP accepts non-empty fields up to 256 / 256 / 1024 UTF-16 units with
        # no NUL and answers malformed-response otherwise -- after this side had already granted,
        # reset the lockout and grown the gallery. The same rule here makes such a blob
        # "no-credentials" before anything is released.
        u, pw, d = creds.get("u"), creds.get("p"), creds.get("d") or "."
        for value, cap in ((u, CP_MAX_USERNAME), (pw, CP_MAX_PASSWORD), (d, CP_MAX_DOMAIN)):
            if (not isinstance(value, str) or not value or "\0" in value
                    or len(value.encode("utf-16-le")) // 2 > cap):
                log.warning("stored credential is not usable by the sign-in screen "
                            "(empty, too long or NUL) -- treated as no-credentials")
                return None
        return {
            "ok": True,
            "username": creds["u"],
            "password": creds["p"],
            "domain": creds.get("d", "."),
        }

    def _left_sign(self) -> "float | None":
        """The calibrated turn sign for the current camera (Stage 9, R6), or None = the
        LEFT_IS_NEGATIVE_YAW default."""
        cal = getattr(self, "_calibration", None) or {}
        entry = (cal.get("cameras") or {}).get(self._camera_id())
        if isinstance(entry, dict) and isinstance(entry.get("left_is_negative_yaw"), bool):
            return -1.0 if entry["left_is_negative_yaw"] else 1.0
        return None

    def _camera_id(self) -> str:
        """What a calibration is bound to: the camera's device name when configured (R10), else
        its index."""
        name = str(getattr(self.cfg, "camera_name", "") or "").strip()
        return f"name:{name}" if name else f"index:{self.cfg.camera_index}"

    def _run_challenge(self, kind_name: "str | None" = None, *, identity: bool = True) -> dict:
        """Stage 9 (act 9b R4): the lock screen's phase 2 -- a GestureSequence.

        ``kind_name`` is the sequence armed in phase 1, e.g. "turn_left,nod". The camera is
        acquired first; the round's clock starts at the FIRST frame that arrives (F-140), so a
        cold open no longer eats the user's window. Identity binding (Stage 7-i, 8b F-11): a frame
        whose face does not match, or that the anti-screen check flagged, is not fed to the
        sequence -- an impostor's motion never reaches it. The round is also bounded by the
        request's own deadline, so it cannot outlive what the lock screen can still use.

        Reply (internal; unlock_gesture turns it into the wire reply):
          {"ok": False, "reason": "camera-busy" | "no-frames" | "no-enrollment" | "engine-error"
                                  | "deadline-exceeded" | "bad-request"}
          {"ok": True, "challenge", "prompt", "passed", "state", "reason" (the sequence's failure
           or None), "identity_frames", "distance_best", "faces", "frames_ok", "screen_flagged",
           "screen_checked", "scene_luma", "fps", "_embedding" (best identity frame, never sent)}
        """
        from .liveness import Challenge, GestureSequence
        from .recognizer import EngineError

        if self._camera_leased_out():
            return {"ok": False, "reason": "camera-busy"}
        try:
            kinds = tuple(Challenge[k.strip().upper()] for k in str(kind_name or "").split(","))
            # late-bound clock: the round runs on this module's time (a test can drive it)
            seq = GestureSequence(kinds, clock=lambda: time.monotonic(), left_sign=self._left_sign())
        except (KeyError, ValueError):
            # Stage 9 (F-65): the value is never echoed back or into the audit.
            return {"ok": False, "reason": "bad-request"}

        identity_frames = 0
        distance_best: float | None = None
        best_emb = None
        faces = 0
        frames_ok = 0
        screen_flagged = 0
        screen_checked = 0
        luma_max: "float | None" = None
        t_first: "float | None" = None
        n = 0

        def request_late() -> bool:
            t0 = getattr(self, "_req_started", None)
            if t0 is None:
                return False
            client = getattr(self, "_client_budget_s", None)
            limit = UNLOCK_GESTURE_DEADLINE_S if client is None else min(UNLOCK_GESTURE_DEADLINE_S, client)
            return time.monotonic() - t0 > limit - 0.3

        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                return {"ok": False, "reason": _camera_reason({"reason": busy})}
            try:
                for _ in range(2):
                    cam.read()
                # Frames first: the round cannot start before one arrives, but a camera that never
                # delivers is bounded by camera_open_attempt_cap_s (the same ceiling as an open).
                first_deadline = time.monotonic() + float(self.cfg.camera_open_attempt_cap_s)
                while not seq.done:
                    if request_late():
                        return {"ok": False, "reason": "deadline-exceeded"}
                    frame = cam.read()
                    if frame is None:
                        if t_first is None and time.monotonic() >= first_deadline:
                            break
                        seq.tick()
                        time.sleep(0.01)          # F-131: no busy spin on a dead device
                        continue
                    frames_ok += 1
                    if t_first is None:
                        t_first = time.monotonic()
                        seq.start()
                    try:
                        sl = scene_luma(frame)
                        luma_max = sl if luma_max is None else max(luma_max, sl)
                    except Exception as e:
                        log.warning("scene luma failed on a gesture frame: %r", e)
                    # D-86: the gesture round can be dumped like the burst and the probe.
                    self._maybe_dump_frame(frame, f"gesture{n}")
                    n += 1
                    try:
                        a = self.recog.analyze_frame(frame)
                    except EngineError as e:
                        log.warning("gesture round: engine error on a frame: %s", e)
                        seq.tick()
                        continue
                    except RuntimeError as e:
                        # The engine refused the whole round (no enrollment, models gone): a fault,
                        # not a failed attempt (Stage 9, R5 / F-62).
                        log.warning("gesture round did not run: %s", e)
                        return {"ok": False, "reason": "no-enrollment"
                                if getattr(self.recog, "_refs", None) is None else "engine-error"}
                    if not a.face:
                        seq.tick()
                        continue
                    faces += 1
                    if distance_best is None or a.distance < distance_best:
                        distance_best = a.distance
                    if a.screen is not None:
                        screen_checked += 1
                        if a.screen:
                            screen_flagged += 1
                    if identity and (not a.is_match or a.screen is True):
                        seq.tick()
                        continue
                    if identity:
                        identity_frames += 1
                        if best_emb is None or a.distance <= distance_best:
                            best_emb = a.embedding
                    seq.feed(a.landmark, a.pose)
            except CameraReadTimeout:
                self._camera_problem = "camera-error"      # R10 (F-144): the read hung
                return {"ok": False, "reason": "camera-error"}
            finally:
                self._done_with(cam)

        self._note_camera_health(frames_ok, luma_max, "gesture")
        if frames_ok == 0 or self._burst_defect(frames_ok, luma_max) is not None:
            # F-131: the camera delivered nothing (or only black) -- a device fault, lockout-neutral.
            log.info("gesture round: camera delivered %d frame(s), luma=%s -> no-frames",
                     frames_ok, _fmt_luma(luma_max))
            return {"ok": False, "reason": "no-frames"}
        elapsed = (time.monotonic() - t_first) if t_first else 0.0
        fps = round(frames_ok / elapsed, 1) if elapsed > 0 else None
        log.info("gesture round %s: passed=%s reason=%s steps=%d identity_frames=%d faces=%d "
                 "screen=%d/%d sceneL=%s best=%s fps=%s", kind_name, seq.passed, seq.reason,
                 seq.steps_done, identity_frames, faces, screen_flagged, screen_checked,
                 _fmt_luma(luma_max), "n/a" if distance_best is None else "%.3f" % distance_best,
                 fps)
        return {
            "ok": True,
            "challenge": ",".join(k.name.lower() for k in kinds),
            "prompt": self._prompt_for(",".join(k.name.lower() for k in kinds)),
            "passed": seq.passed,
            "state": seq.state.name.lower(),
            "reason": seq.reason,
            "steps_done": seq.steps_done,
            "identity_frames": identity_frames,
            "distance_best": None if distance_best is None else round(distance_best, 4),
            "faces": faces,
            "frames_ok": frames_ok,
            "screen_flagged": screen_flagged,
            "screen_checked": screen_checked,
            "scene_luma": None if luma_max is None else round(luma_max, 2),
            "fps": fps,
            "_embedding": best_emb,
        }

    def _prompt_for(self, kind_name: str) -> str:
        """Localized instruction for the armed sequence, in the CURRENT cfg.language: each step's
        prompt, the later ones lower-cased, joined by the language's "then" -- e.g. "Turn your
        head left, then nod your head". Resolved from cfg on every call (reload can change the
        language; this process never calls set_language)."""
        from .i18n import DEFAULT_LANG, TRANSLATIONS
        table = TRANSLATIONS.get(self.cfg.language) or TRANSLATIONS[DEFAULT_LANG]
        en = TRANSLATIONS[DEFAULT_LANG]

        def tr(key):
            return table.get(key) or en.get(key, key)

        parts = [tr(f"gesture.prompt.{k.strip()}") for k in str(kind_name).split(",") if k.strip()]
        if not parts:
            return ""
        out = parts[0]
        for p in parts[1:]:
            out += tr("gesture.then") + (p[:1].lower() + p[1:])
        return out

    def _issue_gesture_token(self) -> tuple[str, str, str]:
        """Pick a random two-movement sequence, arm the one-shot token slot, return
        (sequence, prompt, token). The order is drawn with the system CSPRNG so an observer
        cannot predict what the next lock screen will ask for. Overwrites any previous slot: the
        newest phase-1 reply is the only one that can be answered."""
        from .liveness import random_sequence
        kind = ",".join(k.name.lower() for k in random_sequence())
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

    def _presence_probe(self) -> tuple[str, bool]:
        """(state, real) — semantics depend on config.presence_mode.

        ``state`` is one of ``"present"`` / ``"uncertain"`` / ``"absent"`` (7c-6), ``"unknown"``
        (Stage 9) or ``"error"``. ``uncertain``
        means something face-shaped and close was seen but anti-screen flagged it: it is NOT a
        sighting and NOT an absence, and on its own it must never lock. Only the recognition mode
        can produce it -- detection has no distance and no anti-screen, so it stays two-valued.

        recognition: ``present`` = enrolled face seen at or just past the threshold, anti-screen ok.
        detection:   ``present`` = *any* face detected by YuNet. ``real`` is
                     reported as True (anti-spoofing not evaluated).

        Stage 9 (act 9b R10, F-143): every "cannot see" case -- leased to the wizard, busy, not
        connected, a read that hung, zero or black frames -- is ``"unknown"`` with the cause in
        ``self._probe_why``. It used to be reported as ``present``, which Status then showed as a
        sighting; the monitor now takes it as no decision at all, neither presence nor absence.
        """
        self._probe_why = None
        if self._camera_leased_out():
            log.info("presence probe skipped: camera leased out to enrollment")
            return self._unknown("leased")
        if self.cfg.presence_mode == "detection":
            return self._presence_probe_detection()
        return self._presence_probe_recognition()

    def _unknown(self, why: str) -> tuple:
        """A probe that could not see: no decision, with the cause for Status (R10)."""
        self._probe_why = why
        return "unknown", False

    def _note_probe_errors(self, errors: list) -> None:
        """Stage 8b (F-22): log the FIRST engine exception of an error episode with its traceback,
        then stay quiet (DEBUG) until a probe runs clean again -- once per episode, not once per
        60-second tick."""
        if not errors:
            self._probe_error_logged = False
            return
        if not getattr(self, "_probe_error_logged", False):
            self._probe_error_logged = True
            e = errors[0]
            log.warning("presence probe: engine error on %d frame(s); first: %r "
                        "(logged once per episode)", len(errors), e,
                        exc_info=(type(e), e, e.__traceback__))
        else:
            log.debug("presence probe: engine error persists (%d frame(s)): %r",
                      len(errors), errors[0])

    def _presence_probe_recognition(self) -> tuple[str, bool]:
        # Camera-health telemetry (7b): count the frames that actually arrived and keep the
        # brightest scene luma among them, then judge AFTER the lock is dropped --
        # _note_camera_health takes _cam_lock and threading.Lock is not reentrant. ``scene_luma``
        # is the SAME canonical helper the verify burst uses, so the two paths can never disagree
        # about what "black" means. The result is accumulated instead of returned from inside the
        # lock for that reason; the values themselves are what this returned before.
        frames_ok = 0
        luma_max: "float | None" = None
        result = (False, False)
        seen: list = []          # (ok, distance, real) per analysed frame -- diagnostics only
        errors: list = []        # engine exceptions this burst (Stage 8b, F-22)
        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("presence probe skipped: %s", busy)
                return self._unknown("not-found" if busy == "camera-not-found" else "busy")
            try:
                for _ in range(2):
                    cam.read()
                for _ in range(3):
                    frame = cam.read()
                    if frame is None:
                        continue
                    frames_ok += 1
                    try:                  # Stage 8b (F-44): see _analyze_burst
                        sl = scene_luma(frame)
                        luma_max = sl if luma_max is None else max(luma_max, sl)
                    except Exception as e:
                        log.warning("scene luma failed on a probe frame: %r", e)
                    # Same observer as the verify burst, same position: before the engine, with
                    # no return value anything here reads. This loop carries no frame index, so
                    # the tag is the bare path name; the timestamp in the file name orders them.
                    self._maybe_dump_frame(frame, "probe")
                    try:
                        ok, dist, real = self.recog.verify_frame(frame)
                    except Exception as e:
                        seen.append((None, None, None))
                        errors.append(e)
                        continue
                    seen.append((ok, dist, real))
                    if ok:
                        result = (True, real)
                        break
            except CameraReadTimeout:
                self._camera_problem = "camera-error"
                return self._unknown("camera-error")
            finally:
                self._done_with(cam)
        # No retry on this path: the probe runs on a timer, so the next tick already gets the
        # fresh camera, and absence-strike policy stays entirely the caller's business.
        self._note_camera_health(frames_ok, luma_max, "probe-recog")
        self._note_probe_errors(errors)
        defect = self._burst_defect(frames_ok, luma_max)
        if not result[0] and defect is not None:
            # Either no frame arrived at all, or every one that did was black. The DEVICE is blind;
            # the user is not necessarily gone. Report it the way a busy/leased camera already is
            # -- present, so the monitor does not spend an absence strike (and eventually a lock)
            # on a camera fault. The self-heal above has already dropped the cache, so the next
            # probe opens a fresh one.
            log.info("presence probe: %s (frames_ok=%d luma_max=%s) -> unknown, not absence",
                     defect, frames_ok, "n/a" if luma_max is None else "%.2f" % luma_max)
            return self._unknown("zero-frames" if defect == "zero-frames" else "black")
        # Reaching here means the camera-error gate above did NOT fire, so anything short of a
        # sighting is about the USER, not the device. ``result`` above is the strong-match early
        # exit and still gates the camera-error branch byte-for-byte; the tri-state verdict is
        # derived from the frame classes, which also see the weak and suspect frames that
        # ``result`` cannot represent.
        threshold, soft = self.cfg.threshold, self.cfg.presence_soft_margin
        # Stage 9 (F-134): an engine error in the burst with no sighting at all is an error too --
        # a fault must not be read as an absence.
        sighting = any(_probe_frame_class(ok, d, r, self.cfg.threshold,
                                          self.cfg.presence_soft_margin) in ("strong", "weak")
                       for ok, d, r in seen)
        if seen and (all(ok is None for ok, _d, _r in seen)
                     or (errors and not sighting)):
            # Stage 8b (F-22). Defect: an all-error burst fell through _probe_verdict to "absent".
            # Consequence: no enrollment, a model or a CUDA failure earned an absence strike every
            # idle tick and ended in LockWorkStation. Fix: an engine that could not judge a single
            # frame is an ERROR, reported as ok:false, which the monitor skips without a strike.
            # _probe_verdict and every number it uses are untouched; a burst with at least one
            # frame the engine DID judge is classified exactly as before.
            log.info("presence probe error (recognition): frames=%s frames_ok=%d luma_max=%s "
                     "state=error", _fmt_probe_frames(seen, threshold, soft), frames_ok,
                     _fmt_luma(luma_max))
            return "error", False
        state = _probe_verdict(seen, threshold, soft)
        # DEBUG on present (every interval on a healthy machine), INFO on the two states worth
        # reading -- an absence about to cost a strike, and the uncertainty that used to be one.
        line = ("presence probe %s (recognition): frames=%s hint=%s frames_ok=%d luma_max=%s "
                "state=%s")
        args = (state, _fmt_probe_frames(seen, threshold, soft), _probe_hint(seen, threshold, soft),
                frames_ok, _fmt_luma(luma_max), state)
        if state == "present":
            log.debug(line, *args)
        else:
            log.info(line, *args)
        return state, state == "present"

    def _presence_probe_detection(self) -> tuple[str, bool]:
        # Same camera-health bookkeeping as the recognition probe above, same reasons. Two-valued by
        # construction: YuNet answers yes/no with no distance and no anti-screen, so there is no
        # middle ground to report and "uncertain" can never come out of this path.
        frames_ok = 0
        luma_max: "float | None" = None
        result = (False, True)
        judged = 0
        errors: list = []        # Stage 9 (F-133): detector exceptions this burst
        with self._cam_lock:
            cam, busy = self._acquire_camera()
            if busy:
                log.info("presence probe skipped: %s", busy)
                return self._unknown("not-found" if busy == "camera-not-found" else "busy")
            try:
                for _ in range(2):
                    cam.read()
                for _ in range(5):
                    frame = cam.read()
                    if frame is None:
                        continue
                    frames_ok += 1
                    try:                  # Stage 8b (F-44): see _analyze_burst
                        sl = scene_luma(frame)
                        luma_max = sl if luma_max is None else max(luma_max, sl)
                    except Exception as e:
                        log.warning("scene luma failed on a probe frame: %r", e)
                    try:
                        found = self.detector.has_face(frame)
                    except Exception as e:
                        errors.append(e)
                        continue
                    judged += 1
                    if found:
                        result = (True, True)
                        break
            except CameraReadTimeout:
                self._camera_problem = "camera-error"
                return self._unknown("camera-error")
            finally:
                self._done_with(cam)
        self._note_camera_health(frames_ok, luma_max, "probe-detect")
        self._note_probe_errors(errors)
        defect = self._burst_defect(frames_ok, luma_max)
        if not result[0] and defect is not None:
            # Same reasoning as the recognition probe above: a blind camera is a camera fault.
            log.info("presence probe: %s (frames_ok=%d luma_max=%s) -> unknown, not absence",
                     defect, frames_ok, "n/a" if luma_max is None else "%.2f" % luma_max)
            return self._unknown("zero-frames" if defect == "zero-frames" else "black")
        if not result[0] and errors and judged == 0:
            # Stage 9 (F-133): the detector could not judge a single frame (YuNet missing or
            # unreadable) -- an error, exactly like the recognition probe's all-error burst.
            return "error", False
        # Symmetric to the recognition probe. YuNet only answers yes/no, so there are no distances
        # to report: an absent verdict here means has_face said no on every frame that arrived.
        state = "present" if result[0] else "absent"
        if result[0]:
            log.debug("presence probe present (detection): frames_ok=%d luma_max=%s state=%s",
                      frames_ok, _fmt_luma(luma_max), state)
        else:
            log.info("presence probe absent (detection): frames_ok=%d faces=0 luma_max=%s state=%s",
                     frames_ok, _fmt_luma(luma_max), state)
        # ``real`` stays True on both outcomes -- anti-spoofing is not evaluated in this mode, and
        # that is exactly what result[1] carried before 7c-6.
        return state, result[1]

    # ---------- pipe ----------

    def _status(self) -> dict:
        from .config import EMBED_PATH
        state = self._service_state()
        return {
            "ok": True,
            "uptime_s": time.time() - self._started_at,
            "config": asdict(self.cfg),
            "enrollment": EMBED_PATH.exists(),
            "lockout": self._lockout.status(),
            "audit": self._audit.status(),
            # Stage 8b, additive: False while unlock is refused for custody.
            "data_dir_secure": not getattr(self, "_data_dir_insecure", False),
            # Stage 9 (F-101): the first custody / model problem, for the tray's Status row.
            "custody_problem": getattr(self, "_custody_problem", None),
            "models_problem": getattr(self, "_models_problem", None),
            # Stage 9, additive: serving | refusing (+why), and the lock screen's verdict on the
            # stored password (§2.1: set by report_result ok=false, cleared by a new password).
            "state": state["state"],
            **({"why": state["why"]} if "why" in state else {}),
            "password_rejected": password_rejected(),
            "protocol": PROTOCOL_VERSION,
            # Stage 9 (R10), additive: the camera as the service last saw it.
            "camera": {"problem": self._camera_problem,
                       "open": getattr(self, "_cam", None) is not None,
                       "warm": time.monotonic() < self._warm_until,
                       "boost_disabled": self._boost_disabled},
        }

    def _refusal(self) -> "str | None":
        """Stage 9 (R1 / R11): why this process refuses face functions, or None when it serves.
        One token per cause, in a fixed order; later stages add their causes here."""
        if getattr(self, "_not_owner", False):
            return "not-owner"
        if getattr(self, "_data_dir_insecure", False):
            return "custody"
        if getattr(self, "_models_problem", None):
            return "no-models"
        # R9 (F-112): a lockout state that cannot be saved refuses face unlock -- retried on
        # every check, so the refusal ends as soon as the disk takes the state again.
        lk = getattr(self, "_lockout", None)
        if lk is not None and not getattr(lk, "store_ok", True) and not lk.retry_save():
            return "lockout-store-error"
        return None

    def _service_state(self) -> dict:
        why = self._refusal()
        return {"state": "serving"} if why is None else {"state": "refusing", "why": why}

    def _reload_config(self) -> dict:
        # strict=True: a good config is already in effect, so a broken file must be REJECTED and
        # the running one kept. Config.load()'s default degrade-to-defaults is right for a cold
        # start (nothing to preserve) and wrong here -- it would swap the live service onto
        # defaults behind the user's back. Both the read/parse and validate() now sit inside the
        # try: a TOMLDecodeError used to escape as the generic "exception: ..." reply, and a
        # wrong-TYPED value raises TypeError out of the range comparisons rather than ValueError.
        try:
            new_cfg = Config.load(strict=True)
        except (ValueError, TypeError) as e:
            return {"ok": False, "reason": f"invalid-config: {_scrub(e)}"}
        old = self.cfg
        old_index = self.cfg.camera_index
        old_name = getattr(self.cfg, "camera_name", "")
        old_persistent = self.cfg.persistent_camera
        # Stage 8b (F-15). Defect: self.cfg was swapped FIRST and the lockout / audit were
        # reconfigured after it, so a failure there left a half-applied reload (new cfg, old
        # lockout numbers) and escaped as a generic "exception" reply. Fix: reconfigure the two
        # stateful helpers first, roll them back if either raises, and swap cfg only after both
        # succeeded -- a reload is now all or nothing.
        try:
            self._lockout.reconfigure(new_cfg.max_face_attempts, new_cfg.lockout_seconds)
            self._audit.reconfigure(new_cfg.audit_log, new_cfg.audit_max_mb)
        except Exception as e:
            log.exception("reload_config: applying the new settings failed; keeping the old ones")
            try:
                self._lockout.reconfigure(old.max_face_attempts, old.lockout_seconds)
                self._audit.reconfigure(old.audit_log, old.audit_max_mb)
            except Exception:
                log.exception("reload_config: rollback failed")
            return {"ok": False, "reason": f"reload-failed: {_scrub(e)}"}
        self.cfg = new_cfg
        self.recog.cfg = new_cfg
        # Stage 9 (D-40): the adaptive toggle takes effect at once, both ways.
        try:
            self.recog._refresh_refs()
        except Exception:
            log.exception("reload_config: refreshing the matching set failed")
        # Stage 9 (F-136): switching the frame dump off removes what it wrote.
        if old.debug_dump_frames and not new_cfg.debug_dump_frames:
            try:
                purge_debug_frames(APP_DIR)
            except Exception:
                log.exception("reload_config: purging debug frames failed")
        # Reset camera if camera-affecting settings changed. The new cfg is already
        # applied above, so a failure here must not abort the reload and strand the
        # OLD camera open under the NEW settings -- log it and carry on.
        # (Stage 9, R10: switching auto_lock off releases the camera too.)
        if (new_cfg.camera_index != old_index or new_cfg.camera_name != old_name
                or new_cfg.persistent_camera != old_persistent
                or (old.auto_lock and not new_cfg.auto_lock)):
            try:
                with self._cam_lock:
                    self._release_camera()
            except Exception:
                log.exception("reload_config: releasing the webcam failed")
        return {"ok": True, "config": asdict(new_cfg)}

    # ---- caller identity (Stage 9: no config can switch these gates off, R2) ----

    def _caller_sid(self, handle) -> "str | None":
        """SID of the client on ``handle`` (None when unreadable). A method so the selftests can
        stand in for the lock screen without a real SYSTEM process."""
        return _pipe_client_sid_string(handle)

    def _require_caller(self, handle, want: str, cmd: str) -> "dict | None":
        """None when the caller's SID is ``want``; else the not-authorized reply (logged with the
        client diagnostics -- resolved only here, on a rejection, D-45)."""
        sid = self._caller_sid(handle)
        if sid == want:
            return None
        log.warning("%s rejected: caller sid=%s is not %s (%s)", cmd, sid, want,
                    _pipe_client_diag(handle) if handle is not None else "no handle")
        return {"ok": False, "reason": "not-authorized"}

    def _v2_gate(self, req: dict, cmd: str) -> "dict | None":
        """§2.1: unlock / unlock_gesture / report_result speak protocol v2 only. A CP of another
        version gets version-mismatch -- the tile then says "update Face Unlock" and packs nothing."""
        v = req.get("v")
        if isinstance(v, bool) or v != PROTOCOL_VERSION:
            log.warning("%s refused: protocol version %r, this service speaks %d", cmd, v,
                        PROTOCOL_VERSION)
            return {"ok": False, "reason": "version-mismatch"}
        return None

    def _take_budget(self, req: dict) -> bool:
        """§2.1 (F-61): read the CP's remaining budget. False on a malformed value. Absent means
        "no client budget": the server deadlines alone apply."""
        self._client_budget_s = None
        raw = req.get("budget_ms")
        if raw is None:
            return True
        if isinstance(raw, bool) or not isinstance(raw, int) or not (0 < raw <= CLIENT_BUDGET_MAX_MS):
            return False
        self._client_budget_s = raw / 1000.0 - CLIENT_BUDGET_RESERVE_S
        return True

    def _handle(self, req: dict, handle=None) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            # Stage 9 (R11): the ping carries the service state; a refusing service is ALIVE.
            return {"ok": True, "pong": True, **self._service_state()}

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
            # No self-connect needed here: this shutdown request itself unblocked ConnectNamedPipe,
            # so after this _serve_one() finishes the loop re-checks _stop and exits.
            return {"ok": True, "shutting_down": True}

        if cmd == "pause_camera":
            # Release the webcam and ignore probe/verify for the requested
            # number of seconds so the enrollment wizard can own it.
            # Stage 8b (F-16, act A-5). Defect: any float was accepted, inf and 1e308 included.
            # Consequence: a lease that never ends -- the service blind for good, and presence
            # reporting "present" for as long. Fix: a finite number, clamped to [5, 600] s;
            # anything else is a bad-request and changes nothing.
            raw = req.get("seconds", 120)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                    or not math.isfinite(raw):
                return {"ok": False, "reason": "bad-request"}
            seconds = min(PAUSE_CAMERA_MAX_S, max(PAUSE_CAMERA_MIN_S, float(raw)))
            self._camera_paused_until = time.monotonic() + seconds
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
            # Report what is left of the lease, not the deadline itself: the deadline is a
            # monotonic since-boot number now and would be meaningless in a log line.
            remaining = max(0.0, self._camera_paused_until - time.monotonic())
            self._camera_paused_until = 0.0
            log.info("camera lease cleared (%.1fs still remained)", remaining)
            return {"ok": True}

        if cmd in ("build_enrollment", "clear_enrollment", "verify", "presence", "calibrate_turn"):
            # Stage 9 (R1): a refusing service runs no face function at all.
            why = self._refusal()
            if why is not None:
                return {"ok": False, "reason": why}

        if cmd == "build_enrollment":
            try:
                if req.get("replace") is True:
                    return self._with_pose(self._build_replace())
                return self._with_pose(self._build_add())
            except Exception as e:
                log.exception("build_enrollment failed")
                return {"ok": False, "reason": _scrub(e)}

        if cmd == "clear_enrollment":
            return self._clear_enrollment()

        if cmd == "calibrate_turn":
            return self._calibrate_turn(req, handle)

        if cmd == "verify":
            # §2.3: a diagnostic for the owner's own tools -- SELF only, no strike, no secret.
            denied = self._require_caller(handle, current_user_sid(), "verify")
            if denied is not None:
                return denied
            r = self._capture_and_verify()
            self._audit.write("verify", r.detail)
            # "verdict" is additive: the four legacy keys keep their names, types and values, so
            # an older client that ignores unknown keys is unaffected. The value is read from the
            # burst detail (which already carries it) with .get(), so a detail dict that somehow
            # lacks one yields null rather than raising out of the handler.
            return {"ok": True, "match": r.match, "distance": r.distance, "real": r.real,
                    "verdict": r.detail.get("verdict")}

        if cmd == "presence":
            state, real = self._presence_probe()
            if state == "error":
                # Stage 8b (F-22): engine error, not absence -> ok:false, never a strike.
                return {"ok": False, "reason": "engine-error", "present": False, "real": False,
                        "mode": self.cfg.presence_mode, "state": "error"}
            # ``present`` is kept for compatibility and keeps its old meaning exactly: the manual
            # probes in the tray and the Status window read it, and an older monitor that knows
            # nothing about ``state`` still sees uncertain as not-present (i.e. the pre-7c-6
            # behaviour) rather than something it cannot interpret.
            if state == "unknown":
                # Stage 9 (R10): the camera could not see -- no decision, with the cause.
                return {"ok": True, "present": False, "real": False,
                        "mode": self.cfg.presence_mode, "state": "unknown",
                        "why": self._probe_why or "camera-error"}
            return {"ok": True, "present": state == "present", "real": real,
                    "mode": self.cfg.presence_mode, "state": state}

        if cmd in ("unlock", "unlock_gesture", "report_result"):
            # SYSTEM gate FIRST, unconditionally (Stage 9, R2: no config key can lift it): only the
            # lock-screen Credential Provider -- LogonUI runs as SYSTEM -- may release or settle a
            # credential. Then the protocol version, before anything else is looked at.
            denied = self._require_caller(handle, SYSTEM_SID_STRING, cmd)
            if denied is not None:
                return denied
            bad = self._v2_gate(req, cmd)
            if bad is not None:
                return bad
            if cmd == "report_result":
                return self._report_result(req)
            if not self._take_budget(req):
                return {"ok": False, "reason": "bad-request"}
            if cmd == "unlock":
                return self._unlock()
            return self._unlock_gesture(req)

        return {"ok": False, "reason": "unknown-command"}

    def _unlock_preflight(self, audit) -> "dict | None":
        """The lockout-neutral refusals both unlock phases share, BEFORE the camera opens:
        a refusing service (R1 / R9), then the stored password the lock screen already saw
        rejected (§2.1, F-92), then the face lockout."""
        why = self._refusal()
        if why is not None:
            audit(why)
            return {"ok": False, "reason": why}
        if password_rejected():
            audit("password-rejected")
            return {"ok": False, "reason": "password-rejected"}
        rem = self._lockout.remaining()
        if rem > 0:
            audit("locked-out", rem)
            return {"ok": False, "reason": "locked-out", "retry_after_s": round(rem, 1)}
        return None

    def _unlock(self) -> dict:
        def _pre_audit(outcome, rem=None):
            if outcome == "locked-out":
                self._audit.write("unlock", {"verdict": "LOCKED_OUT", "match": False,
                                             "retry_after_s": round(rem, 1)})
            else:
                self._audit.write("unlock", {"outcome": outcome})
        refused = self._unlock_preflight(_pre_audit)
        if refused is not None:
            return refused
        r = self._capture_and_verify()
        # Stage 3.4 busy camera: the webcam is held by ANOTHER process (or leased to the wizard).
        # Refuse cleanly with reason "camera-busy" -- LOCKOUT-NEUTRAL (act 9b R5).
        if r.camera_busy:
            why = _camera_reason(r.detail)
            self._audit.write("unlock", {**r.detail, "outcome": why})
            return {"ok": False, "reason": why}
        # Stage 8b (F-18) / Stage 9 (R5): a burst in which no frame arrived, in which the engine
        # could not judge enough frames, or in which no face appeared at all is not a failed
        # attempt -- an honest reason, lockout-neutral.
        fault = self._burst_fault(r)
        if fault is not None:
            self._audit.write("unlock", {**r.detail, "outcome": fault})
            return {"ok": False, "reason": fault}
        # Stage 3.3 gated exposure boost: if the burst came back below the floor, try to raise
        # EXPOSURE and re-capture BEFORE the too-dark fallback. Gated (only below the floor) so a
        # normally-lit face is never blown out; transient (exposure always restored inside
        # _maybe_boost); lockout-neutral (a still-dark result stays too-dark below, adding no
        # strike). Boost telemetry is merged into r.detail so every unlock audit below carries it.
        # (Stage 9, D-83: the "not leased" conjunct here was dead -- a lease already answered
        # camera-busy above.)
        boost_audit: dict = {}
        if (r.scene_luma is not None and r.scene_luma < self.cfg.low_light_luma_min
                and self.cfg.low_light_boost):
            r_dark = r
            r, boost_audit = self._maybe_boost(r)
            # Stage 9 (F-130): the fault gate applies to the boosted re-capture too. A re-capture
            # that delivered nothing, or that the engine could not judge, is not a face verdict:
            # keep the dark burst (and its lockout-neutral too-dark answer) instead of turning a
            # device fault in a dim room into a strike.
            if r is not r_dark and self._burst_fault(r) is not None:
                log.info("low-light boost re-capture was faulty (%s); keeping the dark burst",
                         self._burst_fault(r))
                boost_audit = {**boost_audit, "boost_recapture_fault": self._burst_fault(r)}
                r = r_dark
            r = r._replace(detail={**r.detail, **boost_audit})
        # Stage 3.2 low-light gate: below the floor refuse honestly ("too-dark"), LOCKOUT-NEUTRAL.
        too_dark = False
        if r.scene_luma is not None:
            _grant, ll_reason, too_dark = evaluate_low_light(
                r.scene_luma, self.cfg.low_light_luma_min, r.match)
        if too_dark:
            self._audit.write("unlock", {**r.detail, "outcome": "too-dark"})
            return {"ok": False, "reason": ll_reason, "distance": r.distance, "real": r.real}
        # Stage 7-i phase 1. NEEDS_GESTURE means "recognized, but liveness wants an active
        # gesture". .get() is deliberate: a detail dict WITHOUT a verdict falls through to the old
        # no-match path -- fail closed, never into the gesture path.
        needs_gesture = r.detail.get("verdict") == "NEEDS_GESTURE"
        # R5: a no-match on frames that HAD a face is a strike (the fault gate above already took
        # the bursts with no face at all). A gesture escalation is not an attempt -- phase 2
        # records its own outcome. A MATCH is not recorded here either: its reset is part of the
        # grant (settled by report_result).
        if not needs_gesture and not r.match:
            self._lockout.record(False)
        if needs_gesture:
            if self._past_deadline(UNLOCK_DEADLINE_S):
                # D-62: the CP has already left -- arm no token for nobody.
                self._audit.write("unlock", {**r.detail, "outcome": "deadline-exceeded"})
                return {"ok": False, "reason": "deadline-exceeded"}
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
        if self._past_deadline(UNLOCK_DEADLINE_S):
            # F-19: too late for the CP to use it -- release nothing, reset nothing.
            self._audit.write("unlock", {**r.detail, "outcome": "deadline-exceeded"})
            return {"ok": False, "reason": "deadline-exceeded"}
        granted = self._release_credentials()
        if granted is None:
            self._lockout.record(True)     # the face matched: same reset as before 8b
            self._audit.write("unlock", {**r.detail, "outcome": "no-credentials"})
            return {"ok": False, "reason": "no-credentials"}

        def _audit(outcome: str) -> None:
            self._audit.write("unlock", {**r.detail, "outcome": outcome})

        def _commit() -> None:
            self._lockout.record(True)
            _audit("granted")
            self._maybe_adapt_gallery(r)
        return self._arm_grant(granted, _audit, _commit)

    def _unlock_gesture(self, req: dict) -> dict:
        # Stage 7-i phase 2: answer the gesture phase 1 asked for. Same perimeter as unlock (SYSTEM
        # and version checked by _handle), then the shared preflight, then the one-shot token, and
        # only then the camera. _release_credentials is unreachable until all of them passed.
        def _pre_audit(outcome, rem=None):
            self._audit_gesture(reason=outcome)
        refused = self._unlock_preflight(_pre_audit)
        if refused is not None:
            return refused
        slot = self._take_gesture_token(req.get("token"))
        if slot is None:
            # Absent / wrong / expired / already-burnt token. NOT a strike: this is a protocol
            # state, not a failed face attempt -- the camera never even opened.
            self._audit_gesture(reason="gesture-token-invalid")
            return {"ok": False, "reason": "gesture-token-invalid"}
        resp = self._run_challenge(slot["kind"], identity=True)
        if not resp.get("ok"):
            # The round never ran, or the device / engine / deadline cut it off: camera-busy,
            # no-frames, no-enrollment, engine-error, deadline-exceeded -- every one of them is
            # LOCKOUT-NEUTRAL (act 9b R5; F-62, F-131), each with its honest reason.
            raw = resp.get("reason") or "engine-error"
            self._audit_gesture(challenge=slot["kind"], reason=raw)
            return {"ok": False, "reason": raw}
        frames = int(resp.get("identity_frames") or 0)
        best = resp.get("distance_best")
        faces = int(resp.get("faces") or 0)

        # R6 telemetry for every round that ran (numbers only, no image data)
        self._audit.write("gesture_telemetry", {
            "faces": faces, "frames_ok": resp.get("frames_ok"), "fps": resp.get("fps"),
            "screen_flagged": resp.get("screen_flagged"),
            "screen_checked": resp.get("screen_checked"), "scene_luma": resp.get("scene_luma"),
            "steps_done": resp.get("steps_done"), "sequence_reason": resp.get("reason")})

        def _audit_round(reason, passed=None):
            self._audit_gesture(challenge=resp.get("challenge"), passed=passed,
                                identity_frames=frames, distance_best=best, reason=reason)

        if faces == 0:
            # R5: nobody in front of the camera for the whole round -- no attempt was made.
            _audit_round("no-face", passed=False)
            return {"ok": False, "reason": "no-face"}
        # R4 additional layer: too many screen-like frames among the frames with a face fails the
        # round, whatever the head did -- only with anti-screen on and in enough light for the
        # signal to mean something (the same floor as the too-dark gate).
        checked = int(resp.get("screen_checked") or 0)
        flagged = int(resp.get("screen_flagged") or 0)
        luma = resp.get("scene_luma")
        if (self.cfg.anti_screen and checked > 0 and flagged / checked >= SCREEN_DOUBT_FRAC
                and luma is not None and luma >= self.cfg.low_light_luma_min):
            self._lockout.record(False)
            _audit_round("screen-suspected", passed=False)
            return {"ok": False, "reason": "screen-suspected", "challenge": resp.get("challenge"),
                    "identity_frames": frames}
        if resp.get("reason") == "motion-before-prompt":
            # R4: moving before the prompt could be read is what a replay does -- a strike.
            self._lockout.record(False)
            _audit_round("motion-before-prompt", passed=False)
            return {"ok": False, "reason": "motion-before-prompt",
                    "challenge": resp.get("challenge"), "identity_frames": frames}
        # Two conditions, no new numbers: the sequence passed AND enough MATCHING frames were fed
        # (cfg.verify_required, the same bar the passive burst uses).
        passed = bool(resp.get("passed")) and frames >= self.cfg.verify_required
        if not passed:
            self._lockout.record(False)          # R5: gesture-failed on frames with a face
            _audit_round("gesture-failed", passed=resp.get("passed"))
            return {"ok": False, "reason": "gesture-failed",
                    "challenge": resp.get("challenge"), "state": resp.get("state"),
                    "identity_frames": frames}
        if self._past_deadline(UNLOCK_GESTURE_DEADLINE_S):
            # F-19: the round ran past what phase 2 can still deliver -- release nothing.
            _audit_round("deadline-exceeded", passed=True)
            return {"ok": False, "reason": "deadline-exceeded"}
        granted = self._release_credentials()
        if granted is None:
            self._lockout.record(True)     # the round passed: same reset as before 8b
            _audit_round("no-credentials", passed=True)
            return {"ok": False, "reason": "no-credentials"}

        emb = resp.get("_embedding")

        def _audit(outcome: str) -> None:
            self._audit_gesture(challenge=resp.get("challenge"), passed=True,
                                identity_frames=frames, distance_best=best, reason=outcome)

        def _commit() -> None:
            self._lockout.record(True)
            _audit("granted")
            # Stage 9 (act 9b R6, F-47 / D-40): a grant after a passed phase 2 may adapt the
            # gallery too -- the same gates and ceiling (threshold - adaptive_margin) as before;
            # never when any frame of the round was screen-flagged.
            self._maybe_adapt_gallery_embedding(emb, best, gesture_passed=True,
                                                is_screen=flagged > 0)
        return self._arm_grant(granted, _audit, _commit)

    # ---- grant settlement (§2.1 protocol v2: F-60, F-93) ----

    def _arm_grant(self, granted: dict, audit, commit) -> dict:
        """Attach a one-shot grant_id and arm the settlement: nothing is committed now. Once the
        reply is written the grant waits for report_result (GRANT_REPORT_TTL_S); ok=true commits
        (lockout reset, audit "granted", gallery adaptation), ok=false marks the stored password
        rejected, silence abandons it. An undelivered reply is audited and dropped."""
        grant_id = secrets.token_hex(16)
        granted["grant_id"] = grant_id

        def _delivered(delivered: bool) -> None:
            if not delivered:
                audit("grant-undelivered")
                return
            audit("delivered")
            old = getattr(self, "_report_slot", None)
            if old is not None:           # a newer grant replaces one that was never reported
                old["audit"]("grant-abandoned")
            self._report_slot = {
                "grant_id": grant_id,
                "expires": time.monotonic() + GRANT_REPORT_TTL_S,
                "commit": commit,
                "audit": audit,
            }
        self._pending_grant = _delivered
        return granted

    def _expire_report_slot(self) -> None:
        """Drop a grant whose report never came: abandoned, nothing committed."""
        slot = getattr(self, "_report_slot", None)
        if slot is not None and time.monotonic() >= slot["expires"]:
            self._report_slot = None
            log.info("grant abandoned: no report_result within %.0fs", GRANT_REPORT_TTL_S)
            slot["audit"]("grant-abandoned")

    def _report_result(self, req: dict) -> dict:
        self._expire_report_slot()
        gid, ok = req.get("grant_id"), req.get("ok")
        slot = getattr(self, "_report_slot", None)
        if (slot is None or not isinstance(gid, str) or not gid.isascii()
                or not isinstance(ok, bool)
                or not secrets.compare_digest(gid, slot["grant_id"])):
            return {"ok": False, "reason": "grant-unknown"}
        self._report_slot = None             # one-shot
        if ok:
            slot["commit"]()
        else:
            try:
                mark_password_rejected()
            except OSError as e:
                log.error("could not record the rejected password: %s", e)
            log.warning("the lock screen reports the stored password was REJECTED by Windows; "
                        "face unlock stays off until a new password is saved")
            slot["audit"]("password-rejected")
        return {"ok": True}

    def _past_deadline(self, budget_s: float) -> bool:
        """F-19: True once more than ``budget_s`` has passed since this request was read -- or,
        with protocol v2 (F-61), more than the client's own remaining budget minus the reply
        reserve, whichever is smaller. _serve_one stamps the read; a caller that never set the
        stamp (the selftests drive _handle directly) has no deadline to miss."""
        t0 = getattr(self, "_req_started", None)
        if t0 is None:
            return False
        client = getattr(self, "_client_budget_s", None)
        limit = budget_s if client is None else min(budget_s, client)
        late = time.monotonic() - t0 > limit
        if late:
            log.warning("request past its %.1fs deadline (%.1fs) -> failing closed, nothing "
                        "released", limit, time.monotonic() - t0)
        return late

    def _finish_grant(self, delivered: bool) -> None:
        """Settle the delivery half of the grant armed by the request just answered (see
        _arm_grant). Idempotent: the slot is taken before the callback runs."""
        commit, self._pending_grant = getattr(self, "_pending_grant", None), None
        if commit is not None:
            try:
                commit(delivered)
            except Exception:
                log.exception("grant bookkeeping failed")

    def _burst_fault(self, r: "VerifyOutcome") -> "str | None":
        """F-18: the unlock burst's device/engine fault, or None when the burst judged a face.
        "no-frames" -- the camera delivered nothing; "no-enrollment" / "engine-error" -- frames
        arrived but the engine could not judge a single one (told apart by whether a gallery is
        loaded). A burst with any judged frame is None: that is a face verdict, strikes apply."""
        frames_ok = r.detail.get("frames_ok")
        if frames_ok is None or r.detail.get("verdict") in (None, "SKIPPED"):
            return None               # not a measured burst (no telemetry): nothing to judge
        frames_ok = int(frames_ok)
        if frames_ok == 0:
            return "no-frames"
        errors = int(r.detail.get("engine_errors", 0) or 0)
        if errors >= frames_ok:
            return "no-enrollment" if getattr(self.recog, "_refs", None) is None else "engine-error"
        # Stage 9 (F-134): with engine faults in the burst and too few JUDGED frames left to reach
        # verify_required, the verdict is the fault's, not the face's -- neutral too.
        if errors > 0 and (frames_ok - errors) < int(self.cfg.verify_required) and not r.match:
            return "engine-error"
        # Stage 9 (act 9b R5): no face in any frame -- nobody tried; no strike.
        if "faces" in r.detail and int(r.detail.get("faces") or 0) == 0:
            return "no-face"
        return None

    def _with_pose(self, resp: dict) -> dict:
        """Stage 8b (D-17): add the built session's median pose to a successful build reply.
        Additive key; the wizard turns it into a warning. Numbers only, no image data."""
        pose = getattr(self.recog, "last_enroll_pose", None)
        if resp.get("ok") and pose:
            resp["pose"] = {k: (round(v, 1) if isinstance(v, float) else v)
                            for k, v in pose.items()}
        return resp

    def _build_add(self) -> dict:
        """``build_enrollment`` without ``replace`` (the wizard's "Add"): rebuild the gallery from
        every image in ENROLL_DIR. Stage 9 (D-82): audited, success or not."""
        from .config import ENROLL_DIR
        try:
            n = self.recog.enroll_from_dir(ENROLL_DIR)
        except Exception as e:
            self._audit.write("enroll_build", {"mode": "add", "ok": False,
                                               "reason": _scrub(e)[:200]})
            raise
        self._audit.write("enroll_build", {"mode": "add", "ok": True, "accepted": n,
                                           **self._enroll_telemetry()})
        return {"ok": True, "count": n}

    def _enroll_telemetry(self) -> dict:
        info = dict(getattr(self.recog, "last_enroll_info", {}) or {})
        return {k: info.get(k) for k in ("rejected", "other_person", "ear_open_median")}

    def _build_replace(self) -> dict:
        """``build_enrollment`` with ``replace``: the new session replaces the gallery ATOMICALLY.

        Stage 9 (act 9b R6, F-132). Before, the pending build already overwrote embeddings.npz and
        the in-memory refs, and only then were the old images deleted and the new ones moved --
        an OSError in those loops (an old JPG held by AV or a viewer) left the NEW gallery live,
        a mix of old and new images on disk, and a wizard reporting failure. Now:
          1. the gallery is COMPUTED from ENROLL_PENDING_DIR only -- nothing live changes;
          2. the old images are moved into a retired folder and the new ones into ENROLL_DIR --
             any failure moves everything back and the old gallery stays in force;
          3. embeddings.npz is replaced atomically and the service switches to it (the commit);
          4. the retired images are deleted (best effort: problems are reported, not raised);
          5. an audit record is written in every case.
        """
        from .config import ENROLL_DIR, ENROLL_PENDING_DIR
        for d in (ENROLL_DIR, ENROLL_PENDING_DIR):
            if is_reparse(d):
                return {"ok": False, "reason": f"{d.name} is a reparse point (not followed)"}
        try:
            embeds, _rep = self.recog.build_gallery(ENROLL_PENDING_DIR)   # raises: old kept
        except Exception as e:
            self._audit.write("enroll_build", {"mode": "replace", "ok": False,
                                               "reason": _scrub(e)[:200]})
            raise
        images = {".jpg", ".jpeg", ".png"}
        retired = ENROLL_DIR / ".retired"
        moved_old: list = []
        moved_new: list = []
        try:
            retired.mkdir(parents=True, exist_ok=True)
            if is_reparse(retired):
                raise OSError(f"{retired.name} is a reparse point")
            for p in list(ENROLL_DIR.iterdir()):
                if p.suffix.lower() in images and p.is_file() and not is_reparse(p):
                    os.replace(p, retired / p.name)
                    moved_old.append(p.name)
            for p in list(ENROLL_PENDING_DIR.iterdir()):
                if p.suffix.lower() in images and p.is_file() and not is_reparse(p):
                    os.replace(p, ENROLL_DIR / p.name)
                    moved_new.append(p.name)
            self.recog.commit_gallery(embeds)                     # the commit point
        except Exception as e:
            # Roll the files back; the gallery on disk and in memory is still the old one.
            for name in moved_new:
                try:
                    os.replace(ENROLL_DIR / name, ENROLL_PENDING_DIR / name)
                except OSError:
                    pass
            for name in moved_old:
                try:
                    os.replace(retired / name, ENROLL_DIR / name)
                except OSError:
                    pass
            log.exception("build_enrollment(replace): staging failed; the old gallery is kept")
            self._audit.write("enroll_build", {"mode": "replace", "ok": False,
                                               "reason": "staging-failed: " + _scrub(e)[:160]})
            return {"ok": False, "reason": "staging-failed"}
        problems: list = []
        _n, more = remove_tree_no_follow(retired)
        problems.extend(more)
        _n, more = remove_tree_no_follow(ENROLL_PENDING_DIR)
        problems.extend(more)
        if problems:
            log.warning("build_enrollment(replace): cleanup left %s", problems[:3])
        n = int(embeds.shape[0])
        log.info("build_enrollment(replace): %d accepted; %d old image(s) retired, %d promoted",
                 n, len(moved_old), len(moved_new))
        self._audit.write("enroll_build", {"mode": "replace", "ok": True, "accepted": n,
                                           "old_removed": len(moved_old),
                                           "promoted": len(moved_new),
                                           "cleanup_problems": len(problems),
                                           **self._enroll_telemetry()})
        resp = {"ok": True, "count": n, "replaced": True}
        if problems:
            resp["partial"] = True
        return resp

    def _clear_enrollment(self) -> dict:
        """Stage 8b (F-06). Defect: the wizard's "Delete enrollment" unlinked files on disk while
        the running service kept its gallery in memory. Consequence: the deleted face went on
        matching until the next service restart, although the confirmation promised deletion.
        Fix: the service itself forgets the gallery (refs + adaptive ring), deletes
        embeddings.npz and the whole enroll tree without following reparse points, drops any
        pending gesture token, and writes an audit record.
        Stage 9 (F-136): the frame dumps and the *.npz.tmp leftovers are face data too -- they go
        as well; (F-121) the in-memory forget no longer depends on the ring file being deletable."""
        from .config import ADAPTIVE_PATH, EMBED_PATH, ENROLL_DIR
        problems: list = []
        ring_problem = self.recog.clear_enrollment()
        if ring_problem:
            problems.append(ring_problem)
        self._gesture_slot = None
        removed = 0
        for path in (EMBED_PATH, EMBED_PATH.with_name(EMBED_PATH.name + ".tmp"),
                     ADAPTIVE_PATH.with_name(ADAPTIVE_PATH.name + ".tmp")):
            try:
                if path.exists():
                    path.unlink()
                    removed += 1
            except OSError as e:
                problems.append(f"{path.name}: {e}")
        n, more = remove_tree_no_follow(ENROLL_DIR)
        removed += n
        problems.extend(more)
        try:
            removed += purge_debug_frames(APP_DIR)
        except Exception as e:
            problems.append(f"debug_frames: {e!r}")
        self._audit.write("clear_enrollment", {"removed": removed, "problems": len(problems)})
        if problems:
            log.error("clear_enrollment: %d item(s) could not be removed: %s",
                      len(problems), "; ".join(problems[:3]))
            return {"ok": False, "reason": "partial", "removed": removed}
        log.info("clear_enrollment: gallery forgotten, %d item(s) removed", removed)
        return {"ok": True, "removed": removed}

    # ---- turn-sign calibration (Stage 9, act 9b R6 / F-117) ----

    def _load_calibration(self) -> dict:
        from .config import CALIBRATION_PATH
        try:
            data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.warning("calibration.json unreadable (%r); using the default turn sign", e)
            return {}

    def _calibrate_turn(self, req: dict, handle) -> dict:
        """``calibrate_turn``: learn which way yaw moves when THIS user on THIS camera turns to
        their own left. SELF only, and only while the wizard holds the camera lease.

        The wizard saves a few frames of a frontal face and of a turn to the user's left into
        ``<data>\\calibration\\`` and names them in the request ("frontal", "left": file names). The
        service measures the head pose with the SAME estimator the lock screen uses (1k3d68),
        takes the medians, and stores the sign in calibration.json bound to the camera; the
        frames are deleted either way. A turn smaller than the gesture's own YAW_DELTA is refused
        ("turn-too-small") -- such a turn would not pass the lock screen either. Without a
        calibration the LEFT_IS_NEGATIVE_YAW default applies (unchanged)."""
        from .config import CALIBRATION_DIR, CALIBRATION_PATH
        from .liveness import YAW_DELTA
        denied = self._require_caller(handle, current_user_sid(), "calibrate_turn")
        if denied is not None:
            return denied
        if not self._camera_leased_out():
            return {"ok": False, "reason": "not-leased"}
        names = {}
        for key in ("frontal", "left"):
            v = req.get(key)
            if (not isinstance(v, list) or not (1 <= len(v) <= 30)
                    or not all(isinstance(x, str) and x and "/" not in x and "\\" not in x
                               and ".." not in x for x in v)):
                return {"ok": False, "reason": "bad-request"}
            names[key] = v
        poses: dict = {"frontal": [], "left": []}
        try:
            if is_reparse(CALIBRATION_DIR):
                return {"ok": False, "reason": "bad-request"}
            for key, files in names.items():
                for name in files:
                    img = imio.imread(CALIBRATION_DIR / name)
                    pose = self.recog.pose_of(img) if img is not None else None
                    if pose is not None:
                        poses[key].append(pose)
        finally:
            _n, _p = remove_tree_no_follow(CALIBRATION_DIR)   # face frames: never kept
        if len(poses["frontal"]) < 1 or len(poses["left"]) < 1:
            return {"ok": False, "reason": "no-face"}
        yaw_front = float(np.median([y for _p, y in poses["frontal"]]))
        yaw_left = float(np.median([y for _p, y in poses["left"]]))
        delta = yaw_left - yaw_front
        if abs(delta) <= YAW_DELTA:
            self._audit.write("calibrate_turn", {"ok": False, "delta_deg": round(delta, 1)})
            return {"ok": False, "reason": "turn-too-small", "delta_deg": round(delta, 1)}
        cal = self._load_calibration()
        cams = cal.get("cameras") if isinstance(cal.get("cameras"), dict) else {}
        cams[self._camera_id()] = {"left_is_negative_yaw": delta < 0,
                                   "delta_deg": round(delta, 1),
                                   "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        cal = {"version": 1, "cameras": cams}
        tmp = CALIBRATION_PATH.with_name(CALIBRATION_PATH.name + ".tmp")
        tmp.write_text(json.dumps(cal, indent=1), encoding="utf-8")
        os.replace(tmp, CALIBRATION_PATH)
        self._calibration = cal
        self._audit.write("calibrate_turn", {"ok": True, "delta_deg": round(delta, 1),
                                             "left_is_negative_yaw": delta < 0,
                                             "camera": self._camera_id()})
        log.info("turn calibration for %s: left turn moves yaw by %+.1f deg", self._camera_id(),
                 delta)
        return {"ok": True, "left_is_negative_yaw": delta < 0, "delta_deg": round(delta, 1),
                "camera": self._camera_id()}

    def _create_instance(self, first: bool):
        """One pipe instance. ``first`` claims the NAME (FILE_FLAG_FIRST_PIPE_INSTANCE): if anyone
        else already holds it, CreateNamedPipe fails. Remote clients are always rejected and the
        descriptor is always the hardened one (R2)."""
        if getattr(self, "_pipe_sa", None) is None:
            self._pipe_sa = _build_pipe_sa()
        open_mode = win32pipe.PIPE_ACCESS_DUPLEX
        if first:
            open_mode |= FILE_FLAG_FIRST_PIPE_INSTANCE
        return win32pipe.CreateNamedPipe(
            PIPE_NAME,
            open_mode,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT
            | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_MAX_INSTANCES,
            65536, 65536, 0, self._pipe_sa,
        )

    def _bind(self) -> bool:
        """Claim the pipe name. False -- with the stop flag set -- when someone else holds it.

        A loud log and a clean stop instead of a crash-loop; the watchdog retries the start and
        every refusal is logged, so a persistent squatter is visible rather than hidden."""
        try:
            self._listen = self._create_instance(first=True)
            return True
        except pywintypes.error as e:
            if e.winerror in (winerror.ERROR_ACCESS_DENIED, winerror.ERROR_ALREADY_EXISTS,
                              winerror.ERROR_PIPE_BUSY):
                log.error("pipe name %s occupied (winerror=%d) -- refusing to start "
                          "(possible squatter)", PIPE_NAME, e.winerror)
                self._listen = None
                self._stop.set()
                return False
            raise

    def _serve_one(self) -> None:
        """Serve one connection on the listening instance, then hand the name to the next one.

        Stage 9 (R2, F-54): the next instance is created BEFORE the served one is closed, so from
        the first bind to the stop the name is never free for another account to take. It is not
        created before the request is handled on purpose: a client that arrives meanwhile waits in
        WaitNamedPipe and sends only when this server can read it, so the deadline the service
        counts from the read still matches the budget the client sent (§2.1)."""
        if getattr(self, "_listen", None) is None and not self._bind():
            return
        handle = self._listen
        try:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except pywintypes.error as e:
                if e.winerror == winerror.ERROR_NO_DATA:
                    # F-59: a client connected and closed before we got here (a client that failed
                    # its own pre-write check, the stop self-connect) -- nothing to serve.
                    log.debug("client came and went before the connect (ERROR_NO_DATA)")
                    return
                # ERROR_PIPE_CONNECTED: a client connected between Create and Connect -> fine.
                if e.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            # We may have been woken by stop()'s self-connect (Ctrl+C / shutdown) rather than a real
            # request -> just return so the loop re-checks _stop.
            if self._stop.is_set():
                return
            self._serve_connection(handle)
        finally:
            nxt = None
            if not self._stop.is_set():
                try:
                    nxt = self._create_instance(first=False)
                except pywintypes.error as e:
                    log.error("could not pre-create the next pipe instance (winerror=%d); "
                              "re-binding after close", e.winerror)
            try:
                win32pipe.DisconnectNamedPipe(handle)
            except pywintypes.error:
                pass
            win32file.CloseHandle(handle)
            self._listen = nxt
            if nxt is None and not self._stop.is_set():
                self._bind()

    def _close_listen(self) -> None:
        """Release the listening instance (stop, selftests)."""
        h, self._listen = getattr(self, "_listen", None), None
        if h is not None:
            try:
                win32file.CloseHandle(h)
            except pywintypes.error:
                pass

    def _serve_connection(self, handle) -> None:
        """Read one request, answer it, settle the grant bookkeeping, wait for the client to close."""
        try:
            _hr, data = win32file.ReadFile(handle, 65536)
        except pywintypes.error as e:
            # A wake-up connection that closed immediately, or a client that vanished -> no request
            # to handle; return cleanly rather than logging a pipe error. Such a client is the
            # signature of one that bailed after its own pre-write checks (e.g. the CP's server-SID
            # verification): the request never left the client.
            if e.winerror in (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_PIPE_NOT_CONNECTED,
                              winerror.ERROR_NO_DATA):
                log.info("connection closed before request (winerror=%d)", e.winerror)
                return
            raise
        if not data:
            return
        self._req_started = time.monotonic()      # F-19: the deadlines count from here
        self._client_budget_s = None
        self._pending_grant = None
        self._expire_report_slot()
        # Stage 8b (F-42): a body that was not JSON, or JSON that was not an object, answers
        # bad-request; "cmd" is logged bounded.
        try:
            req = json.loads(data.decode("utf-8"))
        except Exception:
            req = None
        if not isinstance(req, dict):
            log.info("request rejected: not a JSON object (%d bytes)", len(data))
            resp = {"ok": False, "reason": "bad-request"}
        else:
            cmd = req.get("cmd")
            (log.debug if cmd in ROUTINE_COMMANDS else log.info)(
                "request cmd=%s", repr(cmd)[:LOG_CMD_MAX])
            try:
                resp = self._handle(req, handle)
            except Exception:
                # Stage 9 (F-66): the exception text (paths, profile names) never goes out; the
                # unlock paths also leave an audit record of the fault.
                log.exception("handler error")
                if cmd in ("unlock", "unlock_gesture"):
                    try:
                        self._audit.write(cmd, {"outcome": "internal-error"})
                    except Exception:
                        pass
                self._pending_grant = None
                resp = {"ok": False, "reason": "internal-error"}
        # Stage 9 (§2.1 / R3): every reply names the protocol and the UI language the tile should
        # use. Additive keys: older Python clients ignore them.
        resp.setdefault("v", PROTOCOL_VERSION)
        resp.setdefault("lang", self._reply_lang())
        delivered = False
        try:
            win32file.WriteFile(handle, (json.dumps(resp) + "\n").encode("utf-8"))
            delivered = True
        except pywintypes.error as e:
            # Stage 8b (F-43): a client that gave up before the reply is a benign timeout: INFO.
            if e.winerror in (winerror.ERROR_NO_DATA, winerror.ERROR_BROKEN_PIPE,
                              winerror.ERROR_PIPE_NOT_CONNECTED):
                log.info("client left before the reply was written (winerror=%d)", e.winerror)
            else:
                log.warning("reply write failed (winerror=%d)", e.winerror)
        finally:
            self._finish_grant(delivered)
        if not delivered:
            return
        # Wait (bounded) for the client to finish reading and close before we discard the pipe
        # (fixes 233). Stage 9 (F-58): no FlushFileBuffers before it -- that call blocks, with no
        # bound, until the client reads, so a client that never read wedged the sequential server.
        self._drain_until_client_closes(handle)

    def _reply_lang(self) -> str:
        """The tile's language: the UI language the user chose, reduced to what the CP carries
        (en, ru); anything else is English (R3 / R16)."""
        lang = getattr(self.cfg, "language", "en")
        return lang if lang in ("en", "ru") else "en"

    def _warmup(self) -> None:
        """Load enrollment, preload heavy models so the first real call is fast."""
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
            enroll_imgs = list(ENROLL_DIR.glob("*.jpg")) + list(ENROLL_DIR.glob("*.png"))
            if enroll_imgs:
                img = imio.imread(enroll_imgs[0])     # Stage 9 (R8): Unicode-safe
                if img is not None:
                    t0 = time.time()
                    self.recog.verify_frame(img)
                    log.info("model warmup ok in %.2fs", time.time() - t0)
            else:
                # Stage 9 (F-190): no enrollment yet -- the first run. Build the engine anyway (its
                # own black-frame warmup included), so the wizard's first Build does not pay the
                # whole engine load inside its pipe call.
                t0 = time.time()
                self.recog._lazy_app()
                log.info("engine loaded without an enrollment in %.2fs", time.time() - t0)
        except Exception as e:
            # Stage 8b (D-07): with the traceback -- "failed: <text>" alone never told a missing
            # enrollment from a broken model.
            log.warning("model warmup failed: %s", e, exc_info=True)

        # Also warm up the camera (open + close if not persistent). If the device is busy at
        # startup, skip gracefully -- the first real request will retry the bounded open.
        try:
            with self._cam_lock:
                cam, busy = self._acquire_camera()
                if busy:
                    log.info("camera warmup skipped: %s", busy)
                else:
                    # close in a finally: a raising read() must not skip it, or the
                    # non-persistent path leaks the handle for the process lifetime.
                    try:
                        # Stage 8b (D-07): "ok" only when a frame actually arrived. It used to be
                        # logged whatever read() returned, so a blind camera looked healthy here.
                        if cam.read() is None:
                            log.warning("camera warmup: opened, but no frame arrived "
                                        "(persistent=%s)", self.cfg.persistent_camera)
                        else:
                            log.info("camera warmup ok (persistent=%s)",
                                     self.cfg.persistent_camera)
                    finally:
                        self._done_with(cam)
        except Exception as e:
            log.warning("camera warmup failed: %s", e)

    # ---------- warm camera on lock (Stage 9, act 9b R10) ----------

    def _lock_watch_step(self, locked: "bool | None", was_locked: bool) -> bool:
        """One poll of the lock watcher; returns the new ``was_locked``. On the lock edge the camera
        is opened and held warm (at most CAMERA_WARM_HOLD_S); on the unlock edge -- or when the
        hold runs out -- it is released unless persistent_camera keeps it anyway. ``locked=None``
        (the probe has no opinion) changes nothing."""
        if locked is None:
            return was_locked
        now = time.monotonic()
        if locked and not was_locked:
            if (self._refusal() is None and not self._camera_leased_out()
                    and not self.cfg.persistent_camera):
                self._warm_until = now + CAMERA_WARM_HOLD_S
                if self._cam_lock.acquire(timeout=0.5):
                    try:
                        cam, busy = self._acquire_camera()
                        log.info("session locked: camera %s", "kept warm" if not busy
                                 else f"not warmed ({busy})")
                    except Exception:
                        log.exception("warming the camera failed")
                    finally:
                        self._cam_lock.release()
            return True
        if self._warm_until and (not locked or now >= self._warm_until):
            self._warm_until = 0.0
            if not self.cfg.persistent_camera:
                with self._cam_lock:
                    if self._cam is not None:
                        self._release_camera()
                        log.info("camera released (%s)", "session unlocked" if not locked
                                 else "warm hold expired")
        return bool(locked)

    def _lock_watch(self) -> None:
        was = False
        while not self._stop.wait(CAMERA_WARM_POLL_S):
            try:
                probe = self._session_locked
                was = self._lock_watch_step(probe() if probe else None, was)
            except Exception:
                log.exception("lock watch step failed")

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

        log.info("FaceService starting; pipe=%s state=%s", PIPE_NAME, self._service_state())
        # Stage 9 (F-54): claim the name BEFORE the warmup -- model and camera loading take seconds,
        # and the old order left the name free for all of them. A client that connects meanwhile
        # simply waits for the first answer.
        bound = self._bind()
        if bound and self._refusal() is None:
            # Stage 9 (R6, D-40): the gallery -- and with it the adaptive ring -- loads at start
            # whatever warmup_on_start says; only the model/camera warmup stays optional.
            try:
                self.recog.load()
            except Exception as e:
                log.warning("enrollment load at start: %s", e)
            if self.cfg.warmup_on_start:
                self._warmup()
        if bound:
            from .session_state import session_locked_wts
            if self._session_locked is None:
                self._session_locked = session_locked_wts
            threading.Thread(target=self._lock_watch, name="lock-watch", daemon=True).start()

        while not self._stop.is_set():
            try:
                self._serve_one()
            except Exception:
                log.exception("pipe error")
                time.sleep(0.5)
        self._close_listen()

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

        Sets the loop flag, then SELF-CONNECTS to our own pipe (a throwaway
        client connect+close) so the blocking ConnectNamedPipe returns at once -- otherwise an idle
        server (e.g. on Ctrl+C) would not notice the stop until a real client happened to connect.
        Safe to call from any thread (the console-ctrl handler runs on a Windows-owned thread).
        """
        self._stop.set()
        self._wake_accept()

    def _wake_accept(self) -> None:
        """Briefly connect to our own pipe to return a _serve_one() blocked in ConnectNamedPipe."""
        try:
            h = win32file.CreateFile(
                PIPE_NAME, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING,
                0x00100000 | 0x00010000,   # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION (F-63)
                None,
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
    """service.log, rotated, plus stderr only when there is one (see face_service.logging_setup)."""
    from .logging_setup import setup_logging
    setup_logging(LOG_PATH)


def _boot_config(custody) -> Config:
    """The config the service starts on: the file when custody holds, else the built-in defaults
    (Stage 9, act 9b R9 / F-99 -- a directory that could not be secured is not read)."""
    if not custody.ok:
        log.error("data directory custody failed -- config.toml is NOT read; built-in defaults "
                  "apply and face functions are refused")
        return Config()
    return Config.load()


def main() -> None:
    # Stage 9 (F-107): the log file lives in the data directory, which is not healed yet. A data
    # directory that is a reparse point is never written through -- the log goes to %TEMP% then.
    if is_reparse(APP_DIR):
        from .logging_setup import setup_logging
        import tempfile
        setup_logging(Path(tempfile.gettempdir()) / "face-unlock-service.log")
        log.error("data directory %s is a reparse point -- logging to %%TEMP%%", APP_DIR)
    else:
        _setup_logging()
    # Stage 8b (F-01 / F-23): secure the data directory BEFORE anything in it is read -- the
    # config below included -- and before the pipe exists. Never raises; a failure is logged at
    # ERROR and turns into the custody refusal inside FaceService.
    custody = heal_data_dir(APP_DIR)
    # Stage 9 (act 9b R9, F-99): a data directory that could not be secured is not READ either --
    # the built-in defaults apply (a planted config could not weaken anything, and no dump is
    # written into it); every face function refuses anyway.
    cfg = _boot_config(custody)
    # Stage 8b (F-12): with the dump knob off, frames left over from an earlier diagnosis are
    # biometric data with no purpose -- remove them (dump-named files only, no reparse points).
    if not cfg.debug_dump_frames and custody.ok:
        purge_debug_frames(APP_DIR)
    svc = FaceService(cfg, custody=custody)
    try:
        svc.serve_forever()
    except KeyboardInterrupt:
        svc.stop()


if __name__ == "__main__":
    main()
