"""tools/presence_guards_selftest.py -- Stage 7c-3 proof for the three auto_lock guards.

  [1] _fullscreen_active -- driven through every QUERY_USER_NOTIFICATION_STATE value: True for
      exactly {QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE}, False otherwise,
      False on a non-S_OK HRESULT and False when the call itself raises (fail-open).
  [2] threshold selection -- fullscreen on with the field at 10 locks on the 10th strike and not
      before; the field at 0 never locks at all (strikes still accrue); fullscreen off locks at
      presence_absent_strikes.
  [3] the already-locked gate -- session_locked() True skips the whole tick: no status poll, no
      presence probe, and the absence episode is cleared.
  [4] config -- presence_fullscreen_strikes validates as an integer >= 0 (0 allowed, -1 and bool
      rejected), and the shipped default is 10.
  [5] i18n -- both new keys resolve in en and ru, and _EN / _RU are still key-for-key equal.
  [6] the camera-defect gate in the presence probe -- a burst with no frames at all, and a burst
      that is all black, both report camera-error (present) instead of absence; a NORMAL burst
      with no face still reports absence, so walk-away is intact.

Scenarios [2], [3] and [6] run SHIPPING code paths: [2]/[3] the real PresenceMonitor._tick on a
real Config, [6] the real FaceService._presence_probe_recognition body and the real
FaceService._burst_defect classifier. Nothing in this file reimplements the strike arithmetic, the
threshold choice or the defect criterion -- only the outside world is replaced -- so a regression in
the shipping branch logic shows up here rather than being masked by a copy of it.

SAFETY -- Stage-7 standing sanction (b), audited before this file was run:
  * No camera is opened. [6] does import face_service.service, which pulls in cv2 and numpy via
    face_service.camera / .detector -- those are library loads with NO module-level side effects
    (checked: neither file runs anything at import), and FaceService is NEVER constructed, so no
    Camera, Recognizer or FaceDetector is instantiated. The capture in [6] is a stub whose read()
    returns None or a numpy array. insightface / onnxruntime are imported lazily by the recognizer
    and are never reached.
  * No pipe: presence_monitor.monitor.pipe_call is replaced before any tick runs; the production
    pipe name is never opened, connected to, or even referenced.
  * No production mutex: no CreateMutex anywhere (serve_forever, which creates it, is never called).
  * No file under ~/.face-unlock: Config() is a plain dataclass construction (Config.load is never
    called), and nothing here writes logs, gallery or audit entries.
  * LockWorkStation is replaced by a counter, so a failing test cannot lock the machine.

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m tools.presence_guards_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import ctypes as _real_ctypes
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from face_service.config import Config
from face_service import i18n as I
from face_service import service as S      # imported for its methods only; never instantiated
from presence_monitor import monitor as M


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


class _CtypesShim:
    """Stands in for the ``ctypes`` module inside presence_monitor.monitor.

    Only ``c_int``, ``byref`` and ``windll.shell32.SHQueryUserNotificationState`` are reached by
    _fullscreen_active, so those are all this provides. ``byref`` hands the object straight through
    because the fake callee below takes it directly -- no CPython internals are relied on, and the
    function under test still builds the c_int, calls through, checks the HRESULT and compares the
    state against the real _FULLSCREEN_STATES.
    """

    c_int = _real_ctypes.c_int

    def __init__(self, state: int | None = None, hr: int = 0, boom: bool = False):
        self.state, self.hr, self.boom = state, hr, boom
        outer = self

        class _Shell32:
            def SHQueryUserNotificationState(self, ref):
                if outer.boom:
                    raise OSError("simulated shell32 failure")
                if outer.state is not None:
                    ref.value = outer.state
                return outer.hr

        class _WinDLL:
            shell32 = _Shell32()

        self.windll = _WinDLL()

    @staticmethod
    def byref(x):
        return x


class _Harness:
    """A REAL PresenceMonitor whose module-level dependencies are patched.

    Patched: pipe_call (status + presence answers), _lock_workstation (counter), session_locked,
    is_remote_context, _fullscreen_active. Everything else -- _tick, the strike counter, the
    threshold choice, _reset_strikes -- is the shipping code.
    """

    def __init__(self, cfg: Config, present: bool = False, locked: bool = False,
                 fullscreen: bool = False):
        self.cfg = cfg
        self.present = present
        self.locked = locked
        self.fullscreen = fullscreen
        self.locks = 0
        self.status_calls = 0
        self.probe_calls = 0
        self._saved: dict = {}

    def __enter__(self) -> "_Harness":
        self._saved = {n: getattr(M, n) for n in
                       ("pipe_call", "_lock_workstation", "session_locked",
                        "is_remote_context", "_fullscreen_active")}

        def _pipe_call(req, timeout_s=30.0):
            if req.get("cmd") == "status":
                self.status_calls += 1
                return {"ok": True, "lockout": {"locked": False}}
            self.probe_calls += 1
            return {"ok": True, "present": self.present, "real": True,
                    "mode": self.cfg.presence_mode}

        def _lock():
            self.locks += 1

        M.pipe_call = _pipe_call
        M._lock_workstation = _lock
        M.session_locked = lambda: self.locked
        M.is_remote_context = lambda: (False, "")
        M._fullscreen_active = lambda: self.fullscreen

        self.mon = M.PresenceMonitor(self.cfg)
        self.mon._notify = lambda gate, message: None    # never import the tray from a test
        return self

    def __exit__(self, *exc) -> None:
        for n, v in self._saved.items():
            setattr(M, n, v)

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            self.mon._tick()


class _StubCam:
    """A capture that hands back a canned sequence. ``None`` is what Camera.read() returns for a
    failed read (face_service/camera.py: ``return frame if ok else None``)."""

    def __init__(self, frame):
        self.frame = frame
        self.reads = 0

    def read(self):
        self.reads += 1
        return None if self.frame is None else self.frame.copy()

    def close(self):
        pass


class _StubSvc:
    """Just enough of FaceService for the REAL presence-probe body to run.

    The two methods under test are taken straight off the class, so this exercises the shipping
    probe and the shipping defect classifier -- not a paraphrase of them. FaceService itself is
    never constructed, so no camera, recognizer or detector comes into existence.
    """

    _burst_defect = S.FaceService._burst_defect
    _presence_probe_recognition = S.FaceService._presence_probe_recognition

    def __init__(self, cfg: Config, frame):
        self.cfg = cfg
        self._cam_lock = threading.Lock()
        self._cam = None
        self.cam = _StubCam(frame)
        self.heals: list = []
        outer = self

        class _Recog:                      # a face is never found: every burst is "absent"
            @staticmethod
            def verify_frame(_frame):
                return False, 1.0, False

        self.recog = _Recog()
        self._acquire = lambda: (outer.cam, False)

    def _acquire_camera(self):
        return self._acquire()

    def _note_camera_health(self, frames_ok, luma_max, where):
        self.heals.append((frames_ok, luma_max, where))
        return False                       # self-heal itself is out of scope here


def _cfg(**over) -> Config:
    cfg = Config()                     # plain dataclass construction -- Config.load is NEVER called
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def main(argv=None) -> int:
    t = T()

    # --- 1) _fullscreen_active over every QUNS value --------------------------------------------
    print("[1] _fullscreen_active vs QUERY_USER_NOTIFICATION_STATE")
    saved_ctypes = M.ctypes
    expected_true = {M._QUNS_BUSY, M._QUNS_RUNNING_D3D_FULL_SCREEN, M._QUNS_PRESENTATION_MODE}
    t.ok(expected_true == {2, 3, 4},
         f"the three fullscreen states are 2/3/4 (got {sorted(expected_true)})")
    try:
        for state in range(1, 8):
            M.ctypes = _CtypesShim(state=state)
            got = M._fullscreen_active()
            want = state in expected_true
            t.ok(got is want, f"QUNS={state} -> {got} (want {want})")
        M.ctypes = _CtypesShim(state=3, hr=-2147467259)          # E_FAIL with a fullscreen state
        t.ok(M._fullscreen_active() is False, "non-S_OK HRESULT -> False (fail-open)")
        M.ctypes = _CtypesShim(boom=True)
        t.ok(M._fullscreen_active() is False, "the call raising -> False (fail-open)")
    finally:
        M.ctypes = saved_ctypes
    t.ok(M.ctypes is saved_ctypes, "real ctypes restored")

    # --- 2) which threshold an absence is judged against ----------------------------------------
    print("[2] threshold selection on the absent path (real _tick)")
    with _Harness(_cfg(auto_lock=True, presence_fullscreen_strikes=10),
                  present=False, fullscreen=True) as h:
        h.tick(9)
        t.ok(h.locks == 0, f"fullscreen + field 10: no lock through 9 strikes (locks={h.locks})")
        t.ok(h.mon._strikes == 9, f"strikes accrued normally (got {h.mon._strikes})")
        t.ok(h.mon._fs_episode is True, "the fullscreen episode was flagged once")
        h.tick(1)
        t.ok(h.locks == 1, f"locked on the 10th strike (locks={h.locks})")
        t.ok(h.mon._strikes == 0 and h.mon._fs_episode is False, "episode reset after the lock")

    with _Harness(_cfg(auto_lock=True, presence_fullscreen_strikes=0),
                  present=False, fullscreen=True) as h:
        h.tick(15)
        t.ok(h.locks == 0, f"field 0: never locks while fullscreen (locks={h.locks})")
        t.ok(h.mon._strikes == 15, f"strikes still accrue and stay visible (got {h.mon._strikes})")
        t.ok(h.mon._last.result == "absent", "Status still reports absent")

    with _Harness(_cfg(auto_lock=True, presence_fullscreen_strikes=10),
                  present=False, fullscreen=False) as h:
        h.tick(h.cfg.presence_absent_strikes)
        t.ok(h.locks == 1,
             f"fullscreen off: locks at presence_absent_strikes={h.cfg.presence_absent_strikes}")

    with _Harness(_cfg(auto_lock=False, presence_fullscreen_strikes=10),
                  present=False, fullscreen=False) as h:
        h.tick(h.cfg.presence_absent_strikes)
        t.ok(h.locks == 0, "auto_lock off still never locks (unchanged)")

    # --- 3) the already-locked gate -------------------------------------------------------------
    print("[3] session_locked -> the whole tick is skipped")
    with _Harness(_cfg(auto_lock=True), present=False, locked=True) as h:
        h.mon._strikes = 5
        h.mon._fs_episode = True
        h.tick(3)
        t.ok(h.probe_calls == 0, f"no presence probe while locked (got {h.probe_calls})")
        t.ok(h.status_calls == 0, f"no status poll while locked (got {h.status_calls})")
        t.ok(h.mon._strikes == 0, f"absence episode cleared (strikes={h.mon._strikes})")
        t.ok(h.mon._fs_episode is False, "fullscreen episode cleared too")
        t.ok(h.locks == 0, "never locks an already-locked session")
        t.ok(h.mon._last.reason == "session-locked", f"Status says why (got {h.mon._last.reason!r})")

    with _Harness(_cfg(auto_lock=True), present=False, locked=False) as h:
        h.tick(1)
        t.ok(h.probe_calls == 1, "unlocked again -> the probe runs on the very next tick")
        t.ok(h.status_calls == 1, "and so does the status poll that feeds notify_service_state")

    # --- 4) config field ------------------------------------------------------------------------
    print("[4] presence_fullscreen_strikes validation")
    t.ok(Config().presence_fullscreen_strikes == 10, "shipped default is 10")
    _cfg(presence_fullscreen_strikes=0).validate()
    t.ok(True, "0 validates (0 = never lock while fullscreen)")
    for bad, label in ((-1, "-1"), (True, "True (bool)")):
        try:
            _cfg(presence_fullscreen_strikes=bad).validate()
            t.ok(False, f"{label} should have been rejected")
        except ValueError:
            t.ok(True, f"{label} rejected by validate()")
    t.ok(Config().presence_absent_strikes == 2, "presence_absent_strikes default untouched (2)")
    t.ok(Config().presence_interval_s == 60, "presence_interval_s default untouched (60)")

    # --- 5) i18n --------------------------------------------------------------------------------
    print("[5] i18n keys")
    saved_lang = I.get_language()
    try:
        for lang in ("en", "ru"):
            I.set_language(lang)
            label = I.t("field.presence_fullscreen_strikes")
            desc = I.t("field.presence_fullscreen_strikes.desc")
            t.ok(label != "field.presence_fullscreen_strikes", f"{lang}: label resolves ({label!r})")
            t.ok(desc != "field.presence_fullscreen_strikes.desc" and len(desc) > 40,
                 f"{lang}: desc resolves ({len(desc)} chars)")
        I.set_language("ru")
        t.ok(I.t("field.presence_fullscreen_strikes") != I.TRANSLATIONS["en"]
             ["field.presence_fullscreen_strikes"], "ru is an actual translation, not the EN string")
    finally:
        I.set_language(saved_lang)
    en, ru = I.TRANSLATIONS["en"], I.TRANSLATIONS["ru"]
    t.ok(len(en) == 200, f"_EN has 200 keys (got {len(en)})")
    t.ok(len(ru) == 200, f"_RU has 200 keys (got {len(ru)})")
    t.ok(set(en) == set(ru), "_EN and _RU are still key-for-key equal")

    # --- 6) the camera-defect gate in the real presence probe -----------------------------------
    print("[6] camera defects report camera-error, not absence (real probe body)")
    cfg = _cfg()
    black = np.zeros((8, 8, 3), dtype=np.uint8)
    lit = np.full((8, 8, 3), 200, dtype=np.uint8)

    svc = _StubSvc(cfg, None)                      # every read fails -> no frames at all
    t.ok(svc._burst_defect(0, None) == "zero-frames", "_burst_defect(0, None) == 'zero-frames'")
    t.ok(svc._presence_probe_recognition() == (True, True),
         "zero-frame burst -> (True, True) = camera-error, so no absence strike")
    t.ok(svc.heals and svc.heals[0][0] == 0, "self-heal was still notified (frames_ok=0)")

    svc = _StubSvc(cfg, black)                     # frames arrive, all of them black
    t.ok(svc._burst_defect(3, 0.0) == "black-burst", "_burst_defect(3, 0.0) == 'black-burst'")
    t.ok(svc._presence_probe_recognition() == (True, True),
         "black burst -> (True, True) = camera-error, so no absence strike")

    svc = _StubSvc(cfg, lit)                       # a normal, well-lit burst with nobody in it
    t.ok(svc._burst_defect(3, 200.0) is None, "_burst_defect on a lit burst -> None (no defect)")
    t.ok(svc._presence_probe_recognition() == (False, False),
         "lit burst with no face -> (False, False) = genuine absence, walk-away intact")
    t.ok(cfg.camera_black_luma == 2.0, "camera_black_luma still the frozen 2.0")

    # And the monitor half: a probe that reports present spends no strike.
    with _Harness(_cfg(auto_lock=True), present=True) as h:
        h.tick(5)
        t.ok(h.locks == 0 and h.mon._strikes == 0,
             f"monitor: a camera-error tick costs no strike (strikes={h.mon._strikes})")

    print()
    print("FAILURES:", t.fail)
    return 1 if t.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
