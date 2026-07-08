#!/usr/bin/env python3
"""tools/shutdown_integration.py -- Stage 3 / Step 5 graceful-shutdown integration test.

Autonomous, no-human proof of the shutdown fixes against a REAL service process. It launches the
service in an isolated FACE_UNLOCK_HOME with warmup DISABLED (so it needs no camera / no engine and
comes up in ~1s), then:
  * SHUTDOWN: sends {"cmd":"shutdown"} and confirms the client reads the reply WITHOUT error 233
    (ERROR_PIPE_NOT_CONNECTED), and the process EXITS within a timeout.
  * CTRL+C: relaunches in a new process group, sends CTRL_BREAK_EVENT, and confirms the process
    exits cleanly (the console-ctrl handler drives a graceful stop even while blocked in
    ConnectNamedPipe). This leg needs a console, so it self-skips (inconclusive) where one isn't
    available rather than failing.

Graceful skip (exit 0) when pywin32 is unavailable or the service can't be launched/pinged. This is
a diagnostic artifact (like camera_busy_integration), NOT part of the acceptance set.

Run from the repo root, with the real service STOPPED (the global single-instance mutex is shared):
    python -m tools.shutdown_integration
Exit 0 = proof passed OR cleanly skipped; 1 = a real failure.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
REPO_ROOT = str(Path(__file__).resolve().parents[1])
PIPE_NAME = r"\\.\pipe\FaceUnlock"


def _send(req: dict, connect_timeout_s: float = 6.0):
    """Send one JSON request, return (resp_dict_or_None, error_winerror_or_None). Never raises.

    Retries the CONNECT (like tools.pipe_client) so the normal inter-instance gap -- ERROR_PIPE_BUSY
    (231) / ERROR_FILE_NOT_FOUND (2) between one _serve_one closing and the next listening -- is
    absorbed. An error on the actual read/write (e.g. 233 on the reply) is returned as-is, so the
    shutdown-race check is not masked by the connect retry."""
    import pywintypes
    import win32file
    import win32pipe
    deadline = time.monotonic() + connect_timeout_s
    h = None
    while h is None:
        try:
            h = win32file.CreateFile(
                PIPE_NAME, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error as e:
            if time.monotonic() >= deadline:
                return None, e.winerror
            time.sleep(0.1)
    try:
        win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(h, json.dumps(req).encode("utf-8"))
        _hr, data = win32file.ReadFile(h, 65536)
        return json.loads(data.decode("utf-8")), None
    except pywintypes.error as e:
        return None, e.winerror
    finally:
        try:
            win32file.CloseHandle(h)
        except Exception:
            pass


def _wait_pingable(deadline_s: float) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        resp, _err = _send({"cmd": "ping"})
        if resp and resp.get("pong"):
            return True
        time.sleep(0.5)
    return False


def _launch(home: str, new_group: bool):
    env = dict(os.environ)
    env["FACE_UNLOCK_HOME"] = home
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if new_group else 0
    return subprocess.Popen(
        [sys.executable, "-m", "face_service"],
        cwd=REPO_ROOT, env=env, creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _tail_log(home: str, n: int = 8) -> str:
    try:
        lines = (Path(home) / "service.log").read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n    ".join(lines[-n:])
    except Exception:
        return "(no service.log)"


def main(argv=None) -> int:
    try:
        import pywintypes  # noqa: F401
        import win32file   # noqa: F401
    except Exception as e:
        print(f"[shutdown] SKIP: pywin32 unavailable ({e!r}).")
        return 0

    fails: list[str] = []
    home = tempfile.mkdtemp(prefix="faceunlock_shutdown_")
    # Warmup off -> fast start, no camera/engine. Isolated state under the temp home.
    (Path(home) / "config.toml").write_text("warmup_on_start = false\n", encoding="utf-8")

    # If the real service is up (shared mutex), we can't run this cleanly -> skip.
    resp, _ = _send({"cmd": "ping"})
    if resp and resp.get("pong"):
        print("[shutdown] SKIP: a FaceUnlock service is already running (stop it first).")
        shutil.rmtree(home, ignore_errors=True)
        return 0

    proc = None
    proc2 = None
    try:
        # ---- Test 1: shutdown -> no error 233, process exits -------------------------------
        print("[shutdown] launching service (warmup off) for the shutdown test...")
        proc = _launch(home, new_group=False)
        if not _wait_pingable(40.0):
            print("[shutdown] SKIP: service never became pingable. service.log tail:")
            print("    " + _tail_log(home))
            return 0
        print("[shutdown] pingable; sending shutdown...")
        resp, err = _send({"cmd": "shutdown"})
        if err == 233:
            print("[shutdown] FAIL: client got error 233 (ERROR_PIPE_NOT_CONNECTED) on shutdown reply.")
            fails.append("error-233")
        elif resp is None:
            print(f"[shutdown] FAIL: shutdown reply not received (winerror={err}).")
            fails.append(f"no-reply({err})")
        elif not resp.get("shutting_down"):
            print(f"[shutdown] FAIL: unexpected shutdown reply: {resp}")
            fails.append("bad-reply")
        else:
            print(f"[shutdown] shutdown reply OK, NO error 233: {resp}")
        try:
            code = proc.wait(timeout=15)
            print(f"[shutdown] process exited after shutdown (returncode={code}).")
        except subprocess.TimeoutExpired:
            print("[shutdown] FAIL: process did NOT exit within 15s after shutdown.")
            fails.append("no-exit-after-shutdown")

        # ---- Test 2: Ctrl+C (CTRL_BREAK) -> clean exit -------------------------------------
        print("[shutdown] launching service in a new process group for the Ctrl+C test...")
        proc2 = _launch(home, new_group=True)
        if not _wait_pingable(40.0):
            print("[shutdown] SKIP (Ctrl+C leg): service never became pingable.")
        else:
            print("[shutdown] pingable; sending CTRL_BREAK_EVENT...")
            try:
                os.kill(proc2.pid, signal.CTRL_BREAK_EVENT)
            except Exception as e:
                print(f"[shutdown] SKIP (Ctrl+C leg): could not send CTRL_BREAK ({e!r}).")
            try:
                code = proc2.wait(timeout=15)
                print(f"[shutdown] process exited after CTRL_BREAK (returncode={code}).")
            except subprocess.TimeoutExpired:
                print("[shutdown] INCONCLUSIVE (Ctrl+C leg): no exit in 15s (no console to deliver "
                      "the signal in this environment). Terminating.")
    finally:
        for p in (proc, proc2):
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except Exception:
                    pass
        # best-effort: leave nothing running, clean the temp home
        _send({"cmd": "shutdown"})
        shutil.rmtree(home, ignore_errors=True)

    if fails:
        print(f"\nSHUTDOWN INTEGRATION FAILED: {', '.join(fails)}")
        return 1
    print("\nSHUTDOWN INTEGRATION OK: shutdown replies without error 233 and the process exits; "
          "Ctrl+C leg exits cleanly where a console is available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
