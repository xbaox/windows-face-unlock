"""tools/gesture_round_selftest.py -- Stage 7-i gesture-round proof (no service, no camera, no pipe).

Reproducible checks for the two-phase gesture unlock:
  [1] phase-1 discriminator: a NEEDS_GESTURE verdict now answers reason "needs-gesture" with
      gesture/prompt/token/ttl_s, while PASS / NOT_LIVE / a verdict-less detail keep their old
      replies (fail-closed: no verdict => the OLD no-match path, never the gesture path).
  [2] token slot: one-shot (a burnt token cannot be replayed), TTL-bounded, overwritten by a
      newer phase 1, and NOT cleared by a wrong token.
  [3] unlock_gesture gate order: SYSTEM gate -> lockout -> token -> camera, each proven to
      short-circuit BEFORE the next (and before _release_credentials).
  [4] identity binding in _run_challenge(identity=True): non-matching frames are DROPPED, not
      fed -- a gesture performed by a face that does not match can never resolve the task;
      identity=False (the `challenge` command) keeps the unfiltered behaviour byte for byte.
  [5] strike semantics: needs-gesture adds NO strike, a failed round does, a granted round
      clears, an invalid token never touches the counter.
  [6] audit: "unlock" carries outcome/gesture (never the token); "unlock_gesture" writes a
      stable five-key record on every branch.

Uses an isolated FACE_UNLOCK_HOME and FaceService.__new__ (no heavy init) so it touches no real
state, no camera and no engine. Time is never actually waited on: the camera stub reports when it
has run dry and a clock shim jumps the wall-clock cap past its deadline at that moment.
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

from face_service.config import Config
from face_service import service as svc
from face_service.service import FaceService, VerifyOutcome, GESTURE_TOKEN_TTL_S
from face_service.liveness import GESTURE_BASELINE_FRAMES, PITCH_DOWN_DELTA, YAW_DELTA
from face_service.recognizer import FrameAnalysis

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


# --- stubs (project convention: bypass the heavy __init__ instead of mocking a whole service) ---

class _LockoutSpy:
    """Records every record() call so strike semantics are asserted directly."""

    def __init__(self, remaining_s: float = 0.0):
        self.records: list[bool] = []
        self._remaining = remaining_s

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


def _svc(cfg=None, remaining_s: float = 0.0):
    """Lightweight FaceService: real methods, no camera / model / pywin32 init."""
    s = FaceService.__new__(FaceService)
    s.cfg = cfg if cfg is not None else Config(pipe_unlock_require_system=False)
    s._lockout = _LockoutSpy(remaining_s)
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s._gesture_slot = None
    s._cam_lock = threading.Lock()
    return s


def _outcome(verdict, match=False, distance=0.086, real=True):
    """A VerifyOutcome as _analyze_burst would build it. scene_luma=None keeps the low-light
    boost AND the too-dark gate out of the way (both are skipped when no scene was measured)."""
    detail = {"verdict": verdict} if verdict is not None else {}
    return VerifyOutcome(match, distance, real, detail, None, None)


def _needs_gesture_svc(cfg=None):
    s = _svc(cfg)
    s._capture_and_verify = lambda: _outcome("NEEDS_GESTURE")
    return s


# --- [1] phase-1 discriminator ------------------------------------------------------------

def test_phase1_discriminator():
    print("[1] phase-1 discriminator")
    s = _needs_gesture_svc()
    r = s._handle({"cmd": "unlock"}, None)
    check("NEEDS_GESTURE -> reason 'needs-gesture'", r.get("reason") == "needs-gesture", r)
    check("gesture is one of the four kinds",
          r.get("gesture") in ("blink", "turn_left", "turn_right", "nod"), r.get("gesture"))
    tok = r.get("token", "")
    check("token is 32 hex chars",
          isinstance(tok, str) and len(tok) == 32 and all(c in "0123456789abcdef" for c in tok), tok)
    check("ttl_s == GESTURE_TOKEN_TTL_S", r.get("ttl_s") == GESTURE_TOKEN_TTL_S, r.get("ttl_s"))
    check("prompt is a non-empty string and not the raw key",
          isinstance(r.get("prompt"), str) and r["prompt"] and not r["prompt"].startswith("gesture."),
          r.get("prompt"))
    check("distance/real still present", r.get("distance") == 0.086 and r.get("real") is True, r)
    check("ok is False (this is not a grant)", r.get("ok") is False, r)
    check("no password field leaked", "password" not in r, sorted(r))

    ru = _needs_gesture_svc(Config(pipe_unlock_require_system=False, language="ru"))
    r_ru = ru._handle({"cmd": "unlock"}, None)
    check("ru language yields a Cyrillic prompt",
          any("Ѐ" <= c <= "ӿ" for c in r_ru.get("prompt", "")), r_ru.get("prompt"))

    xx = _needs_gesture_svc(Config(pipe_unlock_require_system=False, language="ja"))
    r_xx = xx._handle({"cmd": "unlock"}, None)
    check("unlisted language falls back to the English prompt",
          r_xx.get("prompt") in ("Blink now", "Turn your head left",
                                 "Turn your head right", "Nod your head"), r_xx.get("prompt"))


def test_phase1_other_verdicts():
    print("[1b] other verdicts keep their old replies")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}

    p = _svc()
    p._capture_and_verify = lambda: _outcome("PASS", match=True, distance=0.05)
    rp = p._handle({"cmd": "unlock"}, None)
    check("PASS -> credentials", rp.get("ok") is True and rp.get("username") == "admin", rp)
    check("PASS -> record(True)", p._lockout.records == [True], p._lockout.records)

    n = _svc()
    n._capture_and_verify = lambda: _outcome("NOT_LIVE", distance=0.9)
    rn = n._handle({"cmd": "unlock"}, None)
    check("NOT_LIVE -> reason 'no-match'", rn.get("reason") == "no-match", rn)
    check("NOT_LIVE -> record(False)", n._lockout.records == [False], n._lockout.records)
    check("NOT_LIVE arms no token", n._gesture_slot is None, n._gesture_slot)

    m = _svc()
    m._capture_and_verify = lambda: _outcome(None)
    rm = m._handle({"cmd": "unlock"}, None)
    check("missing verdict -> fail closed to 'no-match'", rm.get("reason") == "no-match", rm)
    check("missing verdict arms no token", m._gesture_slot is None, m._gesture_slot)


# --- [2] token slot -----------------------------------------------------------------------

def _pass_round(kind, identity_frames=2):
    return lambda k, *, identity=False: {
        "ok": True, "challenge": k, "prompt": "x", "passed": True, "state": "passed",
        "identity_frames": identity_frames, "distance_best": 0.07,
    }


def test_token_slot():
    print("[2] one-shot token slot")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}

    s = _needs_gesture_svc()
    tok = s._handle({"cmd": "unlock"}, None)["token"]
    s._run_challenge = _pass_round(s._gesture_slot["kind"] if s._gesture_slot else "blink")
    r1 = s._handle({"cmd": "unlock_gesture", "token": tok}, None)
    check("valid token -> credentials", r1.get("ok") is True and r1.get("username") == "admin", r1)
    r2 = s._handle({"cmd": "unlock_gesture", "token": tok}, None)
    check("replay of the same token -> gesture-token-invalid",
          r2.get("reason") == "gesture-token-invalid", r2)

    w = _needs_gesture_svc()
    tok_w = w._handle({"cmd": "unlock"}, None)["token"]
    rw = w._handle({"cmd": "unlock_gesture", "token": "f" * 32}, None)
    check("wrong token -> invalid", rw.get("reason") == "gesture-token-invalid", rw)
    check("wrong token does NOT burn the live slot", w._gesture_slot is not None)
    w._run_challenge = _pass_round("blink")
    check("the real token still works after a wrong one",
          w._handle({"cmd": "unlock_gesture", "token": tok_w}, None).get("ok") is True)

    e = _needs_gesture_svc()
    tok_e = e._handle({"cmd": "unlock"}, None)["token"]
    e._gesture_slot["expires"] = time.monotonic() - 1.0
    re_ = e._handle({"cmd": "unlock_gesture", "token": tok_e}, None)
    check("expired token -> invalid", re_.get("reason") == "gesture-token-invalid", re_)
    check("expired slot is dropped", e._gesture_slot is None)

    o = _needs_gesture_svc()
    tok_a = o._handle({"cmd": "unlock"}, None)["token"]
    tok_b = o._handle({"cmd": "unlock"}, None)["token"]
    check("a second phase 1 issues a different token", tok_a != tok_b)
    check("the older token is dead",
          o._handle({"cmd": "unlock_gesture", "token": tok_a}, None).get("reason")
          == "gesture-token-invalid")

    n = _svc()
    check("unlock_gesture with no armed slot -> invalid",
          n._handle({"cmd": "unlock_gesture", "token": "a" * 32}, None).get("reason")
          == "gesture-token-invalid")
    check("missing token field -> invalid",
          _svc()._handle({"cmd": "unlock_gesture"}, None).get("reason") == "gesture-token-invalid")

    # Hostile token shapes must be rejected cleanly, never raise out of the handler (which would
    # answer "exception: ..."). compare_digest refuses non-ASCII str outright.
    h = _needs_gesture_svc()
    tok_h = h._handle({"cmd": "unlock"}, None)["token"]
    for label, bad in (("non-ASCII", "é" * 32), ("non-string", 12345),
                       ("list", ["a"]), ("None", None), ("empty", "")):
        rh = h._handle({"cmd": "unlock_gesture", "token": bad}, None)
        check(f"hostile token ({label}) -> clean invalid",
              rh.get("reason") == "gesture-token-invalid", rh)
    check("hostile tokens did not burn the live slot", h._gesture_slot is not None)
    h._run_challenge = _pass_round("blink")
    check("the real token still works after hostile ones",
          h._handle({"cmd": "unlock_gesture", "token": tok_h}, None).get("ok") is True)


# --- [3] gate order -----------------------------------------------------------------------

def test_gate_order():
    print("[3] unlock_gesture gate order (SYSTEM -> lockout -> token -> camera)")
    ran = {"n": 0}

    def _spy(k, *, identity=False):
        ran["n"] += 1
        return {"ok": True, "challenge": k, "prompt": "x", "passed": True, "state": "passed",
                "identity_frames": 2, "distance_best": 0.07}

    # Arm a real token first (phase 1 is itself SYSTEM-gated), THEN turn the gate on: what is
    # under test is unlock_gesture's own gate, on a request that is otherwise perfectly valid.
    g = _needs_gesture_svc()
    tok = g._handle({"cmd": "unlock"}, None)["token"]
    g.cfg.pipe_unlock_require_system = True
    g._run_challenge = _spy
    audit_before = len(g._audit.records)
    rg = g._handle({"cmd": "unlock_gesture", "token": tok}, None)
    check("require_system=True + non-SYSTEM -> not-authorized",
          rg == {"ok": False, "reason": "not-authorized"}, rg)
    check("SYSTEM gate short-circuits before the round", ran["n"] == 0)
    check("SYSTEM gate does NOT burn the token", g._gesture_slot is not None)
    check("SYSTEM gate writes no audit record", len(g._audit.records) == audit_before)
    check("SYSTEM gate adds no strike", g._lockout.records == [])

    l = _needs_gesture_svc()
    tok_l = l._handle({"cmd": "unlock"}, None)["token"]
    l._lockout._remaining = 42.5
    l._run_challenge = _spy
    rl = l._handle({"cmd": "unlock_gesture", "token": tok_l}, None)
    check("lockout -> locked-out + retry_after_s",
          rl.get("reason") == "locked-out" and rl.get("retry_after_s") == 42.5, rl)
    check("lockout short-circuits before the round", ran["n"] == 0)
    check("lockout does NOT burn the token", l._gesture_slot is not None)
    check("lockout adds no strike", l._lockout.records == [])

    t = _svc()
    t._run_challenge = _spy
    t._handle({"cmd": "unlock_gesture", "token": "b" * 32}, None)
    check("token gate short-circuits before the round (camera never opens)", ran["n"] == 0)

    b = _needs_gesture_svc()
    tok_b = b._handle({"cmd": "unlock"}, None)["token"]
    b._run_challenge = lambda k, *, identity=False: {"ok": False, "reason": "camera-busy"}
    rb = b._handle({"cmd": "unlock_gesture", "token": tok_b}, None)
    check("camera-busy surfaces as itself", rb == {"ok": False, "reason": "camera-busy"}, rb)
    check("camera-busy is lockout-neutral", b._lockout.records == [], b._lockout.records)
    check("camera-busy still burnt the token (one-shot is unconditional)",
          b._gesture_slot is None)


# --- [4] identity binding -----------------------------------------------------------------

_WARMUP = object()          # consumed by _run_challenge's two drain reads, never analyzed
_BASE_POSE = (-13.0, -3.0)  # neutral pitch/yaw as measured on this webcam
_TURN_LEFT_POSE = (-13.0, -3.0 - (YAW_DELTA + 17.0))   # well past YAW_DELTA from the baseline
_NOD_POSE = (-13.0 - (PITCH_DOWN_DELTA + 6.0), -3.0)


def _frame(is_match: bool, pose):
    """A canned FrameAnalysis. The stub recognizer returns the frame unchanged, so the frame
    IS the analysis -- no camera, no engine, no image data."""
    return FrameAnalysis(True, is_match, 0.07 if is_match else 0.9, False, None,
                         np.asarray(pose + (0.0,), dtype=np.float32), None)


class _StubCam:
    """Yields canned frames, then None forever, flagging when it ran dry."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.drained = False

    def read(self):
        if self._frames:
            return self._frames.pop(0)
        self.drained = True
        return None

    def close(self):
        pass


