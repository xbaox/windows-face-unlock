"""tools/service_hardening_selftest.py -- Stage 8b package D proof (no camera, no engine).

  [1]  F-11  identity round: a screen-flagged matching frame is dropped like a non-matching one.
  [2]  F-15  validate rejects NaN / inf / non-int by declared type and accepts the live 8a config
             values; a broken lockout.json starts clean (never raises); reload is all-or-nothing.
  [3]  F-16  pause_camera: only a finite number, clamped to [5, 600] s, else bad-request.
  [4]  F-18  a burst with no frames, or one the engine could not judge, is lockout-neutral with an
             honest reason; a judged burst still strikes.
  [5]  F-19  past the deadline nothing is released and nothing recorded; a grant is recorded and
             audited only when the reply was delivered (real _serve_one on a private pipe name).
  [6]  F-20  (Stage 9: no pipe keys, no ratchet) pipe_io refuses an untrusted server without
             writing, and bounds a server that never answers.
  [7]  F-21  reset_lockout is gone.
  [8]  F-32  threshold ceiling 0.5 in validate and in the Settings spec.
  [9]  F-42 / F-43  a non-JSON or non-object body gets bad-request; a client that leaves before
             the reply is an INFO line, not a traceback.
  [10] F-44  a frame scene_luma cannot read does not abort the burst.
  [11] F-45  config / adaptive writes are write-then-rename (no .tmp left, content round-trips).
  [12] D-26  the validate() SID lookup is cached.

Private FACE_UNLOCK_HOME under %TEMP%, private pipe names; the real service and data are untouched.
Run:  python -m tools.service_hardening_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_ROOT = Path(tempfile.mkdtemp(prefix="faceunlock_svchard_"))
os.environ["FACE_UNLOCK_HOME"] = str(_ROOT / "home")

import numpy as np
import pywintypes    # type: ignore
import win32file     # type: ignore
import win32pipe     # type: ignore

from face_service import config as C
from face_service import pipe_io as P
from face_service import service as S
from face_service.adaptive import AdaptiveStore
from face_service.config import Config
from face_service.lockout import Lockout
from face_service.recognizer import FrameAnalysis
from face_service.service import FaceService, VerifyOutcome

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


class _LockoutSpy:
    def __init__(self, remaining_s=0.0):
        self.records: list = []
        self._remaining = remaining_s

    def remaining(self):
        return self._remaining

    def record(self, ok):
        self.records.append(ok)

    def status(self):
        return {}

    def reconfigure(self, *_a):
        pass


class _AuditStub:
    def __init__(self):
        self.records: list = []

    def write(self, ev, rec):
        self.records.append((ev, rec))

    def reconfigure(self, *_a):
        pass


def _svc(cfg=None):
    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: "S-1-5-18"   # Stage 9: stand in for the lock screen (SYSTEM)
    s.cfg = cfg or Config()
    s._lockout = _LockoutSpy()
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s._gesture_slot = None
    s._cam_lock = threading.Lock()
    s._cam = None
    s._stop = threading.Event()
    return s


# --- [1] F-11 -----------------------------------------------------------------------------------

def test_screen_in_round():
    print("[1] F-11 identity round drops screen-flagged frames")

    class _Cam:
        def __init__(self, n):
            self.n = n

        def read(self):
            if self.n <= 0:
                return None
            self.n -= 1
            return np.full((4, 4, 3), 100, np.uint8)   # lit, not black (a black round is no-frames)

        def close(self):
            pass

    fed = []

    class _Recog:
        def analyze_frame(self, _f):
            return FrameAnalysis(True, True, 0.05, True, np.zeros((106, 2)), (0.0, 0.0, 0.0))

    s = _svc(Config(persistent_camera=False))
    s.recog = _Recog()
    cam = _Cam(12)
    s._acquire_camera = lambda: (cam, False)
    s._note_camera_health = lambda *a: False
    from face_service import liveness as L
    orig, orig_cap = L.GestureSequence.feed, L.ROUND_CAP_S
    L.GestureSequence.feed = lambda self, lm, pose: fed.append(1)
    L.ROUND_CAP_S = 0.5            # keep the (real-clock) round short in this test
    try:
        r = s._run_challenge("turn_left,nod", identity=True)
    finally:
        L.GestureSequence.feed, L.ROUND_CAP_S = orig, orig_cap
    check("no screen-flagged frame reached the task", fed == [], len(fed))
    check("identity_frames counts none of them", r.get("identity_frames") == 0, r)


# --- [2] F-15 -----------------------------------------------------------------------------------

LIVE_8A_CONFIG = dict(   # the value types of the live config.toml as dumped in 8a (config.txt)
    threshold=0.32, camera_index=0, camera_warmup_frames=10, verify_frames=5, verify_required=2,
    presence_interval_s=60, presence_absent_strikes=2, presence_fullscreen_strikes=10,
    presence_soft_margin=0.05, presence_uncertain_streak=3, presence_confirm_delay_s=2.0,
    liveness_mode="paranoid", max_face_attempts=5, lockout_seconds=300,
    audit_max_mb=5.0, enroll_min_det_score=0.65, enroll_min_sharpness=80.0, enroll_luma_min=55.0,
    enroll_luma_max=210.0, enroll_min_frames=3, adaptive_gallery=True, adaptive_margin=0.17,
    adaptive_max_size=10, adaptive_cooldown_s=1800.0, low_light_luma_min=45.0,
    low_light_exposure_step=2.0, camera_open_retries=2, camera_open_timeout_s=3.0,
    camera_open_attempt_cap_s=5.0, camera_black_luma=2.0, camera_reopen_cooldown_s=30.0,
    watchdog_ping_timeout_s=2.0, watchdog_fail_threshold=3, watchdog_interval_s=30.0,
    watchdog_pause_ttl_s=300.0, language="en", auto_lock=False)


def _rejects(**kw) -> bool:
    try:
        Config(**kw).validate()
        return False
    except (ValueError, TypeError):
        return True


def test_validation():
    print("[2] F-15 validation, lockout.json, reload")
    try:
        Config(**LIVE_8A_CONFIG).validate()
        check("the live 8a config values still validate", True)
    except Exception as e:
        check("the live 8a config values still validate", False, repr(e))
    for name, bad in (("low_light_exposure_step", math.nan), ("presence_confirm_delay_s", math.inf),
                      ("presence_input_idle_s", math.nan), ("adaptive_cooldown_s", -math.inf),
                      ("audit_max_mb", "5"), ("threshold", True)):
        check(f"{name}={bad!r} rejected", _rejects(**{name: bad}))
    for name, bad in (("presence_interval_s", 60.0), ("lockout_seconds", 2.5),
                      ("max_face_attempts", True), ("verify_frames", "5")):
        check(f"int field {name}={bad!r} rejected", _rejects(**{name: bad}))
    check("an int in a float field is fine", not _rejects(threshold=0.3, presence_confirm_delay_s=2))

    path = _ROOT / "lockout.json"
    for label, body in (("JSON list", "[1, 2]"), ("null", "null"), ("text fails", '{"fails": "x"}'),
                        ("inf until", '{"fails": 0, "locked_until": 1e999}'),
                        ("negative", '{"fails": -3, "locked_until": 0}'), ("garbage", "\x00\x01")):
        path.write_text(body, encoding="utf-8")
        try:
            lo = Lockout(path, 5, 300)
            check(f"lockout.json {label}: starts clean, no raise",
                  lo.status()["fails"] == 0 and lo.remaining() == 0.0, lo.status())
        except Exception as e:
            check(f"lockout.json {label}: starts clean, no raise", False, repr(e))

    s = _svc(Config())
    s.recog = type("R", (), {"cfg": None})()
    old_cfg = s.cfg

    class _Boom(_LockoutSpy):
        def reconfigure(self, *_a):
            raise RuntimeError("simulated")

    s._lockout = _Boom()
    C.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    C.CONFIG_PATH.write_text('max_face_attempts = 7\n', encoding="utf-8")
    r = s._reload_config()
    check("reload failure reported", r.get("ok") is False and "reload-failed" in r.get("reason", ""),
          r)
    check("reload failure keeps the old cfg object", s.cfg is old_cfg)
    C.CONFIG_PATH.unlink()


# --- [3] F-16 -----------------------------------------------------------------------------------

def test_pause_camera():
    print("[3] F-16 pause_camera bounds")
    s = _svc()
    s._release_camera = lambda: None
    for bad in (math.inf, math.nan, "60", True, None, [1]):
        r = s._handle({"cmd": "pause_camera", "seconds": bad}, None)
        check(f"seconds={bad!r} -> bad-request", r == {"ok": False, "reason": "bad-request"}, r)
    for given, want in ((1e9, 600.0), (1, 5.0), (120, 120.0)):
        t0 = time.monotonic()
        s._handle({"cmd": "pause_camera", "seconds": given}, None)
        got = s._camera_paused_until - t0
        check(f"seconds={given} -> ~{want}s lease", abs(got - want) < 1.0, round(got, 2))


# --- [4] F-18 -----------------------------------------------------------------------------------

def test_fault_neutral():
    print("[4] F-18 device/engine faults are lockout-neutral")
    cases = (("no frames", {"verdict": "NOT_LIVE", "frames_ok": 0, "engine_errors": 0}, None,
              "no-frames"),
             ("engine, no gallery", {"verdict": "NOT_LIVE", "frames_ok": 5, "engine_errors": 5},
              None, "no-enrollment"),
             ("engine, gallery loaded", {"verdict": "NOT_LIVE", "frames_ok": 5, "engine_errors": 5},
              np.zeros((1, 512)), "engine-error"))
    for label, detail, refs, reason in cases:
        s = _svc()
        s.recog = type("R", (), {"_refs": refs})()
        s._capture_and_verify = lambda d=detail: VerifyOutcome(False, 1.0, False, d, None, None)
        r = s._handle({"cmd": "unlock", "v": 2}, None)
        check(f"{label} -> {reason}", r == {"ok": False, "reason": reason}, r)
        check(f"{label} -> no strike", s._lockout.records == [], s._lockout.records)
    s = _svc()
    s.recog = type("R", (), {"_refs": np.zeros((1, 512))})()
    s._capture_and_verify = lambda: VerifyOutcome(
        False, 0.9, True, {"verdict": "NOT_LIVE", "frames_ok": 5, "engine_errors": 2}, None, None)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("a judged burst still strikes", r.get("reason") == "no-match"
          and s._lockout.records == [False], (r, s._lockout.records))


# --- [5] F-19 + [9] F-42/F-43 through the real _serve_one ------------------------------------------

def _serve_on(s, name, n):
    """Run the REAL _serve_one n times on a private pipe name."""
    S.PIPE_NAME = name
    for _ in range(n):
        s._serve_one()
    s._close_listen()     # Stage 9: the next instance is pre-created; release it


def _client(name, payload: bytes, *, read=True, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        try:
            h = win32file.CreateFile(name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                     0, None, win32file.OPEN_EXISTING, 0, None)
            break
        except pywintypes.error:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    try:
        win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(h, payload)
        if not read:
            return None
        _hr, data = win32file.ReadFile(h, 65536)
        return json.loads(data.decode("utf-8"))
    finally:
        win32file.CloseHandle(h)


def test_deadline_and_delivery():
    print("[5] F-19 deadlines and delivery-gated grants; [9] F-42 / F-43 on the real _serve_one")
    released = []
    S.load_password = lambda: released.append(1) or {"u": "admin", "p": "pw", "d": "."}
    orig_name = S.PIPE_NAME
    try:
        # (a) past the deadline: nothing released, nothing recorded
        s = _svc()
        s._req_started = time.monotonic() - (S.UNLOCK_DEADLINE_S + 0.5)
        s._capture_and_verify = lambda: VerifyOutcome(True, 0.05, True, {"verdict": "PASS"})
        r = s._handle({"cmd": "unlock", "v": 2}, None)
        check("unlock past 11 s -> deadline-exceeded", r == {"ok": False, "reason":
                                                            "deadline-exceeded"}, r)
        check("nothing released", released == [], released)
        check("no reset, no strike", s._lockout.records == [], s._lockout.records)
        g = _svc()
        g._gesture_slot = {"token": "a" * 32, "kind": "turn_left,nod", "expires": 1e18}
        g._run_challenge = lambda k, *, identity=False: {
            "ok": True, "challenge": k, "prompt": "x", "passed": True, "state": "passed",
            "identity_frames": 5, "distance_best": 0.05, "faces": 5, "screen_checked": 5,
            "screen_flagged": 0, "scene_luma": 90.0}
        g._req_started = time.monotonic() - (S.UNLOCK_GESTURE_DEADLINE_S + 0.5)
        r = g._handle({"cmd": "unlock_gesture", "v": 2, "token": "a" * 32}, None)
        check("unlock_gesture past its deadline -> deadline-exceeded",
              r.get("reason") == "deadline-exceeded" and released == [] and g._lockout.records == [],
              (r, g._lockout.records))
        check("constants are 11.0 / 17.0 (below the CP's 12 / 18 s; Stage 9 R4 round 12.4 s)",
              (S.UNLOCK_DEADLINE_S, S.UNLOCK_GESTURE_DEADLINE_S) == (11.0, 17.0))
        # Stage 9 (F-61): the client's own budget caps the deadline too
        c = _svc()
        c._capture_and_verify = lambda: VerifyOutcome(True, 0.05, True, {"verdict": "PASS"})
        c._req_started = time.monotonic() - 3.0
        r = c._handle({"cmd": "unlock", "v": 2, "budget_ms": 3000}, None)
        check("budget_ms 3000 read 3 s ago -> deadline-exceeded (min(11 s, 3 - 0.5 s))",
              r.get("reason") == "deadline-exceeded", r)

        # (b) delivered grant through the real serve loop
        name = r"\\.\pipe\FaceUnlockSelftest-" + uuid.uuid4().hex
        s = _svc()
        s._capture_and_verify = lambda: VerifyOutcome(True, 0.05, True, {"verdict": "PASS"})
        s._maybe_adapt_gallery = lambda r: None
        th = threading.Thread(target=_serve_on, args=(s, name, 2), daemon=True)
        th.start()
        rep = _client(name, b'{"cmd":"unlock","v":2}')
        check("delivered grant: client got the credentials and a grant_id",
              rep.get("username") == "admin" and len(rep.get("grant_id", "")) == 32, rep)
        time.sleep(0.2)
        check("delivered grant: nothing committed before report_result (Stage 9, F-60)",
              s._lockout.records == [] and s._audit.records[-1][1].get("outcome") == "delivered",
              (s._lockout.records, s._audit.records[-1:]))
        rep2 = _client(name, json.dumps({"cmd": "report_result", "v": 2,
                                         "grant_id": rep.get("grant_id"), "ok": True}).encode())
        th.join(5)
        check("report_result ok -> acknowledged", rep2.get("ok") is True, rep2)
        check("reported grant: lockout reset recorded", s._lockout.records == [True],
              s._lockout.records)
        check("reported grant: audited granted",
              s._audit.records[-1][1].get("outcome") == "granted", s._audit.records[-1])

        # (c) client gone before the reply -> grant-undelivered, INFO not ERROR
        name = r"\\.\pipe\FaceUnlockSelftest-" + uuid.uuid4().hex
        s = _svc()

        def slow():
            time.sleep(0.6)
            return VerifyOutcome(True, 0.05, True, {"verdict": "PASS"})

        s._capture_and_verify = slow
        s._maybe_adapt_gallery = lambda r: None
        records: list = []

        class _H(logging.Handler):
            def emit(self, rec):
                records.append(rec)

        hdl = _H(level=logging.DEBUG)
        S.log.addHandler(hdl)
        try:
            th = threading.Thread(target=_serve_on, args=(s, name, 1), daemon=True)
            th.start()
            _client(name, b'{"cmd":"unlock","v":2}', read=False)
            th.join(5)
        finally:
            S.log.removeHandler(hdl)
        check("undelivered grant: no lockout reset", s._lockout.records == [], s._lockout.records)
        check("undelivered grant: audited grant-undelivered",
              s._audit.records and s._audit.records[-1][1].get("outcome") == "grant-undelivered",
              s._audit.records[-1:] if s._audit.records else None)
        errors = [r for r in records if r.levelno >= logging.ERROR]
        check("client gone: no ERROR / traceback logged (F-43)", errors == [],
              [r.getMessage() for r in errors])

        # (d) F-42 bad bodies
        name = r"\\.\pipe\FaceUnlockSelftest-" + uuid.uuid4().hex
        s = _svc()
        th = threading.Thread(target=_serve_on, args=(s, name, 3), daemon=True)
        th.start()
        for label, body in (("not JSON", b"hello"), ("JSON list", b"[1,2]"),
                            ("huge cmd", json.dumps({"cmd": "x" * 5000}).encode())):
            rep = _client(name, body)
            want = "unknown-command" if label == "huge cmd" else "bad-request"
            check(f"{label} -> {want} (+ v, lang)",
                  rep == {"ok": False, "reason": want, "v": 2, "lang": "en"}, rep)
        th.join(5)
    finally:
        S.PIPE_NAME = orig_name


# --- [6] F-20 -----------------------------------------------------------------------------------

def test_perimeter_clients():
    print("[6] client identity check + read deadline (Stage 9: no perimeter keys left)")
    check("Config carries none of the three pipe keys (R2)",
          not any(hasattr(Config(), k) for k in ("pipe_hardened_sd", "pipe_first_instance",
                                                  "pipe_unlock_require_system")))
    check("the ratchet is gone with them (F-104)", not hasattr(S, "POSTURE_KEYS")
          and not hasattr(S.FaceService, "_apply_posture_ratchet"))

    name = r"\\.\pipe\FaceUnlockSelftest-" + uuid.uuid4().hex
    got = []

    def server(reply: bool):
        h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX,
                                      win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                                      | win32pipe.PIPE_WAIT, 1, 65536, 65536, 0, None)
        try:
            win32pipe.ConnectNamedPipe(h, None)
            try:
                _hr, data = win32file.ReadFile(h, 65536)
                got.append(data)
                if reply:
                    win32file.WriteFile(h, b'{"ok": true, "pong": true}')
                else:
                    time.sleep(2.0)
            except pywintypes.error:
                pass
        finally:
            win32file.CloseHandle(h)

    orig = P.server_sid_string
    P.server_sid_string = lambda h: "S-1-5-21-1-2-3-4444"
    try:
        th = threading.Thread(target=server, args=(True,), daemon=True)
        th.start()
        resp, why = P.exchange({"cmd": "ping"}, 3.0, pipe_name=name)
        th.join(3)
    finally:
        P.server_sid_string = orig
    check("foreign server SID -> untrusted-server", resp is None and why == "untrusted-server", why)
    check("nothing was written to the untrusted server", got == [], got)

    name = r"\\.\pipe\FaceUnlockSelftest-" + uuid.uuid4().hex
    th = threading.Thread(target=server, args=(False,), daemon=True)
    th.start()
    t0 = time.monotonic()
    resp, why = P.exchange({"cmd": "ping"}, 0.8, pipe_name=name)
    dt = time.monotonic() - t0
    th.join(3)
    check("SELF server that never answers -> reply-timeout", why == "reply-timeout", why)
    check("... within the budget (< 1.5 s)", dt < 1.5, round(dt, 2))


# --- [7] F-21, [8] F-32 -------------------------------------------------------------------------

def test_small():
    print("[7] F-21 / [8] F-32")
    r = _svc()._handle({"cmd": "reset_lockout"}, None)
    check("reset_lockout -> unknown-command", r == {"ok": False, "reason": "unknown-command"}, r)
    check("threshold 0.5 accepted", not _rejects(threshold=0.5))
    check("threshold 0.51 rejected", _rejects(threshold=0.51))
    from presence_monitor.gui import SETTINGS_FIELDS
    spec = {f[0]: f[2] for f in SETTINGS_FIELDS}
    # Stage 9 (R12, F-155): Settings offers only 0.28-0.35 (validate() still allows up to 0.5).
    check("Settings offers threshold 0.28-0.35 only", (spec.get("threshold") or ())[:2] == (0.28, 0.35),
          spec.get("threshold"))


# --- [10] F-44 ----------------------------------------------------------------------------------

def test_scene_luma():
    print("[10] F-44 scene_luma inside the per-frame try")

    class _Cam:
        def __init__(self):
            self.n = 7

        def read(self):
            self.n -= 1
            return np.zeros((4, 4, 3), np.uint8) if self.n >= 0 else None

    class _Recog:
        def analyze_frame(self, _f):
            return FrameAnalysis(False, False, 1.0, None, None, None)

    s = _svc()
    s.recog = _Recog()
    orig = S.scene_luma
    S.scene_luma = lambda f: (_ for _ in ()).throw(ValueError("odd frame"))
    try:
        out = s._analyze_burst(_Cam())
        check("burst completed", out.detail.get("frames_ok") == s.cfg.verify_frames,
              out.detail.get("frames_ok"))
        check("luma simply missing", out.scene_luma is None, out.scene_luma)
    except Exception as e:
        check("burst completed", False, repr(e))
    finally:
        S.scene_luma = orig


# --- [11] F-45, [12] D-26 -----------------------------------------------------------------------

def test_atomic_and_cache():
    print("[11] F-45 write-then-rename / [12] D-26 SID cache")
    cfg = Config(**LIVE_8A_CONFIG)
    cfg.save()
    check("config.toml written, no .tmp left",
          C.CONFIG_PATH.exists() and not C.CONFIG_PATH.with_name("config.toml.tmp").exists())
    check("config.toml round-trips", Config.load(strict=True).liveness_mode == "paranoid")
    st = AdaptiveStore(_ROOT / "home" / "adaptive.npz")
    st.add(np.ones(512, np.float32), 1.0, 10)
    st.save()
    st2 = AdaptiveStore(_ROOT / "home" / "adaptive.npz")
    check("adaptive.npz round-trips, no .tmp left",
          st2.load() and st2.count == 1 and not (_ROOT / "home" / "adaptive.npz.tmp").exists())
    # Stage 9 (D-74): the one SID helper lives in face_service.identity; validate() no longer
    # needs it at all (the pipe_hardened_sd check went with the key, R2).
    from face_service import identity as I
    I.current_user_sid.cache_clear()
    I.current_user_sid()
    I.current_user_sid()
    info = I.current_user_sid.cache_info()
    check("identity.current_user_sid cached (1 miss, >=1 hit)", info.misses == 1 and info.hits >= 1,
          info)


def main() -> int:
    try:
        test_screen_in_round()
        test_validation()
        test_pause_camera()
        test_fault_neutral()
        test_deadline_and_delivery()
        test_perimeter_clients()
        test_small()
        test_scene_luma()
        test_atomic_and_cache()
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(_ROOT)], capture_output=True)
    if FAILS:
        print(f"\nSERVICE HARDENING SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nSERVICE HARDENING SELFTEST OK: screen frames cannot drive the identity round; "
          "validation / lockout.json / reload fail safe; pause_camera is bounded; device and engine "
          "faults cost no strike; nothing is released past the deadline and a grant counts only "
          "when delivered; clients verify the server and bound the read; reset_lockout is gone; "
          "threshold <= 0.5; bad bodies get bad-request; writes are atomic; the SID is cached.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
