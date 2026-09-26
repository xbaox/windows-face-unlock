"""tools/watchdog.py -- the service watchdog (self-loop). Stage 3 / Step 5; reworked in Stage 9 (R11).

Pings the face-unlock pipe on an interval and restarts the service after N consecutive failures,
UNLESS a deliberate-stop pause is active. Reuses the existing `ping` command (no new pipe command).

What counts as alive (Stage 9, act 9b R11): any pong. A service that answers
``{"state":"refusing","why":...}`` (custody, not-owner, no-models, lockout-store-error) is ALIVE --
restarting it would change nothing, so the refusal is logged once per episode and left alone.

Restart is KILL-THEN-START (critical): a hung-but-alive service still holds the
``Local\\FaceUnlockService`` single-instance mutex, so a bare task start would spawn an instance
that instantly exits on ERROR_ALREADY_EXISTS and the service would stay dead. So we kill the stray
``face_service`` process first (see ``_is_service_proc`` for the criterion), WAIT for it to actually
die (the mutex and the FIRST_PIPE_INSTANCE pipe name stay held until the last handle is gone), then
start the task, then WAIT FOR A PONG (at most POST_RESTART_PONG_S). No pong in time = the restart
failed. Restarts of one unhealthy run are spaced 60 s x 2^n apart (cap 30 min); n resets after
10 minutes of health (F-248). EVERY way of not getting a pong counts as a failure -- a server that
stays merely BUSY for the whole ping budget included, because "busy" is exactly what a wedged-but-
alive instance looks like from outside. See ``ping`` for the failure classes.

The failure counter does not grow while a pause is active and restarts from zero when it lifts
(F-247), so a pause that ends never triggers an instant restart of a service that is simply still
starting. A second watchdog in the same session exits at once (``Local\\FaceUnlockWatchdog``,
F-249). A heartbeat line (and a small heartbeat file beside the pause marker) is written every hour,
so a healthy watchdog can be told from one that is not running (D-126).

Process work (kill, wait, count) is psutil, in-process -- no PowerShell child whose failures read as
"zero processes" (F-251).

The watchdog's own settings are read once, at startup, with no reload path: editing config.toml
changes them only after the FaceUnlock-Watchdog task itself restarts (D-127).

Deployed as the Scheduled Task FaceUnlock-Watchdog, declared in tools/tasks.psd1 and created by
tools/register_tasks.ps1. Runs in both layouts: ``python -m tools.watchdog`` from a checkout, and
face_unlock_watchdog.exe (windowed, no console) installed.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is in requirements.txt and in the frozen bundle
    psutil = None  # type: ignore

log = logging.getLogger("watchdog")

SERVICE_TASK = "FaceUnlock-Service"
_DEATH_WAIT_S = 10.0     # upper bound on the post-kill "are they really gone?" wait
_PONG_POLL_S = 1.0       # cadence of the post-restart pong wait
HEARTBEAT_S = 3600.0     # D-126: one heartbeat line (and file) per hour
WATCHDOG_MUTEX = "Local\\FaceUnlockWatchdog"
# (Stage 9, D-129: the connect-retry cadence lives in face_service.pipe_io.RETRY_SLEEP_S.)

# Are we the frozen watchdog exe, or `python -m tools.watchdog` out of the repo? Everything
# layout-dependent below hangs off this one answer. PyInstaller sets sys.frozen on the bundled
# interpreter; nothing else in this project does.
INSTALLED = bool(getattr(sys, "frozen", False))

# The frozen service executable. This string has to agree with THREE other places or the watchdog
# matches nothing: InstalledExe for FaceUnlock-Service in tools/tasks.psd1, the EXE name= in
# installer/windows_face_unlock.spec, and the -Execute the registrar builds from the former.
# tools/packaging_selftest.py pins the first two together.
_INSTALLED_SERVICE_EXE = "face_service.exe"


def _norm(path: str) -> str:
    """F-253: paths are compared as Windows compares them -- case-insensitive, resolved."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except Exception:
        return os.path.normcase(str(path))


def _session_of(pid: int) -> "int | None":
    import ctypes
    sid = ctypes.c_ulong(0)
    try:
        if ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.c_ulong(int(pid)), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return None


def _installed_target() -> str:
    """Full path of the installed service exe: laid down beside this watchdog exe in {app}."""
    return str(Path(sys.executable).resolve().with_name(_INSTALLED_SERVICE_EXE))


