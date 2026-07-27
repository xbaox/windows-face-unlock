"""tools/watchdog.py -- Stage 3 / Step 5 service watchdog (self-loop).

Pings the face-unlock pipe on an interval and restarts the service after N consecutive failures,
UNLESS a deliberate-stop pause is active. Reuses the existing `ping` command (no new pipe command).

Restart is KILL-THEN-START (critical): a hung-but-alive service still holds the
``Local\\FaceUnlockService`` single-instance mutex, so a bare ``Start-ScheduledTask`` would spawn an
instance that instantly exits on ERROR_ALREADY_EXISTS and the service would stay dead. So we kill
the stray ``face_service`` process first (commandline match, like tools/clean_restart.ps1), then
WAIT for it to actually die, then start the task. The wait is load-bearing: Stop-Process only
*signals*, and both the mutex and the FIRST_PIPE_INSTANCE pipe name stay held until the last handle
is gone -- starting on a blind delay races a slow-dying process into a mutex-loser exit. A hung
service that never answers ping (e.g. wedged in a long camera open) is what this catches; a per-ping
timeout means "no answer" counts as a failure.

Deployed as the Scheduled Task FaceUnlock-Watchdog, declared in tools/tasks.psd1 and created by
tools/register_tasks.ps1. DEV LAYOUT ONLY for now: the match below is Name='pythonw.exe', which
never matches an installed face_service.exe, so the declaration skips this task in Installed mode.
Run: python -m tools.watchdog
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger("watchdog")

SERVICE_TASK = "FaceUnlock-Service"
_POST_START_SETTLE_S = 5.0     # after task start, wait for a mutex-loser to exit before counting
_UNRECOVERABLE_BACKOFF_S = 120.0  # extra idle when a restart can't bring the service up (see below)
_DEATH_WAIT_S = 10.0     # upper bound on the post-kill "are they really gone?" poll
_DEATH_POLL_MS = 200     # how often that poll re-counts (inside ONE powershell, not one per probe)

# The ONE process-matching criterion, shared by kill / count / death-wait below so the three can
# never drift apart. The filter is Name='pythonw.exe' ONLY: prod runs via pythonw (register_tasks.ps1
# / setup.ps1 both launch .venv\Scripts\pythonw.exe), while a DEV instance is
# `python.exe -m face_service` (visible console) -- so the watchdog restarts the prod service but
# does NOT kill a dev instance you are debugging. The watchdog itself (`-m tools.watchdog`) and
# presence (`-m presence_monitor`) lack "face_service" in their commandline, so they are never
# matched either. NB a healthy venv service is TWO matches, not one: the .venv pythonw.exe launcher
# stub and the base-interpreter worker it spawns both carry "face_service" on their command line.
_MATCH_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" -ErrorAction SilentlyContinue | "
    "Where-Object { $_.CommandLine -like '*face_service*' }"
)
# Kill the stray PROD service by commandline match, then report how many were MATCHED (Stop-Process
# has no -Wait, so this is a signal count, not a death count -- see _wait_dead).
_KILL_PS = (
    "$p = @(" + _MATCH_PS + "); "
    "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
    "$p.Count"
)
# Count running prod (pythonw) service instances -- used after a start to tell "a fresh service came
# up" from "nothing survived (a non-pythonw instance holds the mutex, or the launch is misconfig)".
_COUNT_PS = "@(" + _MATCH_PS + ").Count"
# Poll until nothing matches any more, or the bound elapses; emit the count still standing. The loop
# lives INSIDE one powershell so a 200 ms cadence costs one process launch, not one per probe.
_WAIT_DEAD_PS = (
    "$sw = [Diagnostics.Stopwatch]::StartNew(); "
    "$n = @(" + _MATCH_PS + ").Count; "
    "while ($n -gt 0 -and $sw.Elapsed.TotalSeconds -lt " + repr(_DEATH_WAIT_S) + ") { "
    "Start-Sleep -Milliseconds " + str(_DEATH_POLL_MS) + "; "
    "$n = @(" + _MATCH_PS + ").Count }; "
    "$n"
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
        log.warning("%s failed: %r", label, e)
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
        log.warning("%s failed: %r", label, e)
    return None


def _wait_dead() -> None:
    """Block until every process matching the kill criterion is really gone, or ``_DEATH_WAIT_S``
    elapses. This is what actually frees the ``Local\\FaceUnlockService`` mutex and the
    FIRST_PIPE_INSTANCE pipe name; starting the task while a killed process is still winding down
    hands the new instance an ERROR_ALREADY_EXISTS exit and leaves the service dead until the next
    restart cycle. Bounded on purpose -- on timeout (or if the probe itself fails) we log loudly and
    start anyway, because a watchdog that can hang is worse than one that races."""
    left = _run_ps_int(_WAIT_DEAD_PS, "death-wait")
    if left is None:
        log.warning("death-wait probe failed; starting %s anyway", SERVICE_TASK)
    elif left > 0:
        log.warning("%d pythonw face_service process(es) STILL alive after %gs; starting %s anyway "
                    "(the new instance may exit as a mutex-loser)", left, _DEATH_WAIT_S, SERVICE_TASK)
    else:
        log.info("all killed processes confirmed gone")


def restart_service():
    """Kill any stray PROD (pythonw) face_service process (frees the mutex), start the service task,
    and report whether a service instance survived. Returns ``(killed, alive_after_start)``."""
    killed = _run_ps_int(_KILL_PS, "kill")
    killed = 0 if killed is None else killed
    log.info("signalled %d pythonw face_service process(es); waiting for them to exit...", killed)
    _wait_dead()   # frees the mutex / pipe name / camera handles BEFORE the new instance starts
    log.info("starting %s...", SERVICE_TASK)
    started = _run(["schtasks", "/run", "/tn", SERVICE_TASK], "schtasks /run")
    time.sleep(_POST_START_SETTLE_S)   # give a mutex-loser time to exit (the check is pre-warmup)
    alive = _run_ps_int(_COUNT_PS, "count")
    alive = 0 if alive is None else alive
    log.info("after start: %d pythonw face_service running (schtasks ok=%s)", alive, started)
    return killed, alive


def _setup_logging() -> None:
    """Same file-logging shape as the service and the presence monitor, in its own watchdog.log.
    Load-bearing under the scheduled task: it runs via pythonw.exe, which has no console, so bare
    prints went nowhere -- exactly the diagnostics you want after an unattended restart."""
    from face_service.config import LOG_PATH
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_PATH.with_name("watchdog.log"), encoding="utf-8"),
                  logging.StreamHandler()],
    )


def main(argv=None) -> int:
    _setup_logging()
    from face_service.config import Config, WATCHDOG_PAUSE_PATH
    from face_service.watchdog import should_restart, restart_outcome, is_paused, clear_pause

    cfg = Config()
    interval = cfg.watchdog_interval_s
    timeout = cfg.watchdog_ping_timeout_s
    threshold = cfg.watchdog_fail_threshold
    log.info("self-loop: interval=%ss ping_timeout=%ss fail_threshold=%s pause=%s",
             interval, timeout, threshold, WATCHDOG_PAUSE_PATH)

    fails = 0
    try:
        while True:
            if ping(timeout):
                fails = 0
            else:
                fails += 1
                paused = is_paused(WATCHDOG_PAUSE_PATH, time.time())
                if should_restart(fails, threshold, paused):
                    log.warning("%d consecutive ping failures -> restart (kill-then-start)", fails)
                    killed, alive = restart_service()
                    clear_pause(WATCHDOG_PAUSE_PATH)   # a restart clears the deliberate-stop pause
                    fails = 0
                    if restart_outcome(alive) == "unrecoverable":
                        # Killed the prod pythonw (if any) and started the task, yet NO service
                        # instance survived -> the mutex is held by a non-pythonw instance (a dev
                        # `python -m face_service`, which we spare) or the launch is misconfigured.
                        # Log clearly and back off so we don't tight-loop a no-op kill-start.
                        log.error("no service instance survived the task start (killed %d pythonw, "
                                  "%d running after) -- possibly a dev python.exe '-m face_service' "
                                  "holding the mutex, or a launch misconfig; backing off %gs",
                                  killed, alive, _UNRECOVERABLE_BACKOFF_S)
                        time.sleep(_UNRECOVERABLE_BACKOFF_S)
                    else:
                        log.info("a service instance is up after restart")
                elif paused:
                    log.info("ping failed (%d/%d) but a deliberate pause is active -> not restarting",
                             fails, threshold)
                else:
                    log.info("ping failed (%d/%d)", fails, threshold)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
