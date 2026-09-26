"""tools/gesture_round_selftest.py -- the two-phase gesture unlock (Stage 7-i, reworked in Stage 9 R4/R5).

No service process, no camera, no pipe. Reproducible checks:
  [1] phase-1 discriminator: NEEDS_GESTURE answers "needs-gesture" with a two-movement sequence
      (two DIFFERENT kinds from turn_left / turn_right / nod -- never blink), a localized prompt
      naming both steps in order, a token and ttl_s; PASS / NOT_LIVE / no verdict keep their old
      replies (fail closed).
  [2] token slot: one-shot, TTL-bounded, overwritten by a newer phase 1, not cleared by a wrong or
      hostile token.
  [3] unlock_gesture gate order: SYSTEM -> refusals/lockout -> token -> camera.
  [4] the REAL round over canned frames: identity binding (non-matching and screen-flagged frames
      are not fed); both steps in order pass; the still start (motion-before-prompt); the order
      check (gesture-order); a single step is not enough; the calibrated turn sign.
  [5] R5 strike rules: a failed round on frames with a face strikes; motion-before-prompt and
      screen-suspected strike; no face at all, no frames, an engine refusal, a busy camera and a
      request deadline do NOT. B14 N-13: >= SCREEN_DOUBT_FRAC screen-flagged face frames fail a
      round even when the head did everything right (only with anti_screen on and enough light).
  [6] B14 N-15: the round's clock starts at the FIRST frame -- a slow camera open no longer eats
      the user's window.
  [7] audit records and the phase-2 adaptation (F-47): a grant after a passed round offers the
      best identity frame to the adaptive gallery, never when a frame was screen-flagged.

Uses an isolated FACE_UNLOCK_HOME and FaceService.__new__. Time is never waited on: a clock shim
advances 0.1 s per camera read and jumps past every deadline when the canned frames run out.
Run:  python -m tools.gesture_round_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FACE_UNLOCK_HOME", tempfile.mkdtemp(prefix="faceunlock_gesture_"))

import numpy as np

from tools.testkit import deliver_and_report  # noqa: E402  (Stage 9: v2 report)
from face_service.config import Config
from face_service import service as svc
from face_service.service import FaceService, VerifyOutcome, GESTURE_TOKEN_TTL_S
from face_service.liveness import (GESTURE_BASELINE_FRAMES, PITCH_DOWN_DELTA, SCREEN_DOUBT_FRAC,
                                   STILLNESS_MAX_DEG, YAW_DELTA)
from face_service.recognizer import FrameAnalysis

FAILS: list[str] = []
KINDS = ("turn_left", "turn_right", "nod")


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


class _LockoutSpy:
    def __init__(self, remaining_s: float = 0.0):
        self.records: list[bool] = []
        self._remaining = remaining_s
        self.store_ok = True

    def remaining(self):
        return self._remaining

    def record(self, ok):
        self.records.append(ok)

    def status(self):
        return {}


class _AuditStub:
    def __init__(self):
        self.records: list[tuple[str, dict]] = []

    def write(self, ev, rec):
        self.records.append((ev, rec))

    def last(self, ev):
        return next((r for e, r in reversed(self.records) if e == ev), None)


def _svc(cfg=None, remaining_s: float = 0.0):
    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: "S-1-5-18"   # the lock screen (SYSTEM)
    s.cfg = cfg if cfg is not None else Config()
    s._lockout = _LockoutSpy(remaining_s)
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s._gesture_slot = None
    s._cam_lock = threading.Lock()
    return s


def _outcome(verdict, match=False, distance=0.086, real=True):
    detail = {"verdict": verdict} if verdict is not None else {}
    return VerifyOutcome(match, distance, real, detail, None, None)


def _needs_gesture_svc(cfg=None):
    s = _svc(cfg)
    s._capture_and_verify = lambda: _outcome("NEEDS_GESTURE")
    return s


def _round_result(passed=True, identity_frames=4, faces=6, reason=None, flagged=0, checked=6,
                  luma=90.0):
    """What _run_challenge returns for a round that ran (for tests of unlock_gesture's logic)."""
    def runner(k, *, identity=True):
        return {"ok": True, "challenge": k, "prompt": "x", "passed": passed,
                "state": "passed" if passed else "failed", "reason": reason,
                "identity_frames": identity_frames, "distance_best": 0.07, "faces": faces,
                "frames_ok": faces, "screen_flagged": flagged, "screen_checked": checked,
                "scene_luma": luma, "fps": 20.0, "steps_done": 2 if passed else 0,
                "_embedding": np.ones(512, np.float32) / np.sqrt(512)}
    return runner


# --- [1] ------------------------------------------------------------------------------------

def test_phase1():
    print("[1] phase-1 discriminator: a two-movement sequence")
    seen = set()
    for _ in range(40):
        s = _needs_gesture_svc()
        r = s._handle({"cmd": "unlock", "v": 2}, None)
        parts = tuple(r.get("gesture", "").split(","))
        seen.add(parts)
        if not (len(parts) == 2 and parts[0] != parts[1] and all(p in KINDS for p in parts)):
            check("gesture is two different movements from turn_left/turn_right/nod", False, parts)
            break
    else:
        check("gesture is two different movements from turn_left/turn_right/nod (40 draws)", True)
    check("blink is never a phase-2 step", not any("blink" in p for p in seen), seen)
    check("the order is random (several different sequences in 40 draws)", len(seen) >= 3, seen)
    s = _needs_gesture_svc()
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    tok = r.get("token", "")
    check("token is 32 hex chars", len(tok) == 32 and all(c in "0123456789abcdef" for c in tok), tok)
    check("ttl_s == GESTURE_TOKEN_TTL_S", r.get("ttl_s") == GESTURE_TOKEN_TTL_S, r.get("ttl_s"))
    check("no password leaked; ok False", "password" not in r and r.get("ok") is False, sorted(r))

    s = _needs_gesture_svc()
    s._issue_gesture_token = lambda: ("turn_left,nod", s._prompt_for("turn_left,nod"), "ab" * 16)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("the prompt names both steps in order (EN)",
          r.get("prompt") == "Turn your head left, then nod your head", r.get("prompt"))
    ru = _svc(Config(language="ru"))
    check("... and in Russian",
          ru._prompt_for("nod,turn_right") == "Кивни головой, затем поверни голову вправо",
          ru._prompt_for("nod,turn_right"))
    xx = _svc(Config(language="ja"))
    check("an unlisted language falls back to English",
          xx._prompt_for("turn_right,turn_left") == "Turn your head right, then turn your head left",
          xx._prompt_for("turn_right,turn_left"))

    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
    p = _svc()
    p._capture_and_verify = lambda: _outcome("PASS", match=True, distance=0.05)
    rp = p._handle({"cmd": "unlock", "v": 2}, None)
    check("PASS -> credentials, nothing recorded before the report",
          rp.get("ok") is True and p._lockout.records == [], rp)
    deliver_and_report(p)
    check("PASS -> record(True) once reported", p._lockout.records == [True], p._lockout.records)
    n = _svc()
    n._capture_and_verify = lambda: _outcome("NOT_LIVE", distance=0.9)
    rn = n._handle({"cmd": "unlock", "v": 2}, None)
    check("NOT_LIVE -> no-match + strike, no token",
          rn.get("reason") == "no-match" and n._lockout.records == [False] and n._gesture_slot is None, rn)
    m = _svc()
    m._capture_and_verify = lambda: _outcome(None)
    check("missing verdict -> fail closed to no-match",
          m._handle({"cmd": "unlock", "v": 2}, None).get("reason") == "no-match")


# --- [2] ------------------------------------------------------------------------------------

def test_token_slot():
    print("[2] one-shot token slot")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
    s = _needs_gesture_svc()
    tok = s._handle({"cmd": "unlock", "v": 2}, None)["token"]
    s._run_challenge = _round_result()
    r1 = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
    check("valid token -> credentials", r1.get("ok") is True, r1)
    r2 = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
    check("replay -> gesture-token-invalid", r2.get("reason") == "gesture-token-invalid", r2)

    w = _needs_gesture_svc()
    tok_w = w._handle({"cmd": "unlock", "v": 2}, None)["token"]
    check("wrong token -> invalid",
          w._handle({"cmd": "unlock_gesture", "v": 2, "token": "f" * 32}, None).get("reason")
          == "gesture-token-invalid")
    w._run_challenge = _round_result()
    check("the real token still works after a wrong one",
          w._handle({"cmd": "unlock_gesture", "v": 2, "token": tok_w}, None).get("ok") is True)

    e = _needs_gesture_svc()
    tok_e = e._handle({"cmd": "unlock", "v": 2}, None)["token"]
    e._gesture_slot["expires"] = time.monotonic() - 1.0
    check("expired token -> invalid, slot dropped",
          e._handle({"cmd": "unlock_gesture", "v": 2, "token": tok_e}, None).get("reason")
          == "gesture-token-invalid" and e._gesture_slot is None)

    o = _needs_gesture_svc()
    tok_a = o._handle({"cmd": "unlock", "v": 2}, None)["token"]
    tok_b = o._handle({"cmd": "unlock", "v": 2}, None)["token"]
    check("a newer phase 1 kills the older token", tok_a != tok_b and o._handle(
        {"cmd": "unlock_gesture", "v": 2, "token": tok_a}, None).get("reason") == "gesture-token-invalid")

    h = _needs_gesture_svc()
    tok_h = h._handle({"cmd": "unlock", "v": 2}, None)["token"]
    for label, bad in (("non-ASCII", "é" * 32), ("non-string", 12345), ("list", ["a"]),
                       ("None", None), ("empty", "")):
        rh = h._handle({"cmd": "unlock_gesture", "v": 2, "token": bad}, None)
        check(f"hostile token ({label}) -> clean invalid",
              rh.get("reason") == "gesture-token-invalid", rh)
    h._run_challenge = _round_result()
    check("the real token still works after hostile ones",
          h._handle({"cmd": "unlock_gesture", "v": 2, "token": tok_h}, None).get("ok") is True)


# --- [3] ------------------------------------------------------------------------------------

def test_gate_order():
    print("[3] unlock_gesture gate order")
    ran = {"n": 0}

    def spy(k, *, identity=True):
        ran["n"] += 1
        return _round_result()(k)

    g = _needs_gesture_svc()
    tok = g._handle({"cmd": "unlock", "v": 2}, None)["token"]
    g._caller_sid = lambda h: "S-1-5-21-1-2-3-1001"
    g._run_challenge = spy
    before = len(g._audit.records)
    rg = g._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
    check("non-SYSTEM caller -> not-authorized, before the round, token kept, no audit",
          rg == {"ok": False, "reason": "not-authorized"} and ran["n"] == 0
          and g._gesture_slot is not None and len(g._audit.records) == before, rg)
    l = _needs_gesture_svc()
    tok_l = l._handle({"cmd": "unlock", "v": 2}, None)["token"]
    l._lockout._remaining = 42.5
    l._run_challenge = spy
    rl = l._handle({"cmd": "unlock_gesture", "v": 2, "token": tok_l}, None)
    check("lockout -> locked-out + retry_after_s, before the round, token kept",
          rl.get("reason") == "locked-out" and rl.get("retry_after_s") == 42.5 and ran["n"] == 0
          and l._gesture_slot is not None, rl)
    t = _svc()
    t._run_challenge = spy
    t._handle({"cmd": "unlock_gesture", "v": 2, "token": "b" * 32}, None)
    check("a bad token never opens the camera", ran["n"] == 0)


# --- [4] the real round -------------------------------------------------------------------

_WARMUP = object()
_BASE = (-13.0, -3.0)
# A turn to the user's OWN left reads as a POSITIVE yaw deviation on the reference camera
# (live calibration 2026-07-27) -- written out literally, never derived from the constant.
_LEFT = (-13.0, -3.0 + (YAW_DELTA + 17.0))
_RIGHT = (-13.0, -3.0 - (YAW_DELTA + 17.0))
_NOD = (-13.0 - (PITCH_DOWN_DELTA + 6.0), -3.0)


def _f(is_match: bool, pose, screen=False, face=True):
    if not face:
        return FrameAnalysis(False, False, 1.0, None, None, None, None)
    return FrameAnalysis(True, is_match, 0.07 if is_match else 0.9, screen, None,
                         np.asarray(pose + (0.0,), dtype=np.float32),
                         np.ones(512, np.float32) / np.sqrt(512) if is_match else None)


def _hold(pose, n=GESTURE_BASELINE_FRAMES + 2, **kw):
    return [_f(True, pose, **kw) for _ in range(n)]


class _StubCam:
    def __init__(self, frames, delay_reads=0):
        self._frames = list(frames)
        self._delay = delay_reads          # None reads before the first frame (a cold camera)
        self.reads = 0
        self.drained = False

    def read(self):
        self.reads += 1
        if self._delay > 0:
            self._delay -= 1
            return None
        if self._frames:
            return self._frames.pop(0)
        self.drained = True
        return None

    def close(self):
        pass


class _StubRecog:
    def analyze_frame(self, frame):
        return frame


class _Clock:
    """The service module's `time` during one round: 0.1 s per camera read, and past every
    deadline once the canned frames run out."""
    def __init__(self, cam):
        self._cam = cam
        self._base = time.monotonic()

    def monotonic(self):
        return self._base + 0.1 * self._cam.reads + (1e6 if self._cam.drained else 0.0)

    def time(self):
        return time.time()

    def sleep(self, _s):
        pass

    def strftime(self, *a):
        return time.strftime(*a)


def _round(kinds, frames, cfg=None, delay_reads=0, left_sign=None):
    s = _svc(cfg or Config(persistent_camera=False))
    # with a slow start the two drain reads fall into the delay, so no warm-up frames then
    cam = _StubCam(([] if delay_reads else [_WARMUP, _WARMUP]) + list(frames), delay_reads)
    s.recog = _StubRecog()
    s._acquire_camera = lambda: (cam, False)
    s._camera_leased_out = lambda: False
    s._note_camera_health = lambda *a: False
    s._calibration = ({"cameras": {s._camera_id(): {"left_is_negative_yaw": left_sign < 0}}}
                      if left_sign is not None else {})
    real = svc.time
    svc.time = _Clock(cam)
    try:
        return s._run_challenge(kinds, identity=True)
    finally:
        svc.time = real


def _scene(frames):
    """Frames are FrameAnalysis objects here; scene_luma needs an image -- give it one."""
    return frames


def test_round():
    print("[4] the real round over canned frames")
    orig_luma = svc.scene_luma
    svc.scene_luma = lambda frame: 90.0
    try:
        good = _round("turn_left,nod", _hold(_BASE) + _hold(_LEFT, 2) + _hold(_LEFT, 3) + _hold(_NOD, 2))
        check("both steps in order -> passed", good.get("passed") is True, good)
        check("identity frames and faces are counted", good.get("identity_frames") >= 8
              and good.get("faces") == good.get("identity_frames"), good)
        check("the best identity embedding is kept for adaptation (never sent)",
              good.get("_embedding") is not None)

        one = _round("turn_left,nod", _hold(_BASE) + _hold(_LEFT, 3))
        check("only the first step -> not passed (the round needs both)", one.get("passed") is False, one)
        check("... it fails on the second step's timeout", one.get("reason") in
              ("gesture-timeout", "round-timeout"), one.get("reason"))

        wrong = _round("turn_left,nod", _hold(_BASE) + _hold(_NOD, 3) + _hold(_LEFT, 3))
        check("the second step first -> gesture-order", wrong.get("reason") == "gesture-order", wrong)

        jump = [_f(True, _BASE), _f(True, (_BASE[0], _BASE[1] + STILLNESS_MAX_DEG + 3.0))] \
            + _hold(_LEFT, 3) + _hold(_NOD, 3)
        mv = _round("turn_left,nod", jump)
        check("motion in the first 0.4 s -> motion-before-prompt",
              mv.get("reason") == "motion-before-prompt", mv)

        imp = _round("turn_left,nod", [_f(False, p) for p in [_BASE] * 5 + [_LEFT] * 3 + [_NOD] * 3])
        check("a non-matching face never drives the round",
              imp.get("passed") is False and imp.get("identity_frames") == 0 and imp.get("faces") == 11, imp)

        mixed = []
        for p in [_BASE] * 5 + [_LEFT] * 3 + [_NOD] * 3:
            mixed += [_f(False, _LEFT), _f(True, p)]
        mx = _round("turn_left,nod", mixed)
        check("impostor frames interleaved as noise do not stop the owner", mx.get("passed") is True, mx)

        scr = _round("turn_left,nod", _hold(_BASE, screen=True) + _hold(_LEFT, 3, screen=True)
                     + _hold(_NOD, 3, screen=True))
        check("screen-flagged frames are not fed (F-11)",
              scr.get("passed") is False and scr.get("screen_flagged") == scr.get("faces"), scr)

        # R6 calibration: on a mirroring camera the user's left turn reads as a NEGATIVE yaw
        mirrored = _hold(_BASE) + _hold(_RIGHT, 3) + _hold(_NOD, 3)
        m0 = _round("turn_left,nod", mirrored)
        m1 = _round("turn_left,nod", mirrored, left_sign=-1.0)
        check("mirrored camera without calibration: the left turn is not recognised",
              m0.get("passed") is False, m0)
        check("with the calibrated sign (N-14) the same frames pass", m1.get("passed") is True, m1)

        cold = _round("turn_left,nod", _hold(_BASE) + _hold(_LEFT, 3) + _hold(_NOD, 3), delay_reads=40)
        check("N-15: 4 s of camera start-up before the first frame, the round still has its full "
              "window", cold.get("passed") is True, cold)

        none = _round("turn_left,nod", [], delay_reads=0)
        check("no frame at all -> the round reports no-frames", none == {"ok": False, "reason": "no-frames"}, none)

        empty = _round("turn_left,nod", [_f(False, _BASE, face=False)] * 8)
        check("frames without a face -> faces == 0", empty.get("faces") == 0, empty)
        check("bad sequence -> bad-request", _round("blink,nod", []) == {"ok": False, "reason": "bad-request"})
    finally:
        svc.scene_luma = orig_luma


# --- [5] strike rules ------------------------------------------------------------------------

def _armed(runner, cfg=None):
    s = _needs_gesture_svc(cfg)
    tok = s._handle({"cmd": "unlock", "v": 2}, None)["token"]
    s._run_challenge = runner
    return s, tok


def test_strikes():
    print("[5] R5 strike rules")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
    cases = (
        ("failed round on face frames", _round_result(passed=False, identity_frames=5), "gesture-failed", [False]),
        ("too few identity frames", _round_result(passed=True, identity_frames=1), "gesture-failed", [False]),
        ("motion-before-prompt", _round_result(passed=False, reason="motion-before-prompt"),
         "motion-before-prompt", [False]),
        ("no face in the whole round", _round_result(passed=False, identity_frames=0, faces=0, checked=0),
         "no-face", []),
    )
    for label, runner, want, strikes in cases:
        s, tok = _armed(runner)
        r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
        check(f"{label} -> {want}, strikes {strikes}",
              r.get("reason") == want and s._lockout.records == strikes, (r, s._lockout.records))
    for raw in ("camera-busy", "no-frames", "no-enrollment", "engine-error", "deadline-exceeded"):
        s, tok = _armed(lambda k, *, identity=True, raw=raw: {"ok": False, "reason": raw})
        r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
        check(f"round cut off by {raw} -> {raw}, NO strike (R5)",
              r == {"ok": False, "reason": raw} and s._lockout.records == [], (r, s._lockout.records))

    # N-13: screen fraction over the face frames
    n = 6
    at = int(np.ceil(SCREEN_DOUBT_FRAC * n))
    for flagged, want in ((0, True), (at - 1, True), (at, False)):
        s, tok = _armed(_round_result(passed=True, identity_frames=4, faces=n, flagged=flagged, checked=n))
        r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
        ok = r.get("ok") is True
        check(f"N-13: {flagged}/{n} screen-flagged -> {'grant' if want else 'screen-suspected'}",
              ok == want and (want or (r.get("reason") == "screen-suspected"
                                       and s._lockout.records == [False])), (r, s._lockout.records))
    s, tok = _armed(_round_result(passed=True, flagged=n, checked=n, luma=20.0))
    check("N-13: in the dark (sceneL below the floor) the screen layer stays out",
          s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None).get("ok") is True)
    s, tok = _armed(_round_result(passed=True, flagged=n, checked=n), Config(anti_screen=False))
    check("N-13: with anti_screen off the screen layer stays out",
          s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None).get("ok") is True)

    # phase 1: R5 -- a burst with no face at all is no attempt
    f = _svc()
    f._capture_and_verify = lambda: VerifyOutcome(False, 1.0, False,
                                                  {"verdict": "NOT_LIVE", "frames_ok": 5,
                                                   "engine_errors": 0, "faces": 0}, None, None)
    r = f._handle({"cmd": "unlock", "v": 2}, None)
    check("phase 1 with no face in any frame -> no-face, no strike",
          r.get("reason") == "no-face" and f._lockout.records == [], (r, f._lockout.records))
    g = _svc()
    g._capture_and_verify = lambda: VerifyOutcome(False, 0.9, True,
                                                  {"verdict": "NOT_LIVE", "frames_ok": 5,
                                                   "engine_errors": 0, "faces": 5}, None, None)
    r = g._handle({"cmd": "unlock", "v": 2}, None)
    check("phase 1 no-match on face frames -> strike",
          r.get("reason") == "no-match" and g._lockout.records == [False], (r, g._lockout.records))
    h = _svc()
    h._capture_and_verify = lambda: VerifyOutcome(False, 0.2, True,
                                                  {"verdict": "NOT_LIVE", "frames_ok": 5,
                                                   "engine_errors": 4, "faces": 1}, None, None)
    r = h._handle({"cmd": "unlock", "v": 2}, None)
    check("F-134: 4 of 5 frames engine faults, too few judged -> engine-error, no strike",
          r.get("reason") == "engine-error" and h._lockout.records == [], (r, h._lockout.records))


