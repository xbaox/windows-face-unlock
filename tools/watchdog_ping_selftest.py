"""tools/watchdog_ping_selftest.py -- Stage 7c-2 proof for tools.watchdog.ping + _load_config.

Covers the single-threaded overlapped ping and every failure class it can report, against a MINI
PIPE SERVER living inside this test process -- never the production service:

  [1] healthy pong                        -> (True, None)
  [2] nothing listening                   -> (False, "no-pipe"), and it spends ~the whole budget
  [3] the single instance held by a hog    -> (False, "busy")   <- what a wedged-but-alive server is
  [4] server accepts, then goes silent     -> (False, "reply-timeout"); cancel+drain must not crash
  [5] server answers garbage               -> (False, "bad-reply"), immediately, without retrying
  [6] Config.load() raises                 -> _load_config() falls back to the built-in defaults

SAFETY -- Stage-7 standing sanction (b), audited before this file was run:
  * No camera. Nothing here imports cv2 or face_service.camera, and importing tools.watchdog runs
    only imports plus constants (its _setup_logging is called by main(), which this never calls).
  * Never the production pipe. Every name here is FaceUnlockPingSelftest<pid>-<n>; the production
    name from face_service.config.PIPE_NAME is only ever the DEFAULT that these calls override.
  * Never the production single-instance mutex Local\\FaceUnlockService: no CreateMutex at all.
  * No file under ~/.face-unlock is read or written. Scenario [6] patches Config.load to raise
    BEFORE it can reach the disk, and the Config() fallback is a plain dataclass construction; no
    other scenario touches config, the gallery or the audit log.

Budgets here are 0.3-0.5s to keep the run short. The production default is untouched
(face_service/config.py watchdog_ping_timeout_s = 2.0).

Run from the repo root:
    .\\.venv\\Scripts\\python.exe -m tools.watchdog_ping_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pywintypes    # type: ignore
import win32file     # type: ignore
import win32pipe     # type: ignore
import winerror      # type: ignore

from tools import watchdog as W

_BUDGET = 0.4        # per-ping budget for the scenarios below (production default stays 2.0)
_HOLD_S = 3.0        # how long a deliberately unhelpful server keeps its connection


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def _pipe_name(n: int) -> str:
    """A name that cannot collide with production or with a parallel run of this test."""
    return r"\\.\pipe\FaceUnlockPingSelftest%d-%d" % (os.getpid(), n)


class _Server:
    """ONE pipe instance in a daemon thread, behaving per ``mode``.

    nMaxInstances=1 on purpose. The real service also has exactly one instance alive at a time --
    face_service/service.py::_serve_one re-creates it per connection -- so a second client gets
    ERROR_PIPE_BUSY there too. This reproduces that observable without the real service.

    Modes: ``pong`` (answer correctly), ``garbage`` (answer non-JSON), ``silent`` (read the request
    and never answer), ``hog`` (accept the connection and serve nothing -- the point is only that
    the single instance is taken).
    """

    def __init__(self, name: str, mode: str):
        self.name = name
        self.mode = mode
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.error: Exception | None = None
        self.th = threading.Thread(target=self._run, name="mini-pipe-server", daemon=True)

    def __enter__(self) -> "_Server":
        self.th.start()
        self.ready.wait(5.0)
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        self.th.join(3.0)

    def _run(self) -> None:
        h = None
        try:
            h = win32pipe.CreateNamedPipe(
                self.name, win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                1,                     # ONE instance -- see the class docstring
                65536, 65536, 0, None,
            )
            self.ready.set()
            try:
                win32pipe.ConnectNamedPipe(h, None)
            except pywintypes.error as e:
                if e.winerror != winerror.ERROR_PIPE_CONNECTED:
                    raise
            if self.mode == "hog":
                self.stop.wait(_HOLD_S)
                return
            win32file.ReadFile(h, 65536)
            if self.mode == "silent":
                self.stop.wait(_HOLD_S)
                return
            if self.mode == "garbage":
                win32file.WriteFile(h, b"this is not json at all")
            else:
                win32file.WriteFile(h, json.dumps({"ok": True, "pong": True}).encode("utf-8"))
            self.stop.wait(1.0)
        except Exception as e:                      # reported by the scenario, never raised here
            self.error = e
        finally:
            self.ready.set()                        # never leave __enter__ blocked on a failed start
            if h is not None:
                try:
                    win32file.CloseHandle(h)
                except Exception:
                    pass


def _timed(fn, *a):
    t0 = time.monotonic()
    out = fn(*a)
    return out, time.monotonic() - t0


def main(argv=None) -> int:
    t = T()

    # --- 1) healthy pong ------------------------------------------------------------------------
    print("[1] a healthy server answers pong")
    name = _pipe_name(1)
    with _Server(name, "pong") as srv:
        (ok, reason), dt = _timed(W.ping, _BUDGET, name)
    t.ok(ok is True, "ok is True")
    t.ok(reason is None, f"reason is None (got {reason!r})")
    t.ok(dt < _BUDGET, f"answered well inside the budget ({dt:.3f}s < {_BUDGET}s)")
    t.ok(srv.error is None, f"mini server had no error ({srv.error!r})")

    # --- 2) nothing listening -------------------------------------------------------------------
    print("[2] no pipe at all -> no-pipe, after spending the budget")
    (ok, reason), dt = _timed(W.ping, _BUDGET, _pipe_name(2))
    t.ok(ok is False, "ok is False")
    t.ok(reason == "no-pipe", f"reason == 'no-pipe' (got {reason!r})")
    t.ok(dt >= _BUDGET * 0.5, f"retried until the budget rather than failing instantly ({dt:.3f}s)")
    t.ok(dt < _BUDGET + 2.0, f"and still returned promptly after it ({dt:.3f}s)")

    # --- 3) the single instance is taken --------------------------------------------------------
    print("[3] the one instance is held by another client -> busy (a wedged-but-alive server)")
    name = _pipe_name(3)
    hog = None
    with _Server(name, "hog") as srv:
        hog = win32file.CreateFile(
            name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0, None, win32file.OPEN_EXISTING, 0, None,
        )
        (ok, reason), dt = _timed(W.ping, _BUDGET, name)
        try:
            win32file.CloseHandle(hog)
        except Exception:
            pass
    t.ok(ok is False, "ok is False -- busy NEVER reads as alive, else a wedged service is never "
                      "restarted")
    t.ok(reason == "busy", f"reason == 'busy' (got {reason!r})")
    t.ok(dt >= _BUDGET * 0.5, f"retried for the budget before giving up ({dt:.3f}s)")

    # --- 4) connected, then silence -------------------------------------------------------------
    print("[4] server accepts and never answers -> reply-timeout (cancel + drain, no crash)")
    name = _pipe_name(4)
    with _Server(name, "silent") as srv:
        (ok, reason), dt = _timed(W.ping, _BUDGET, name)
    t.ok(ok is False, "ok is False")
    t.ok(reason == "reply-timeout", f"reason == 'reply-timeout' (got {reason!r})")
    t.ok(dt >= _BUDGET * 0.5, f"waited for the budget ({dt:.3f}s)")
    t.ok(dt < _BUDGET + 2.0, f"CancelIo + drain returned promptly, no hang ({dt:.3f}s)")
    t.ok(threading.active_count() >= 1, "process still healthy after a cancelled overlapped read")

    # --- 5) garbage answer ----------------------------------------------------------------------
    print("[5] server answers non-JSON -> bad-reply, immediately")
    name = _pipe_name(5)
    with _Server(name, "garbage") as srv:
        (ok, reason), dt = _timed(W.ping, _BUDGET, name)
    t.ok(ok is False, "ok is False")
    t.ok(reason == "bad-reply", f"reason == 'bad-reply' (got {reason!r})")
    t.ok(dt < _BUDGET, f"failed at once instead of retrying a wrong answer ({dt:.3f}s < {_BUDGET}s)")

    # --- 6) a broken config must not take the supervisor down -----------------------------------
    print("[6] Config.load() raises -> _load_config falls back to built-in defaults")
    import face_service.config as C
    original = C.Config.__dict__["load"]

    def _boom(cls):
        raise RuntimeError("simulated unreadable config.toml")

    C.Config.load = classmethod(_boom)
    try:
        cfg = W._load_config()
    finally:
        C.Config.load = original
    t.ok(cfg is not None, "a Config was still returned (the loop can start)")
    t.ok(cfg.watchdog_ping_timeout_s == 2.0, "watchdog_ping_timeout_s fell back to 2.0")
    t.ok(cfg.watchdog_fail_threshold == 3, "watchdog_fail_threshold fell back to 3")
    t.ok(cfg.watchdog_interval_s == 30.0, "watchdog_interval_s fell back to 30.0")
    t.ok(cfg.watchdog_pause_ttl_s == 300.0, "watchdog_pause_ttl_s fell back to 300.0")
    t.ok(C.Config.__dict__["load"] is original, "Config.load restored after the test")

    print()
    print("FAILURES:", t.fail)
    return 1 if t.fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