class _StubRecog:
    def analyze_frame(self, frame):
        return frame


class _ClockShim:
    """Stands in for the `time` module inside face_service.service for one _run_challenge call.

    monotonic() jumps a million seconds the moment the camera stub runs dry, so the 11 s
    wall-clock cap trips instantly instead of being waited out -- the selftest never sleeps.
    liveness.py keeps its own real clock (untouched), so a task can only resolve through actual
    gesture data, never through a faked timeout.
    """

    def __init__(self, cam):
        self._cam = cam
        self._base = time.monotonic()

    def monotonic(self):
        return self._base + (1e6 if self._cam.drained else 0.0)

    def time(self):
        return time.time()

    def sleep(self, _s):
        pass


def _round(kind, frames, identity):
    """Drive the REAL _run_challenge over canned frames."""
    s = _svc(Config(pipe_unlock_require_system=False, persistent_camera=False))
    cam = _StubCam([_WARMUP, _WARMUP] + list(frames))
    s.recog = _StubRecog()
    s._acquire_camera = lambda: (cam, False)
    s._camera_leased_out = lambda: False
    real_time = svc.time
    svc.time = _ClockShim(cam)
    try:
        return s._run_challenge(kind, identity=identity)
    finally:
        svc.time = real_time


def test_identity_binding():
    print("[4] identity binding in _run_challenge")
    baseline = [_frame(True, _BASE_POSE)] * GESTURE_BASELINE_FRAMES
    turn = _frame(True, _TURN_LEFT_POSE)
    imposter_baseline = [_frame(False, _BASE_POSE)] * GESTURE_BASELINE_FRAMES
    imposter_turn = _frame(False, _TURN_LEFT_POSE)

    good = _round("turn_left", baseline + [turn], identity=True)
    check("(sanity) matching face performing the gesture passes",
          good.get("passed") is True, good)
    check("(sanity) identity_frames counts every fed frame",
          good.get("identity_frames") == GESTURE_BASELINE_FRAMES + 1, good)
    check("(sanity) distance_best reported", good.get("distance_best") == 0.07, good)

    # (a) the whole gesture performed by a NON-matching face must not advance the task
    a = _round("turn_left", imposter_baseline + [imposter_turn] * 4, identity=True)
    check("(a) non-match frames never resolve the task", a.get("passed") is False, a)
    check("(a) identity_frames == 0", a.get("identity_frames") == 0, a)

    # (b1) motion comes only from the impostor, the matching face merely sits there
    b1 = _round("turn_left",
                [imposter_turn, baseline[0], imposter_turn, baseline[0],
                 imposter_turn, baseline[0], imposter_turn, imposter_turn],
                identity=True)
    check("(b1) impostor motion + passive matching face -> not passed",
          b1.get("passed") is False, b1)
    check("(b1) only the matching frames were fed",
          b1.get("identity_frames") == 3, b1)

    # (b2) the matching face does the whole gesture, impostor frames interleaved as noise
    b2 = _round("turn_left",
                [imposter_turn, baseline[0], imposter_turn, baseline[0],
                 imposter_turn, baseline[0], imposter_turn, turn],
                identity=True)
    check("(b2) interleaved noise does not stop the matching face", b2.get("passed") is True, b2)
    check("(b2) identity_frames counts only matching frames",
          b2.get("identity_frames") == 4, b2)

    # (c) identity=False -- the `challenge` command -- has no filter at all
    c = _round("turn_left", imposter_baseline + [imposter_turn], identity=False)
    check("(c) identity=False passes on non-matching frames", c.get("passed") is True, c)
    check("(c) identity=False reply keeps its exact old key set",
          set(c) == {"ok", "challenge", "prompt", "passed", "state"}, sorted(c))

    nod = _round("nod", [_frame(True, _BASE_POSE)] * GESTURE_BASELINE_FRAMES
                 + [_frame(True, _NOD_POSE)], identity=True)
    check("a second gesture kind (nod) also binds and passes", nod.get("passed") is True, nod)


