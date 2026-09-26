"""tools/enrollment_presence_selftest.py -- Stage 8b package C proof (no camera, no engine, no pipe).

  [1] F-05: a presence reply with ok:false -- on the probe AND on the confirmation re-probe -- is
      skipped like an unreachable service: no strike, never a lock (real PresenceMonitor._tick).
  [2] F-22: an engine that raises on every frame makes the REAL recognition probe report "error"
      (the pipe answers ok:false, reason engine-error), not "absent"; the first exception of the
      episode is logged once with its traceback; a burst with a frame the engine did judge is
      classified exactly as before. analyze_frame raises EngineError (not a RuntimeError) for an
      empty frame and for an engine failure.
  [3] F-06: clear_enrollment makes the running service forget the gallery at once (refs and the
      adaptive ring), deletes embeddings.npz and the whole enroll tree without following a
      junction, drops a pending gesture token, and audits it.
  [4] F-12: build_enrollment with replace builds from the pending session only; a failed build
      leaves the old images untouched, a successful one removes them and promotes the new ones.
  [5] F-31: the tray / wizard mutex admits one instance per name.
  [6] F-30: the wizard's build pause is the standard self-expiring watchdog pause, and only the
      wizard's own marker is cleared afterwards.

Runs on a FACE_UNLOCK_HOME it creates under %TEMP%; the real ~/.face-unlock is never touched.
Run:  python -m tools.enrollment_presence_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_ROOT = Path(tempfile.mkdtemp(prefix="faceunlock_enrollpres_"))
os.environ["FACE_UNLOCK_HOME"] = str(_ROOT / "home")

import numpy as np

from face_service import config as C
from face_service import recognizer as R
from face_service import service as S
from face_service.config import Config
from presence_monitor import monitor as M
from tools.presence_guards_selftest import _Harness, _StubSvc, _cfg

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


# --- [1] F-05 -------------------------------------------------------------------------------------

def test_ok_false():
    print("[1] ok:false is skipped, never scored as absent")
    for label, answers in (("probe", [{"ok": False, "reason": "exception: boom"}]),
                           ("confirmation", [{"ok": True, "present": False, "state": "absent",
                                              "real": True, "mode": "recognition"},
                                             {"ok": False, "reason": "engine-error"}])):
        with _Harness(_cfg(auto_lock=True, presence_absent_strikes=1,
                           presence_confirm_delay_s=0.01), present=False) as h:
            seq = list(answers)

            def _pipe(req, timeout_s=30.0):
                if req.get("cmd") == "status":
                    return {"ok": True, "lockout": {"locked": False}}
                return seq.pop(0) if seq else {"ok": False, "reason": "exception: again"}

            M.pipe_call = _pipe
            h.tick(3)
            check(f"{label}: no strike", h.mon._strikes == 0, h.mon._strikes)
            check(f"{label}: no lock", h.locks == 0, h.locks)
            check(f"{label}: Status shows an error, not absent",
                  h.mon._last.result == "error" and "service-error" in h.mon._last.reason,
                  (h.mon._last.result, h.mon._last.reason))


# --- [2] F-22 -------------------------------------------------------------------------------------

class _ProbeSvc(_StubSvc):
    _note_probe_errors = S.FaceService._note_probe_errors


def test_engine_error():
    print("[2] engine error is not absence")
    frame = np.full((8, 8, 3), 120, dtype=np.uint8)
    svc = _ProbeSvc(_cfg(), frame)

    def boom(_f):
        raise R.EngineError("insightface get failed: simulated")

    svc.recog.verify_frame = boom
    records: list = []

    class _H(logging.Handler):
        def emit(self, rec):
            records.append(rec)

    h = _H(level=logging.DEBUG)
    S.log.addHandler(h)
    old = S.log.level
    S.log.setLevel(logging.DEBUG)
    try:
        state, _real = svc._presence_probe_recognition()
        check("all-error burst -> state 'error'", state == "error", state)
        state2, _ = svc._presence_probe_recognition()
        firsts = [r for r in records if r.levelno == logging.WARNING and "engine error" in r.getMessage()]
        check("first exception logged once, with a traceback",
              len(firsts) == 1 and firsts[0].exc_info is not None, len(firsts))
        check("second erroring burst stays 'error'", state2 == "error", state2)
        svc.recog.verify_frame = lambda _f: (False, 1.0, False)
        state3, _ = svc._presence_probe_recognition()
        check("a judged burst is classified as before (absent)", state3 == "absent", state3)
        check("a clean burst ends the error episode", svc._probe_error_logged is False)
    finally:
        S.log.removeHandler(h)
        S.log.setLevel(old)

    s = S.FaceService.__new__(S.FaceService)
    s._caller_sid = lambda h: "S-1-5-18"   # Stage 9: stand in for the lock screen (SYSTEM)
    s.cfg = Config()
    s._presence_probe = lambda: ("error", False)
    r = s._handle({"cmd": "presence"}, None)
    check("presence reply: ok:false reason engine-error",
          r.get("ok") is False and r.get("reason") == "engine-error" and r.get("present") is False, r)

    rec = R.Recognizer.__new__(R.Recognizer)
    rec._refs = np.zeros((1, 512), dtype=np.float32)

    class _App:
        def get(self, _img):
            raise ValueError("onnx exploded")

    rec._lazy_app = lambda: _App()
    for label, arg in (("engine failure", np.zeros((4, 4, 3), np.uint8)), ("None frame", None)):
        try:
            rec.analyze_frame(arg)
            check(f"analyze_frame raises EngineError on {label}", False, "no exception")
        except R.EngineError:
            check(f"analyze_frame raises EngineError on {label}", True)
        except Exception as e:
            check(f"analyze_frame raises EngineError on {label}", False, repr(e))
    check("EngineError is not a RuntimeError (gesture round's refusal branch stays unreached)",
          not issubclass(R.EngineError, RuntimeError))


# --- [3] F-06 -------------------------------------------------------------------------------------

class _AuditStub:
    def __init__(self):
        self.records: list = []

    def write(self, ev, rec):
        self.records.append((ev, rec))


def _svc_with_real_recognizer():
    s = S.FaceService.__new__(S.FaceService)
    s._caller_sid = lambda h: "S-1-5-18"   # Stage 9: stand in for the lock screen (SYSTEM)
    s.cfg = Config(adaptive_gallery=True)
    s.recog = R.Recognizer(s.cfg)
    s._audit = _AuditStub()
    s._gesture_slot = {"token": "a" * 32, "kind": "blink", "expires": 1e18}
    return s


def test_clear_enrollment():
    print("[3] clear_enrollment forgets and deletes")
    C.ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    (C.ENROLL_DIR / "a.jpg").write_bytes(b"a")
    (C.ENROLL_DIR / "_qc_crops").mkdir(exist_ok=True)
    (C.ENROLL_DIR / "_qc_crops" / "c.png").write_bytes(b"c")
    victim = _ROOT / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"k")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(C.ENROLL_DIR / "link"), str(victim)],
                   capture_output=True, check=True)
    refs = np.random.default_rng(0).normal(size=(3, 512)).astype(np.float32)
    np.savez(C.EMBED_PATH, embeddings=refs, engine=np.array(R.ENGINE_TAG),
             dim=np.array(R.EMBED_DIM, dtype=np.int64))
    s = _svc_with_real_recognizer()
    check("gallery loaded before", s.recog.load() and s.recog._refs is not None)
    s.recog._adaptive.add(refs[0], 1.0, 10)
    s.recog._adaptive.save()
    check("adaptive ring on disk before", C.ADAPTIVE_PATH.exists())

    # Stage 9 (F-136): the leftovers are face data too
    C.EMBED_PATH.with_name(C.EMBED_PATH.name + ".tmp").write_bytes(b"t")
    C.ADAPTIVE_PATH.with_name(C.ADAPTIVE_PATH.name + ".tmp").write_bytes(b"t")
    dumps = C.APP_DIR / "debug_frames"
    dumps.mkdir(exist_ok=True)
    (dumps / "20260101-000000-000_verify0.npy").write_bytes(b"x")
    r = s._handle({"cmd": "clear_enrollment"}, None)
    check("reply ok", r.get("ok") is True and r.get("removed", 0) >= 4, r)
    check("F-136: *.npz.tmp leftovers and frame dumps deleted too",
          not C.EMBED_PATH.with_name(C.EMBED_PATH.name + ".tmp").exists()
          and not C.ADAPTIVE_PATH.with_name(C.ADAPTIVE_PATH.name + ".tmp").exists()
          and not (dumps / "20260101-000000-000_verify0.npy").exists())
    check("refs forgotten in memory", s.recog._refs is None and s.recog._enroll_refs is None)
    check("adaptive ring gone (memory + disk)",
          s.recog._adaptive.count == 0 and not C.ADAPTIVE_PATH.exists())
    check("embeddings.npz deleted", not C.EMBED_PATH.exists())
    check("enroll tree deleted, sub-directories included", not C.ENROLL_DIR.exists())
    check("junction target untouched", (victim / "keep.txt").exists())
    check("pending gesture token dropped", s._gesture_slot is None)
    check("audited", s._audit.records and s._audit.records[-1][0] == "clear_enrollment",
          s._audit.records)
    try:
        s.recog.analyze_frame(np.zeros((4, 4, 3), np.uint8))
        check("matching refuses after the clear", False, "no exception")
    except RuntimeError as e:
        check("matching refuses after the clear (no enrollment)", "No enrollment" in str(e), e)


# --- [4] F-12 -------------------------------------------------------------------------------------

def test_replace():
    print("[4] build_enrollment replace -- atomic (Stage 9, R6 / F-132) and audited (D-82)")
    C.ENROLL_DIR.mkdir(parents=True, exist_ok=True)
    for n in ("old1.jpg", "old2.jpg"):
        (C.ENROLL_DIR / n).write_bytes(b"o")
    C.ENROLL_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    for n in ("new1.jpg", "new2.jpg"):
        (C.ENROLL_PENDING_DIR / n).write_bytes(b"n")
    s = _svc_with_real_recognizer()
    built: list = []
    committed: list = []

    def fail(d):
        built.append(Path(d))
        raise RuntimeError("enrollment rejected: only 1 of 1 image(s) passed quality control")

    s.recog.build_gallery = fail
    s.recog.commit_gallery = lambda e: committed.append(e.shape)
    r = s._handle({"cmd": "build_enrollment", "replace": True}, None)
    check("failed build -> ok:false", r.get("ok") is False, r)
    check("built from the pending session only", built[-1] == C.ENROLL_PENDING_DIR, built)
    check("nothing committed, old images untouched, pending kept",
          committed == [] and sorted(p.name for p in C.ENROLL_DIR.glob("*.jpg")) == ["old1.jpg", "old2.jpg"]
          and (C.ENROLL_PENDING_DIR / "new1.jpg").exists())
    check("the failed build is audited (D-82)",
          s._audit.records[-1][0] == "enroll_build" and s._audit.records[-1][1]["ok"] is False,
          s._audit.records[-1:])

    # staging fails half way (a locked file): everything is rolled back, nothing committed
    s.recog.build_gallery = lambda d: (np.ones((2, 512), np.float32), None)
    real_replace = S.os.replace

    def flaky(src, dst):
        if Path(dst).name == "new2.jpg" and Path(dst).parent == C.ENROLL_DIR:
            raise PermissionError(13, "locked", str(dst))
        return real_replace(src, dst)

    S.os.replace = flaky
    try:
        r = s._handle({"cmd": "build_enrollment", "replace": True}, None)
    finally:
        S.os.replace = real_replace
    check("staging failure -> ok:false staging-failed", r == {"ok": False, "reason": "staging-failed"}, r)
    check("... old images back in place, new ones back in pending, nothing committed",
          sorted(p.name for p in C.ENROLL_DIR.glob("*.jpg")) == ["old1.jpg", "old2.jpg"]
          and sorted(p.name for p in C.ENROLL_PENDING_DIR.glob("*.jpg")) == ["new1.jpg", "new2.jpg"]
          and committed == [], (list(C.ENROLL_DIR.glob("*.jpg")), committed))

    r = s._handle({"cmd": "build_enrollment", "replace": True}, None)
    check("successful build -> replaced", r.get("ok") is True and r.get("replaced") is True
          and r.get("count") == 2, r)
    check("the new gallery was committed once", committed == [(2, 512)], committed)
    check("old images removed, new promoted",
          sorted(p.name for p in C.ENROLL_DIR.glob("*.jpg")) == ["new1.jpg", "new2.jpg"])
    check("pending and retired folders removed",
          not C.ENROLL_PENDING_DIR.exists() and not (C.ENROLL_DIR / ".retired").exists())
    ev, rec = s._audit.records[-1]
    check("audited: enroll_build replace ok", ev == "enroll_build" and rec.get("mode") == "replace"
          and rec.get("ok") is True and rec.get("promoted") == 2, (ev, rec))

    s.recog.enroll_from_dir = lambda d: 1
    r = s._handle({"cmd": "build_enrollment", "replace": "yes"}, None)
    check("replace must be literally true (else the ordinary build)", r.get("replaced") is None, r)
    check("the ordinary build is audited too (D-82)",
          s._audit.records[-1][0] == "enroll_build" and s._audit.records[-1][1]["mode"] == "add",
          s._audit.records[-1:])


# --- [5] F-31 -------------------------------------------------------------------------------------

def test_single_instance():
    print("[5] single-instance mutex")
    from presence_monitor import __main__ as PM
    name = "Local\\FaceUnlockSelftest-" + uuid.uuid4().hex
    check("first instance acquires", PM._first_instance(name) is True)
    check("second instance is refused", PM._first_instance(name) is False)
    check("tray and wizard use distinct names", PM.TRAY_MUTEX != PM.ENROLL_MUTEX)


# --- [6] F-30 -------------------------------------------------------------------------------------

def test_build_pause():
    print("[6] wizard build pause")
    from face_service.watchdog import is_paused, write_pause
    from presence_monitor import enroll_gui as E
    import time
    created = E._pause_watchdog(300.0)
    check("pause written", created is not None and is_paused(C.WATCHDOG_PAUSE_PATH, time.time()))
    E._resume_watchdog(created)
    check("own pause cleared", not C.WATCHDOG_PAUSE_PATH.exists())
    created = E._pause_watchdog(300.0)
    write_pause(C.WATCHDOG_PAUSE_PATH, created + 5.0, 300.0)    # someone else's, newer
    E._resume_watchdog(created)
    check("a foreign pause is left alone", C.WATCHDOG_PAUSE_PATH.exists())
    C.WATCHDOG_PAUSE_PATH.unlink()


def main() -> int:
    try:
        test_ok_false()
        test_engine_error()
        test_clear_enrollment()
        test_replace()
        test_single_instance()
        test_build_pause()
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(_ROOT)], capture_output=True)
    if FAILS:
        print(f"\nENROLLMENT/PRESENCE SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nENROLLMENT/PRESENCE SELFTEST OK: ok:false never scores as absent; an all-error "
          "engine burst is an error, not absence; clear_enrollment forgets the gallery at once and "
          "deletes reparse-safe; Replace keeps the old gallery until the new one builds; one tray "
          "and one wizard per session; the build pauses the watchdog and clears only its own pause.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
