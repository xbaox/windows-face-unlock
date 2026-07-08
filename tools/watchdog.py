"""tools/watchdog.py -- Stage 3 / Step 5 service watchdog (self-loop).

Pings the face-unlock pipe on an interval and restarts the service after N consecutive failures,
UNLESS a deliberate-stop pause is active. Reuses the existing `ping` command (no new pipe command).

Restart is KILL-THEN-START (critical): a hung-but-alive service still holds the
``Local\\FaceUnlockService`` single-instance mutex, so a bare ``Start-ScheduledTask`` would spawn an
instance that instantly exits on ERROR_ALREADY_EXISTS and the service would stay dead. So we kill
the stray ``face_service`` process first (commandline match, like tools/clean_restart.ps1), then
start the task. A hung service that never answers ping (e.g. wedged in a long camera open) is what
this catches; a per-ping timeout means "no answer" counts as a failure.

Deployed as the Scheduled Task FaceUnlock-Watchdog (see tools/register_watchdog_task.ps1).
Run: python -m tools.watchdog
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SERVICE_TASK = "FaceUnlock-Service"
_POST_START_SETTLE_S = 5.0     # after task start, wait for a mutex-loser to exit before counting
_UNRECOVERABLE_BACKOFF_S = 120.0  # extra idle when a restart can't bring the service up (see below)

# Kill the stray PROD service by commandline match, then print how many were killed. The filter is
# Name='pythonw.exe' ONLY: prod runs via pythonw (register_tasks.ps1 / setup.ps1 both launch
# .venv\Scripts\pythonw.exe), while a DEV instance is `python.exe -m face_service` (visible console)
# -- so the watchdog restarts the prod service but does NOT kill a dev instance you are debugging.
# The watchdog itself (`-m tools.watchdog`) and presence (`-m presence_monitor`) lack "face_service"
# in their commandline, so they are never matched either.
_KILL_PS = (
    "$p = @(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
    "Where-Object { $_.CommandLine -like '*face_service*' }); "
    "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
    "$p.Count"
)
# Count running prod (pythonw) service instances -- used after a start to tell "a fresh service came
# up" from "nothing survived (a non-pythonw instance holds the mutex, or the launch is misconfig)".
_COUNT_PS = (
    "@(Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
    "Where-Object { $_.CommandLine -like '*face_service*' }).Count"
)


def ping(timeout_s: float) -> bool:
    """True iff the service answered `ping` with pong within ``timeout_s``. A hung server that
    accepts the connection but never replies is bounded by running the exchange in a worker thread
    and joining with the timeout -> counts as a failure (which is exactly what should trigger a
    restart)."""
    import win32file
    import win32pipe
    from face_service.config import PIPE_NAME

    box = {"ok": False}

    def _do():
        h = None
        try:
            h = win32file.CreateFile(
                PIPE_NAME, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
            win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
            win32file.WriteFile(h, json.dumps({"cmd": "ping"}).encode("utf-8"))
            _hr, data = win32file.ReadFile(h, 65536)
            resp = json.loads(data.decode("utf-8"))
            box["ok"] = bool(resp.get("ok") and resp.get("pong"))
        except Exception:
            box["ok"] = False
        finally:
            if h is not None:
                try:
                    win32file.CloseHandle(h)
                except Exception:
                    pass

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(timeout_s)
    return box["ok"]   # False if the worker is still blocked (hung server) or the exchange failed


def _run(cmd: list, label: str) -> bool:
    try:
        subprocess.run(cmd, timeout=30, capture_output=True)
        return True
    except Exception as e:
        print(f"[watchdog] {label} failed: {e!r}")
        return False


def _run_ps_int(script: str, label: str):
    """Run a PowerShell snippet and parse its last stdout line as an int (a process count)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             timeout=30, capture_output=True, text=True)
        lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
        if lines and lines[-1].lstrip("-").isdigit():
            return int(lines[-1])
    except Exception as e:
        print(f"[watchdog] {label} failed: {e!r}")
    return None


def restart_service():
    """Kill any stray PROD (pythonw) face_service process (frees the mutex), start the service task,
    and report whether a service instance survived. Returns ``(killed, alive_after_start)``."""
    killed = _run_ps_int(_KILL_PS, "kill")
    killed = 0 if killed is None else killed
    print(f"[watchdog] killed {killed} pythonw face_service process(es); starting {SERVICE_TASK}...")
    time.sleep(1.0)   # let the OS release the mutex / pipe / camera handles
    started = _run(["schtasks", "/run", "/tn", SERVICE_TASK], "schtasks /run")
    time.sleep(_POST_START_SETTLE_S)   # give a mutex-loser time to exit (the check is pre-warmup)
    alive = _run_ps_int(_COUNT_PS, "count")
    alive = 0 if alive is None else alive
    print(f"[watchdog] after start: {alive} pythonw face_service running (schtasks ok={started})")
    return killed, alive


def main(argv=None) -> int:
    from face_service.config import Config, WATCHDOG_PAUSE_PATH
    from face_service.watchdog import should_restart, restart_outcome, is_paused, clear_pause

    cfg = Config()
    interval = cfg.watchdog_interval_s
    timeout = cfg.watchdog_ping_timeout_s
    threshold = cfg.watchdog_fail_threshold
    print(f"[watchdog] self-loop: interval={interval}s ping_timeout={timeout}s "
          f"fail_threshold={threshold} pause={WATCHDOG_PAUSE_PATH}")

    fails = 0
    try:
        while True:
            if ping(timeout):
                fails = 0
            else:
                fails += 1
                paused = is_paused(WATCHDOG_PAUSE_PATH, time.time())
                if should_restart(fails, threshold, paused):
                    print(f"[watchdog] {fails} consecutive ping failures -> restart (kill-then-start)")
                    killed, alive = restart_service()
                    clear_pause(WATCHDOG_PAUSE_PATH)   # a restart clears the deliberate-stop pause
                    fails = 0
                    if restart_outcome(alive) == "unrecoverable":
                        # Killed the prod pythonw (if any) and started the task, yet NO service
                        # instance survived -> the mutex is held by a non-pythonw instance (a dev
                        # `python -m face_service`, which we spare) or the launch is misconfigured.
                        # Log clearly and back off so we don't tight-loop a no-op kill-start.
                        print(f"[watchdog] WARNING: no service instance survived the task start "
                              f"(killed {killed} pythonw, {alive} running after) -- possibly a dev "
                              f"python.exe '-m face_service' holding the mutex, or a launch "
                              f"misconfig; backing off {_UNRECOVERABLE_BACKOFF_S:g}s")
                        time.sleep(_UNRECOVERABLE_BACKOFF_S)
                    else:
                        print("[watchdog] a service instance is up after restart")
                elif paused:
                    print(f"[watchdog] ping failed ({fails}/{threshold}) but a deliberate pause is "
                          f"active -> not restarting")
                else:
                    print(f"[watchdog] ping failed ({fails}/{threshold})")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("[watchdog] stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