# --- [5]/[6] strikes + audit --------------------------------------------------------------

def test_strikes_and_audit():
    print("[5]/[6] strike semantics + audit records")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}

    s = _needs_gesture_svc()
    tok = s._handle({"cmd": "unlock"}, None)["token"]
    check("needs-gesture adds NO strike", s._lockout.records == [], s._lockout.records)
    ev, rec = s._audit.records[-1]
    check("phase-1 audit event is 'unlock'", ev == "unlock", ev)
    check("phase-1 audit carries outcome 'needs-gesture'",
          rec.get("outcome") == "needs-gesture", rec)
    check("phase-1 audit carries the gesture", rec.get("gesture") in
          ("blink", "turn_left", "turn_right", "nod"), rec)
    check("phase-1 audit keeps the burst detail", rec.get("verdict") == "NEEDS_GESTURE", rec)
    check("phase-1 audit NEVER records the token",
          "token" not in rec and tok not in repr(rec), rec)

    f = _needs_gesture_svc()
    tok_f = f._handle({"cmd": "unlock"}, None)["token"]
    f._run_challenge = lambda k, *, identity=False: {
        "ok": True, "challenge": k, "prompt": "x", "passed": False, "state": "failed",
        "identity_frames": 1, "distance_best": 0.08}
    rf = f._handle({"cmd": "unlock_gesture", "token": tok_f}, None)
    check("failed round -> gesture-failed", rf.get("reason") == "gesture-failed", rf)
    check("failed round exposes challenge/state/identity_frames",
          rf.get("state") == "failed" and rf.get("identity_frames") == 1, rf)
    check("failed round -> record(False)", f._lockout.records == [False], f._lockout.records)

    t = _needs_gesture_svc()
    tok_t = t._handle({"cmd": "unlock"}, None)["token"]
    t._run_challenge = lambda k, *, identity=False: {
        "ok": True, "challenge": k, "prompt": "x", "passed": True, "state": "passed",
        "identity_frames": 1, "distance_best": 0.08}
    rt = t._handle({"cmd": "unlock_gesture", "token": tok_t}, None)
    check("task passed but too few identity frames -> gesture-failed",
          rt.get("reason") == "gesture-failed", rt)
    check("thin-identity failure -> record(False)", t._lockout.records == [False])

    g = _needs_gesture_svc()
    tok_g = g._handle({"cmd": "unlock"}, None)["token"]
    g._run_challenge = _pass_round("blink", identity_frames=3)
    rg = g._handle({"cmd": "unlock_gesture", "token": tok_g}, None)
    check("granted round -> credentials", rg.get("ok") is True and rg.get("username") == "admin", rg)
    check("granted round -> record(True)", g._lockout.records == [True], g._lockout.records)

    i = _svc()
    i._handle({"cmd": "unlock_gesture", "token": "c" * 32}, None)
    check("invalid token -> no strike at all", i._lockout.records == [], i._lockout.records)

    keys = {"challenge", "passed", "identity_frames", "distance_best", "reason"}
    for label, s2, tok2, runner, expect in (
        ("granted", *_armed(_pass_round("blink", 3)), "granted"),
        ("gesture-failed", *_armed(lambda k, *, identity=False: {
            "ok": True, "challenge": k, "prompt": "x", "passed": False, "state": "failed",
            "identity_frames": 0, "distance_best": None}), "gesture-failed"),
        ("camera-busy", *_armed(lambda k, *, identity=False: {
            "ok": False, "reason": "camera-busy"}), "camera-busy"),
    ):
        s2._run_challenge = runner
        s2._handle({"cmd": "unlock_gesture", "token": tok2}, None)
        ev2, rec2 = s2._audit.records[-1]
        check(f"audit '{label}': event is unlock_gesture", ev2 == "unlock_gesture", ev2)
        check(f"audit '{label}': stable five-key record", set(rec2) == keys, sorted(rec2))
        check(f"audit '{label}': reason == {expect}", rec2.get("reason") == expect, rec2)

    inv = _svc()
    inv._handle({"cmd": "unlock_gesture", "token": "d" * 32}, None)
    ev3, rec3 = inv._audit.records[-1]
    check("audit 'gesture-token-invalid': stable five-key record",
          ev3 == "unlock_gesture" and set(rec3) == keys
          and rec3["reason"] == "gesture-token-invalid", rec3)
    check("audit 'gesture-token-invalid': absent fields are null",
          rec3["challenge"] is None and rec3["passed"] is None
          and rec3["identity_frames"] is None and rec3["distance_best"] is None, rec3)


def _armed(runner):
    """A service with a live phase-1 token, plus that token. (runner is returned untouched so
    the caller's tuple unpacking stays readable.)"""
    s = _needs_gesture_svc()
    tok = s._handle({"cmd": "unlock"}, None)["token"]
    return s, tok, runner


def main() -> int:
    test_phase1_discriminator()
    test_phase1_other_verdicts()
    test_token_slot()
    test_gate_order()
    test_identity_binding()
    test_strikes_and_audit()
    if FAILS:
        print(f"\nGESTURE-ROUND SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nGESTURE-ROUND SELFTEST OK: phase-1 discriminator + one-shot token + gate order; "
          "non-matching frames cannot drive the gesture; strike and audit semantics hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