# --- [7] audit + adaptation ------------------------------------------------------------------

def test_audit_and_adaptation():
    print("[7] audit records and phase-2 adaptation (F-47)")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
    s, tok = _armed(_round_result())
    ev, rec = s._audit.records[-1]
    check("phase-1 audit: outcome needs-gesture, the sequence, never the token",
          ev == "unlock" and rec.get("outcome") == "needs-gesture" and "," in rec.get("gesture", "")
          and "token" not in rec and tok not in repr(rec), rec)
    offered = []
    s._maybe_adapt_gallery_embedding = lambda emb, d, gesture_passed, is_screen: offered.append(
        (emb is not None, gesture_passed, is_screen))
    r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": tok}, None)
    check("granted round -> credentials", r.get("ok") is True, r)
    check("nothing committed and nothing adapted before the report",
          s._lockout.records == [] and offered == [], (s._lockout.records, offered))
    deliver_and_report(s)
    check("after report_result ok: reset + the best identity frame offered with gesture_passed",
          s._lockout.records == [True] and offered == [(True, True, False)], (s._lockout.records, offered))
    rec = s._audit.last("unlock_gesture")
    check("audit: stable five-key record, reason granted",
          set(rec) == {"challenge", "passed", "identity_frames", "distance_best", "reason"}
          and rec["reason"] == "granted", rec)
    s2, tok2 = _armed(_round_result(flagged=1, checked=6))
    offered2 = []
    s2._maybe_adapt_gallery_embedding = lambda emb, d, gesture_passed, is_screen: offered2.append(is_screen)
    s2._handle({"cmd": "unlock_gesture", "v": 2, "token": tok2}, None)
    deliver_and_report(s2)
    check("a round with any screen-flagged frame is offered as is_screen=True (never adapts)",
          offered2 == [True], offered2)
    tele = s2._audit.last("gesture_telemetry")
    check("R6: per-round telemetry recorded (faces, fps, screen, sceneL; no image data)",
          tele is not None and tele.get("fps") == 20.0 and tele.get("screen_flagged") == 1, tele)


def main() -> int:
    test_phase1()
    test_token_slot()
    test_gate_order()
    test_round()
    test_strikes()
    test_audit_and_adaptation()
    if FAILS:
        print(f"\nGESTURE-ROUND SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nGESTURE-ROUND SELFTEST OK: two-movement sequence with a still start and a checked order; "
          "identity binding; R5 strike rules; screen layer; round clock from the first frame.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