def _is_service_proc(info: dict, *, installed: bool, target: str, session: "int | None") -> bool:
    """The ONE process-matching criterion, shared by kill / wait / count.

    INSTALLED: the executable path equals <our install dir>\\face_service.exe (compared with
    normcase + realpath, F-253) and the process runs in OUR session (Stage 8b, F-36).

    DEV: pythonw.exe whose argv carries ``-m face_service`` as arguments (not a substring of the
    interpreter path -- a checkout under a path containing "face_service" must not match the tray
    or this watchdog), in our session. A debugging ``python.exe -m face_service`` with a console is
    spared on purpose. NB a healthy venv service is TWO matches: the .venv launcher stub and the
    base-interpreter worker it spawns carry the same argv.

    ``info`` is a psutil ``as_dict`` with name / exe / cmdline / pid.
    """
    if session is not None and _session_of(info.get("pid") or 0) not in (session, None):
        return False
    if installed:
        exe = info.get("exe") or ""
        return bool(exe) and _norm(exe) == _norm(target)
    if (info.get("name") or "").lower() != "pythonw.exe":
        return False
    argv = [str(a).lower() for a in (info.get("cmdline") or [])]
    return any(a == "-m" and b == "face_service" for a, b in zip(argv, argv[1:]))


# How to name the thing we just killed/counted, in a log line.
_PROC_LABEL = _INSTALLED_SERVICE_EXE if INSTALLED else "pythonw -m face_service"
_UNRECOVERABLE_HINT = (
    "the installed service exe may have been renamed or moved, or the instance is wedged in its "
    "start -- check that InstalledExe in tools/tasks.psd1 still matches what the "
    "FaceUnlock-Service task launches"
    if INSTALLED else
    "possibly a dev python.exe '-m face_service' holding the mutex, or a launch misconfig"
)


def _service_procs() -> list:
    """The running service processes by the layout's criterion ([] without psutil, logged)."""
    if psutil is None:
        log.error("psutil is not available -- cannot find the service process")
        return []
    target = _installed_target() if INSTALLED else ""
    session = _session_of(os.getpid())
    out = []
    for p in psutil.process_iter(attrs=["pid", "name", "exe", "cmdline"]):
        try:
            if _is_service_proc(p.info, installed=INSTALLED, target=target, session=session):
                out.append(p)
        except Exception as e:
            log.debug("process %s skipped: %r", getattr(p, "pid", "?"), e)
    return out


def _kill_and_wait() -> int:
    """Kill every service process and wait (at most _DEATH_WAIT_S) until they are gone -- this is
    what frees the mutex and the pipe name. Returns how many were signalled. Failures are logged
    with their cause (F-251); a watchdog that can hang is worse than one that races, so on timeout
    the start goes ahead anyway."""
    procs = _service_procs()
    for p in procs:
        try:
            p.kill()
        except Exception as e:
            log.warning("kill of pid %s failed: %r", p.pid, e)
    if procs and psutil is not None:
        try:
            _gone, alive = psutil.wait_procs(procs, timeout=_DEATH_WAIT_S)
        except Exception as e:
            log.warning("death-wait failed: %r; starting %s anyway", e, SERVICE_TASK)
        else:
            if alive:
                log.warning("%d %s process(es) STILL alive after %gs; starting %s anyway "
                            "(the new instance may exit as a mutex-loser)",
                            len(alive), _PROC_LABEL, _DEATH_WAIT_S, SERVICE_TASK)
            else:
                log.info("all killed processes confirmed gone")
    return len(procs)


def ping(timeout_s: float, pipe_name: str = "") -> "tuple[bool, str | None]":
    """Ask the service for a pong within ``timeout_s``. Returns ``(ok, reason)``.

    ``ok`` is True for ANY pong -- ``state: refusing`` included (R11); the caller reads the state
    from ``last_ping_state``. ``reason`` is None on success, otherwise one of:

    * ``"busy"``          -- every connect attempt hit ERROR_PIPE_BUSY until the budget ran out.
                             This is what a WEDGED-BUT-ALIVE server looks like from outside, so it
                             stays a FAILURE.
    * ``"no-pipe"``       -- connects kept hitting ERROR_FILE_NOT_FOUND: nothing is listening.
    * ``"reply-timeout"`` -- connected, but the exchange did not finish inside the budget.
    * ``"bad-reply"``     -- an answer arrived and is not a pong. Fails immediately.
    * ``"error: ..."``    -- anything else, carrying the win32 code so the log can be acted on.
    * ``"untrusted-server"`` -- the pipe is owned by a process that is not our own user (F-20).

    The exchange itself is face_service.pipe_io.exchange, shared by every Python client.
    ``timeout_s`` is the budget for the WHOLE exchange, connect included. ``pipe_name`` exists
    for the selftest; production passes nothing and gets face_service.config.PIPE_NAME.
    """
    from face_service.pipe_io import exchange

    global last_ping_state
    resp, why = exchange({"cmd": "ping"}, timeout_s, pipe_name=pipe_name)
    if resp is None:
        return False, why
    if resp.get("ok") and resp.get("pong"):
        st = resp.get("state") or "serving"
        last_ping_state = st if st != "refusing" else f"refusing:{resp.get('why') or '?'}"
        return True, None
    return False, "bad-reply"


