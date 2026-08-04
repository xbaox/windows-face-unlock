"""tools/watchdog.py -- Stage 3 / Step 5 service watchdog (self-loop).

Pings the face-unlock pipe on an interval and restarts the service after N consecutive failures,
UNLESS a deliberate-stop pause is active. Reuses the existing `ping` command (no new pipe command).

Restart is KILL-THEN-START (critical): a hung-but-alive service still holds the
``Local\\FaceUnlockService`` single-instance mutex, so a bare ``Start-ScheduledTask`` would spawn an
instance that instantly exits on ERROR_ALREADY_EXISTS and the service would stay dead. So we kill
the stray ``face_service`` process first (commandline match -- the same criterion the registrar
tools/register_tasks.ps1 carries; tools/clean_restart.ps1 is a thin wrapper over it), then
WAIT for it to actually die, then start the task. The wait is load-bearing: Stop-Process only
*signals*, and both the mutex and the FIRST_PIPE_INSTANCE pipe name stay held until the last handle
is gone -- starting on a blind delay races a slow-dying process into a mutex-loser exit. A hung
service that never answers ping (e.g. wedged in a long camera open) is what this catches: the ping
is bounded by a per-ping budget, and EVERY way of not getting a pong counts as a failure -- a server
that stays merely BUSY for the whole budget included, because "busy" is exactly what a wedged-but-
alive instance looks like from outside. See ``ping`` for the failure classes.

CONFIG IS READ ONCE, AT STARTUP, and there is no reload path: editing ~/.face-unlock/config.toml
changes nothing until the FaceUnlock-Watchdog task itself is restarted. Before Stage 7c-2 the file
was not read AT ALL -- main() constructed Config() directly, so every watchdog number was the
built-in default no matter what the file said.

Deployed as the Scheduled Task FaceUnlock-Watchdog, declared in tools/tasks.psd1 and created by
tools/register_tasks.ps1. Runs in BOTH layouts since 7d-D: the process-matching criterion is
chosen from ``sys.frozen`` (see ``_match_ps``), because the dev service is a pythonw.exe with
"face_service" on its command line while the installed one is a bare face_service.exe -- and a
matcher that only knew the first made every kill, death-wait and survivor count silently return
zero on an installed machine.
Run: python -m tools.watchdog   (dev)   |   face_unlock_watchdog.exe   (installed)
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger("watchdog")

SERVICE_TASK = "FaceUnlock-Service"
_POST_START_SETTLE_S = 5.0     # after task start, wait for a mutex-loser to exit before counting
_UNRECOVERABLE_BACKOFF_S = 120.0  # extra idle when a restart can't bring the service up (see below)
_DEATH_WAIT_S = 10.0     # upper bound on the post-kill "are they really gone?" poll
_DEATH_POLL_MS = 200     # how often that poll re-counts (inside ONE powershell, not one per probe)

# Connect-phase retry cadence for ping (7c-2). The pipe server is strictly SEQUENTIAL: it keeps ONE
# instance and re-creates it per connection (face_service/service.py::_serve_one), so while a request
# is being served a client gets ERROR_PIPE_BUSY, and in the sliver between that CloseHandle and the
# next CreateNamedPipe it gets ERROR_FILE_NOT_FOUND. Neither proves the service is dead, so we retry
# at this cadence INSIDE the same budget instead of failing on the first refusal -- the shape
# presence_monitor/monitor.py and tools/pipe_client.py already use. This is a cadence, NOT a budget:
# the budget stays cfg.watchdog_ping_timeout_s, enforced by the deadline, and a server that is busy
# for the WHOLE budget still fails (see ping).
_PING_RETRY_SLEEP_S = 0.1

# Are we the frozen watchdog exe, or `python -m tools.watchdog` out of the repo? Everything
# layout-dependent below hangs off this one answer. PyInstaller sets sys.frozen on the bundled
# interpreter; nothing else in this project does.
INSTALLED = bool(getattr(sys, "frozen", False))

# The frozen service executable. This string has to agree with THREE other places or the watchdog
# matches nothing: InstalledExe for FaceUnlock-Service in tools/tasks.psd1, the EXE name= in
# installer/windows_face_unlock.spec, and the -Execute the registrar builds from the former.
# tools/packaging_selftest.py pins the first two together.
_INSTALLED_SERVICE_EXE = "face_service.exe"


def _match_ps(installed: bool) -> str:
    """The ONE process-matching criterion, shared by kill / count / death-wait so the three can
    never drift apart.

    DEV filters Name='pythonw.exe' AND "face_service" in the command line: the registrar launches
    .venv\\Scripts\\pythonw.exe -m face_service, while a debugging instance is `python.exe -m
    face_service` with a visible console -- so the watchdog restarts the prod service and does NOT
    kill the one you are stepping through. The watchdog itself (`-m tools.watchdog`) and presence
    (`-m presence_monitor`) have no "face_service" in their command line, so they are never matched
    either. NB a healthy venv service is TWO matches, not one: the .venv pythonw.exe launcher stub
    and the base-interpreter worker it spawns both carry "face_service" on their command line.

    INSTALLED filters the image name alone. The task runs <InstallDir>\\face_service.exe with NO
    arguments (tools/register_tasks.ps1), so there is no command-line needle to test -- and the
    dev-instance exemption is meaningless there, because a repo checkout is not what is running.
    Matching by image name is also what the registrar's own Get-FuProcess does in Installed mode.
    """
    if installed:
        return ("Get-CimInstance Win32_Process -Filter \"Name='" + _INSTALLED_SERVICE_EXE + "'\" "
                "-ErrorAction SilentlyContinue")
    return (
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -like '*face_service*' }"
    )


_MATCH_PS = _match_ps(INSTALLED)
# How to name the thing we just killed/counted, in a log line. Used everywhere the restart path
# used to hardcode "pythonw" -- which would have been a lie in the installed layout, and a lie in
# exactly the logs someone reads after an unattended restart went wrong.
_PROC_LABEL = _INSTALLED_SERVICE_EXE if INSTALLED else "pythonw face_service"
# Why a restart can end with nothing running, per layout. Dev has a legitimate suspect the
# installed layout does not have (a debugging `python -m face_service`, which the matcher spares
# on purpose); installed has one dev does not (a stale InstallDir or a renamed exe).
_UNRECOVERABLE_HINT = (
    "the installed service exe may have been renamed or moved -- check that InstalledExe in "
    "tools/tasks.psd1 still matches what the FaceUnlock-Service task launches"
    if INSTALLED else
    "possibly a dev python.exe '-m face_service' holding the mutex, or a launch misconfig"
)
# Kill the stray PROD service by the layout's criterion, then report how many were MATCHED
# (Stop-Process has no -Wait, so this is a signal count, not a death count -- see _wait_dead).
_KILL_PS = (
    "$p = @(" + _MATCH_PS + "); "
    "$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
    "$p.Count"
)
# Count running prod service instances -- used after a start to tell "a fresh service came up"
# from "nothing survived" (something else holds the mutex, or the launch is misconfigured).
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


def ping(timeout_s: float, pipe_name: str = "") -> "tuple[bool, str | None]":
    """Ask the service for a pong within ``timeout_s``. Returns ``(ok, reason)``.

    ``reason`` is None on success, otherwise one of:

    * ``"busy"``          -- every connect attempt hit ERROR_PIPE_BUSY until the budget ran out.
                             This is what a WEDGED-BUT-ALIVE server looks like from outside (Stage-7a
                             exhibit A: one instance stuck forever inside a single verify), so it
                             stays a FAILURE. "Busy" must never read as "alive", or the one thing
                             this supervisor exists for -- restarting a hung service -- never fires.
    * ``"no-pipe"``       -- connects kept hitting ERROR_FILE_NOT_FOUND: nothing is listening.
    * ``"reply-timeout"`` -- connected, but the exchange did not finish inside the budget.
    * ``"bad-reply"``     -- an answer arrived and is not a pong (unparseable, or no ok/pong). Fails
                             IMMEDIATELY, without retrying: a server that answers wrongly will not
                             answer better a tenth of a second later.
    * ``"error: ..."``    -- anything else, carrying the win32 code so the log can be acted on.

    SINGLE-THREADED by construction. The previous version ran the exchange in a worker thread and
    joined with the timeout: the join returned, but the worker stayed BLOCKED inside the native call
    -- one leaked daemon thread per timed-out ping, accumulating precisely while the service was
    wedged. Overlapped I/O bounds the wait with no second thread: on expiry we CancelIo (which
    cancels this thread's pending op on this handle, and there is exactly one) and then DRAIN it
    with a blocking GetOverlappedResult. That drain is mandatory, not tidiness: the kernel owns the
    OVERLAPPED and the read buffer until the cancelled op really ends, so freeing them or closing
    the handle first is a use-after-free.

    ``timeout_s`` keeps its meaning -- the budget for the WHOLE exchange, connect included, exactly
    as the old join() bounded the whole worker. ``pipe_name`` exists for the selftest; production
    passes nothing and gets face_service.config.PIPE_NAME.
    """
    import pywintypes
    import win32con
    import win32event
    import win32file
    import win32pipe
    import winerror
    from face_service.config import PIPE_NAME

    name = pipe_name or PIPE_NAME
    deadline = time.monotonic() + float(timeout_s)

    def _shut(handle) -> None:
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass

    def _sleep_within() -> bool:
        """Sleep one retry cadence, clamped to what is left. False = the budget is spent."""
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(_PING_RETRY_SLEEP_S, left))
        return True

    # --- phase 1: connect. Retry the two refusals that mean "the server is mid-turnover"; every
    # other win32 failure is reported as-is rather than burning the budget on a hopeless retry.
    h = None
    stalled = "no-pipe"          # which refusal we were still getting when the budget ran out
    while True:
        try:
            h = win32file.CreateFile(
                name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED, None,
            )
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_PIPE_BUSY:
                stalled = "busy"
            elif e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                stalled = "no-pipe"
            else:
                return False, f"error: connect winerror={e.winerror}"
            if not _sleep_within():
                return False, stalled
            continue
        try:
            win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        except pywintypes.error:
            # Same phase, same race: the server tore its instance down between our CreateFile and
            # this call. Drop the handle and try to catch the next instance inside the budget.
            _shut(h)
            h = None
            stalled = "busy"
            if not _sleep_within():
                return False, stalled
            continue
        break

    # --- phase 2: exchange, bounded by what is LEFT of the same budget.
    ov = pywintypes.OVERLAPPED()
    ov.hEvent = win32event.CreateEvent(None, True, False, None)   # manual-reset, unsignalled

    def _await(label: str):
        """Bytes transferred, or None if the budget expired (then: cancel, drain, report)."""
        ms = max(0, int((deadline - time.monotonic()) * 1000))
        if win32event.WaitForSingleObject(ov.hEvent, ms) == win32event.WAIT_OBJECT_0:
            return win32file.GetOverlappedResult(h, ov, False)
        win32file.CancelIo(h)
        try:
            win32file.GetOverlappedResult(h, ov, True)   # MUST finish before anything is freed
        except Exception:
            pass
        log.debug("ping: %s did not finish within the budget; cancelled and drained", label)
        return None

    try:
        win32event.ResetEvent(ov.hEvent)
        win32file.WriteFile(h, json.dumps({"cmd": "ping"}).encode("utf-8"), ov)
        if _await("write") is None:
            return False, "reply-timeout"

        buf = win32file.AllocateReadBuffer(65536)
        win32event.ResetEvent(ov.hEvent)
        win32file.ReadFile(h, buf, ov)
        n = _await("read")
        if n is None:
            return False, "reply-timeout"

        try:
            resp = json.loads(bytes(buf[:n]).decode("utf-8"))
        except Exception:
            return False, "bad-reply"
        if isinstance(resp, dict) and resp.get("ok") and resp.get("pong"):
            return True, None
        return False, "bad-reply"
    except pywintypes.error as e:
        return False, f"error: exchange winerror={e.winerror}"
    except Exception as e:
        return False, f"error: {e!r}"
    finally:
        _shut(ov.hEvent)
        _shut(h)


# Both helpers below spawn a console-subsystem binary (schtasks.exe / powershell.exe) while the
# watchdog itself runs under the scheduled task via pythonw.exe -- a parent with NO console of its
# own. Without CREATE_NO_WINDOW Windows allocates a BRAND NEW console for each child: those are the
# black windows that blink on the desktop during a restart. capture_output only redirects the
# streams, it does not stop the allocation. Same idiom as presence_monitor/tray.py::_launch_enroll.
def _run(cmd: list, label: str) -> bool:
    """True iff the command LAUNCHED (no exception). A non-zero exit is LOGGED, not returned as
    False: restart_service only reports this value, so changing its meaning would change that
    report. Before this the whole result -- exit code and stderr alike -- was dropped silently."""
    try:
        out = subprocess.run(cmd, timeout=30, capture_output=True, text=True, errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        if out.returncode != 0:
            tail = (out.stderr or "").strip()[-200:]
            log.warning("%s exited %d%s", label, out.returncode, f": {tail}" if tail else "")
        return True
    except Exception as e:
        log.warning("%s failed: %r", label, e)
        return False


def _run_ps_int(script: str, label: str):
    """Run a PowerShell snippet and parse its last stdout line as an int (a process count).
    CREATE_NO_WINDOW for the same reason as _run above; the parsing is untouched."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             timeout=30, capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
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
        log.warning("%d %s process(es) STILL alive after %gs; starting %s anyway "
                    "(the new instance may exit as a mutex-loser)",
                    left, _PROC_LABEL, _DEATH_WAIT_S, SERVICE_TASK)
    else:
        log.info("all killed processes confirmed gone")


def restart_service():
    """Kill any stray prod service process (frees the mutex), start the service task, and report
    whether a service instance survived. Returns ``(killed, alive_after_start)``.

    What counts as "the service process" is layout-dependent -- see ``_match_ps``. The task name
    is not: FaceUnlock-Service is the same in both layouts, so the start step needs no branch."""
    killed = _run_ps_int(_KILL_PS, "kill")
    killed = 0 if killed is None else killed
    log.info("signalled %d %s process(es); waiting for them to exit...", killed, _PROC_LABEL)
    _wait_dead()   # frees the mutex / pipe name / camera handles BEFORE the new instance starts
    log.info("starting %s...", SERVICE_TASK)
    started = _run(["schtasks", "/run", "/tn", SERVICE_TASK], "schtasks /run")
    time.sleep(_POST_START_SETTLE_S)   # give a mutex-loser time to exit (the check is pre-warmup)
    alive = _run_ps_int(_COUNT_PS, "count")
    alive = 0 if alive is None else alive
    log.info("after start: %d %s running (schtasks ok=%s)", alive, _PROC_LABEL, started)
    return killed, alive


def _setup_logging() -> None:
    """Same file-logging shape as the service and the presence monitor, in its own watchdog.log.
    Load-bearing under the scheduled task: it runs via pythonw.exe, which has no console, so bare
    prints went nowhere -- exactly the diagnostics you want after an unattended restart. That is
    also why the stream handler is conditional now: this function used to reason correctly about
    having no console and then attach one anyway."""
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    setup_logging(LOG_PATH.with_name("watchdog.log"))


def _load_config():
    """Read the config ONCE, and never let a broken config file take the supervisor down with it.

    Since Stage 7d-A ``Config.load()`` degrades to defaults with an ERROR of its own rather than
    raising, so this is now belt-and-braces: it was written when load() DID raise, and a watchdog
    that dies there leaves the service unsupervised at exactly the moment someone is hand-editing
    settings. The except stays because that argument still holds for anything unforeseen -- the
    supervisor must survive the config file unconditionally. Either way the outcome is the same:
    degraded (the file's values are ignored) but still supervising.

    Read once, at startup, with no reload path -- see the module docstring. Until Stage 7c-2 this
    was ``Config()``, i.e. the file was never read at all.
    """
    from face_service.config import Config
    try:
        return Config.load()
    except Exception as e:
        log.warning("config load failed (%r) -- falling back to built-in defaults", e)
        return Config()


def main(argv=None) -> int:
    """Ping / restart loop. Config is read ONCE here and never reloaded (see _load_config)."""
    _setup_logging()
    from face_service.config import WATCHDOG_PAUSE_PATH
    from face_service.watchdog import should_restart, restart_outcome, is_paused, clear_pause

    cfg = _load_config()
    interval = cfg.watchdog_interval_s
    timeout = cfg.watchdog_ping_timeout_s
    threshold = cfg.watchdog_fail_threshold
    log.info("self-loop: interval=%ss ping_timeout=%ss fail_threshold=%s pause=%s",
             interval, timeout, threshold, WATCHDOG_PAUSE_PATH)

    fails = 0
    try:
        while True:
            ok, reason = ping(timeout)
            if ok:
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
                        # Killed whatever matched (if anything) and started the task, yet NO
                        # service instance survived. Log clearly and back off so we don't
                        # tight-loop a no-op kill-start. The likely cause differs by layout, so
                        # the hint does too -- a diagnosis that names the wrong layout's failure
                        # is worse than none, and this line is the one someone reads afterwards.
                        log.error("no service instance survived the task start (killed %d %s, "
                                  "%d running after) -- %s; backing off %gs",
                                  killed, _PROC_LABEL, alive, _UNRECOVERABLE_HINT,
                                  _UNRECOVERABLE_BACKOFF_S)
                        time.sleep(_UNRECOVERABLE_BACKOFF_S)
                    else:
                        log.info("a service instance is up after restart")
                elif paused:
                    log.info("ping failed (%d/%d): %s -- but a deliberate pause is active -> "
                             "not restarting", fails, threshold, reason)
                else:
                    log.info("ping failed (%d/%d): %s", fails, threshold, reason)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