last_ping_state = "serving"


# schtasks.exe is a console-subsystem binary and the watchdog has no console of its own (installed:
# a windowed exe; dev: pythonw.exe). Without CREATE_NO_WINDOW Windows allocates a new console for
# the child -- the black window that blinked on the desktop during a restart.
def _run(cmd: list, label: str) -> bool:
    """True iff the command ran and exited 0; a non-zero exit is logged with its stderr tail."""
    try:
        out = subprocess.run(cmd, timeout=30, capture_output=True, text=True, errors="replace",
                             creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
    except Exception as e:
        log.warning("%s failed: %r", label, e)
        return False
    if out.returncode != 0:
        tail = (out.stderr or out.stdout or "").strip()[-200:]
        log.warning("%s exited %d%s", label, out.returncode, f": {tail}" if tail else "")
        return False
    return True


def _wait_pong(timeout_s: float, ping_timeout_s: float) -> bool:
    """R11: after a start, poll for a pong for at most ``timeout_s``."""
    deadline = time.monotonic() + timeout_s
    while True:
        ok, _why = ping(ping_timeout_s)
        if ok:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PONG_POLL_S)


def restart_service(ping_timeout_s: float = 2.0) -> "tuple[int, bool]":
    """Kill any stray service process (frees the mutex), start the service task, and wait for a
    pong. Returns ``(killed, pong)``."""
    from face_service.watchdog import POST_RESTART_PONG_S
    killed = _kill_and_wait()
    log.info("signalled %d %s process(es); starting %s...", killed, _PROC_LABEL, SERVICE_TASK)
    started = _run(["schtasks", "/run", "/tn", SERVICE_TASK], "schtasks /run")
    pong = _wait_pong(POST_RESTART_PONG_S, ping_timeout_s) if started else False
    log.info("after start: pong=%s within %gs (schtasks ok=%s)", pong, POST_RESTART_PONG_S, started)
    return killed, pong


def _setup_logging() -> None:
    """Same file-logging shape as the service and the presence monitor, in its own watchdog.log.
    Load-bearing: the watchdog has no console in either layout (a windowed exe installed, pythonw
    in a checkout), so bare prints went nowhere."""
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    setup_logging(LOG_PATH.with_name("watchdog.log"))


def _load_config():
    """Read the config ONCE, and never let a broken config file take the supervisor down with it.
    Config.load() degrades per key; the except is belt-and-braces for anything unforeseen."""
    from face_service.config import Config
    try:
        return Config.load()
    except Exception as e:
        log.warning("config load failed (%r) -- falling back to built-in defaults", e)
        return Config()


def _single_instance():
    """F-249: the watchdog's own single-instance guard. Returns the mutex handle (keep it alive),
    or None when another watchdog in this session already holds it. A failing check lets this one
    run (a duplicate supervisor is less bad than none)."""
    try:
        import win32api  # type: ignore
        import win32event  # type: ignore
        import winerror  # type: ignore
        h = win32event.CreateMutex(None, False, WATCHDOG_MUTEX)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return None
        return h
    except Exception as e:
        log.warning("watchdog mutex check failed, continuing: %r", e)
        return True


class State:
    """What one watchdog remembers between iterations."""

    def __init__(self, now: float):
        self.fails = 0                 # consecutive ping failures (not counted while paused)
        self.was_paused = False
        self.restarts = 0              # restarts since the service was last healthy for 10 min
        self.last_restart_at: "float | None" = None
        self.healthy_since: "float | None" = None
        self.refusing: "str | None" = None
        # D-126 counters, reset at every heartbeat
        self.n_ok = 0
        self.n_fail = 0
        self.n_restart = 0
        self.last_restart_wall: "float | None" = None
        self.heartbeat_at = now


def _heartbeat(st: State, now: float, force: bool = False) -> None:
    if not force and now - st.heartbeat_at < HEARTBEAT_S:
        return
    st.heartbeat_at = now
    log.info("heartbeat: pings ok=%d failed=%d restarts=%d state=%s last_restart=%s",
             st.n_ok, st.n_fail, st.n_restart, st.refusing or last_ping_state,
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.last_restart_wall))
             if st.last_restart_wall else "never")
    try:
        from face_service.config import WATCHDOG_PAUSE_PATH
        hb = WATCHDOG_PAUSE_PATH.with_name("watchdog_heartbeat.json")
        tmp = hb.with_name(hb.name + ".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "pings_ok": st.n_ok,
                                   "pings_failed": st.n_fail, "restarts": st.n_restart,
                                   "last_restart": st.last_restart_wall,
                                   "state": st.refusing or last_ping_state}), encoding="utf-8")
        os.replace(tmp, hb)
    except Exception as e:
        log.debug("heartbeat file not written: %r", e)
    st.n_ok = st.n_fail = st.n_restart = 0


def main() -> int:
    """Ping / restart loop. The watchdog's settings are read once here (see _load_config)."""
    _setup_logging()
    mutex = _single_instance()
    if mutex is None:
        log.warning("another watchdog is already running in this session; exiting")
        return 0
    from face_service.config import WATCHDOG_PAUSE_PATH

    cfg = _load_config()
    interval = cfg.watchdog_interval_s
    timeout = cfg.watchdog_ping_timeout_s
    threshold = cfg.watchdog_fail_threshold
    pause_ttl = cfg.watchdog_pause_ttl_s
    log.info("self-loop: interval=%ss ping_timeout=%ss fail_threshold=%s pause=%s",
             interval, timeout, threshold, WATCHDOG_PAUSE_PATH)

    st = State(time.monotonic())
    try:
        while True:
            # Stage 8b (F-29): one failing iteration is logged with its traceback and the loop
            # carries on at the normal interval -- the watchdog task has no restart policy.
            try:
                _iteration(st, timeout, threshold, pause_ttl)
            except Exception:
                log.exception("watchdog iteration failed; continuing")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


def _iteration(st: State, timeout: float, threshold: int, pause_ttl: float) -> None:
    """One ping / decide / (restart) step on ``st``."""
    from face_service.config import WATCHDOG_PAUSE_PATH
    from face_service.watchdog import (HEALTHY_RESET_S, is_paused, restart_backoff_s,
                                       restart_outcome, should_restart)

    now = time.monotonic()
    ok, reason = ping(timeout)
    _heartbeat(st, now)
    if ok:
        st.n_ok += 1
        st.fails = 0
        st.was_paused = False
        refusing = last_ping_state if last_ping_state.startswith("refusing") else None
        if refusing != st.refusing:
            if refusing:
                log.warning("service is up but %s -- alive, not restarting", refusing)
            elif st.refusing:
                log.info("service serves again")
            st.refusing = refusing
        if st.healthy_since is None:
            st.healthy_since = now
        if st.restarts and now - st.healthy_since >= HEALTHY_RESET_S:
            log.info("service healthy for %.0f min -- restart back-off reset",
                     HEALTHY_RESET_S / 60)
            st.restarts = 0
        return

    st.n_fail += 1
    st.healthy_since = None
    if is_paused(WATCHDOG_PAUSE_PATH, time.time(), pause_ttl):
        # F-247: a deliberate stop is not a failure run -- nothing is counted while it lasts.
        if not st.was_paused:
            log.info("ping failed (%s) during a deliberate pause -- not counting", reason)
        st.was_paused = True
        st.fails = 0
        return
    if st.was_paused:
        log.info("deliberate pause lifted -- failure count starts from zero")
        st.was_paused = False
        st.fails = 0
    st.fails += 1
    if not should_restart(st.fails, threshold, False):
        log.info("ping failed (%d/%d): %s", st.fails, threshold, reason)
        return
    if st.restarts and st.last_restart_at is not None:
        wait = restart_backoff_s(st.restarts - 1)
        if now - st.last_restart_at < wait:
            log.info("ping failed (%d/%d): %s -- next restart in %.0fs (back-off %d)",
                     st.fails, threshold, reason, wait - (now - st.last_restart_at), st.restarts)
            return
    log.warning("%d consecutive ping failures (%s) -> restart (kill-then-start)", st.fails, reason)
    killed, pong = restart_service(timeout)
    # (Stage 9, F-250: the pause marker is not removed here -- the restarted service does that,
    # and a marker written during the restart window belongs to whoever wrote it.)
    st.restarts += 1
    st.n_restart += 1
    st.last_restart_at = time.monotonic()
    st.last_restart_wall = time.time()
    st.fails = 0
    if restart_outcome(pong) == "unrecoverable":
        log.error("no pong within the restart window (killed %d %s) -- %s; next restart not "
                  "before %.0fs", killed, _PROC_LABEL, _UNRECOVERABLE_HINT,
                  restart_backoff_s(st.restarts - 1))
    else:
        st.healthy_since = time.monotonic()
        log.info("the service answers again after the restart")


if __name__ == "__main__":
    raise SystemExit(main())
