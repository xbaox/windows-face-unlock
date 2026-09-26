"""The Face Unlock scheduled tasks for an INSTALLED copy (Stage 9, act 9b R17).

Setup and the uninstaller call the signed product executable -- ``face_unlock_tray.exe
--register --user-sid <SID>``, ``--unregister``, ``--stop``, ``--start`` -- instead of an
unsigned PowerShell registrar (F-211, SAC). This module is what those flags run: the Windows Task
Scheduler over COM (pywin32), process work with psutil, the pipe with pipe_io. No PowerShell.

``TASKS`` is the declaration of the installed layout; tools/tasks.psd1 carries the same three for
the developer registrar (tools/register_tasks.ps1 -Mode Dev), and tools/packaging_selftest.py keeps
the two equal.

Semantics kept from the PowerShell registrar (8b):
* Register is two-phase. Phase A builds and validates everything -- the owner SID resolves to an
  account, every executable exists, every task definition is built -- and touches nothing. Phase B
  registers (create-or-update in place, no unregister first), removes OUR orphans, stops what runs
  (pipe shutdown first, then the scheduler, then a kill with a bounded death-wait), starts the tasks
  and VERIFIES that each is registered and running or ready (F-232, F-233).
* Stop / Unregister: pipe shutdown first, scheduler stop, kill + death-wait, and a survivor count
  that decides the exit code.

Stage 9 changes: processes are matched by executable path inside the install directory
(normcase + realpath, F-235) in ANY session -- an uninstall run from another user's session stops
the owner's stack too (F-227); orphans are only tasks this product registered (author tag or an
action inside the install directory), never any other "FaceUnlock-*" task (F-239); the task
description is registered (D-118); the log goes to <install>\\logs\\register_tasks.log and is closed
at the end (F-226); every step's result is checked (F-233).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from xml.sax.saxutils import escape

log = logging.getLogger("face_service.taskreg")

AUTHOR = "WindowsFaceUnlock"
DEATH_WAIT_S = 10.0
GRACEFUL_WAIT_S = 5.0
_SID_RE = re.compile(r"^S-1-(5-21|12-1)-\d+-\d+-\d+-\d+$")

TASK_CREATE_OR_UPDATE = 6
TASK_LOGON_INTERACTIVE_TOKEN = 3
TASK_ENUM_HIDDEN = 1
TASK_STATE_READY, TASK_STATE_RUNNING = 3, 4


@dataclass(frozen=True)
class Task:
    name: str
    description: str
    dev_args: str
    exe: str
    priority: int = 7            # the scheduler default (below normal)
    restart_on_failure: bool = False


TASKS: "tuple[Task, ...]" = (
    # Normal priority for the service: it is the process the lock screen waits on (P-10).
    Task("FaceUnlock-Service", "Face Unlock: sign-in service (named pipe, face recognition)",
         "-m face_service", "face_service.exe", priority=5),
    Task("FaceUnlock-Presence", "Face Unlock: tray icon, windows and walk-away lock",
         "-m presence_monitor", "face_unlock_tray.exe", restart_on_failure=True),
    Task("FaceUnlock-Watchdog", "Face Unlock: restarts the service when it stops answering",
         "-m tools.watchdog", "face_unlock_watchdog.exe", restart_on_failure=True),
)


def norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except Exception:
        return os.path.normcase(os.path.abspath(path))


def under(path: str, root: str) -> bool:
    """``path`` lies inside ``root`` (both canonical, case-insensitive)."""
    p, r = norm(path), norm(root).rstrip("\\/")
    return p.startswith(r + os.sep)


def task_xml(task: Task, install_dir: str, user_sid: str) -> str:
    """The Task Scheduler definition (schema 1.2) of one task for the installed layout."""
    exe = os.path.join(install_dir, task.exe)
    restart = ("<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>"
               if task.restart_on_failure else "")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{AUTHOR}</Author>
    <Description>{escape(task.description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user_sid)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_sid)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>{int(task.priority)}</Priority>
    {restart}
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(exe)}</Command>
      <WorkingDirectory>{escape(install_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class Scheduler:
    """The root folder of the Task Scheduler over COM. Tests pass a fake with the same methods."""

    def __init__(self):
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
        pythoncom.CoInitialize()
        self._svc = win32com.client.Dispatch("Schedule.Service")
        self._svc.Connect()
        self._root = self._svc.GetFolder("\\")

    def register(self, name: str, xml: str) -> None:
        self._root.RegisterTask(name, xml, TASK_CREATE_OR_UPDATE, None, None,
                                TASK_LOGON_INTERACTIVE_TOKEN)

    def tasks(self) -> "list[tuple[str, str, str]]":
        """(name, author, first action path) of every task in the root folder."""
        out = []
        coll = self._root.GetTasks(TASK_ENUM_HIDDEN)
        for i in range(1, coll.Count + 1):
            t = coll.Item(i)
            try:
                d = t.Definition
                author = str(d.RegistrationInfo.Author or "")
                path = ""
                if d.Actions.Count:
                    a = d.Actions.Item(1)
                    path = str(getattr(a, "Path", "") or "")
            except Exception:
                author, path = "", ""
            out.append((str(t.Name), author, path))
        return out

    def state(self, name: str) -> "int | None":
        try:
            return int(self._root.GetTask(name).State)
        except Exception:
            return None

    def stop(self, name: str) -> None:
        try:
            self._root.GetTask(name).Stop(0)
        except Exception:
            pass

    def run(self, name: str) -> None:
        self._root.GetTask(name).Run(None)

    def delete(self, name: str) -> None:
        self._root.DeleteTask(name, 0)


def ours(tasks, install_dir: str) -> "list[str]":
    """The names of the tasks this product registered: declared names, or our author tag, or an
    action inside the install directory. Never "any FaceUnlock-*" (F-239)."""
    declared = {t.name for t in TASKS}
    out = []
    for name, author, path in tasks:
        if name in declared or author == AUTHOR or (path and under(path, install_dir)):
            out.append(name)
    return out


def stack_processes(install_dir: str, *, own_pid: "int | None" = None):
    """Face Unlock processes of THIS install, in any session (F-227). Excludes this process."""
    import psutil  # type: ignore
    own = os.getpid() if own_pid is None else own_pid
    out = []
    for p in psutil.process_iter(attrs=["pid", "exe"]):
        try:
            exe = p.info.get("exe") or ""
            if exe and p.info["pid"] != own and under(exe, install_dir):
                out.append(p)
        except Exception:
            continue
    return out


def graceful_shutdown() -> bool:
    """Ask the service to stop over the pipe. Works when this process runs as the owner (the
    common single-user case, elevated or not); another administrator's process is refused by
    the pipe -- then the kill below is the stop (F-228, documented)."""
    try:
        from .pipe_io import exchange
        resp, why = exchange({"cmd": "shutdown"}, 3.0)
    except Exception as e:
        log.info("graceful shutdown not possible: %r", e)
        return False
    if resp and resp.get("ok"):
        log.info("service accepted the shutdown request")
        return True
    log.info("graceful shutdown not accepted (%s)", why or resp)
    return False


def kill_and_wait(install_dir: str, wait_s: float = DEATH_WAIT_S) -> int:
    """Kill the stack and wait (bounded) until it is gone. Returns the survivors."""
    import psutil  # type: ignore
    procs = stack_processes(install_dir)
    for p in procs:
        try:
            p.kill()
            log.info("killed pid %s (%s)", p.pid, p.info.get("exe"))
        except Exception as e:
            log.warning("kill of pid %s failed: %r", p.pid, e)
    if procs:
        try:
            psutil.wait_procs(procs, timeout=wait_s)
        except Exception as e:
            log.warning("death-wait failed: %r", e)
    left = stack_processes(install_dir)
    for p in left:
        log.warning("still running: pid %s %s", p.pid, p.info.get("exe"))
    return len(left)


def _stop_stack(sched, install_dir: str) -> int:
    graceful_shutdown()
    t0 = time.monotonic()
    while time.monotonic() - t0 < GRACEFUL_WAIT_S and any(
            p.info.get("exe", "").lower().endswith("face_service.exe")
            for p in stack_processes(install_dir)):
        time.sleep(0.2)
    for name in ours(sched.tasks(), install_dir):
        sched.stop(name)
    return kill_and_wait(install_dir)


def _account_of(user_sid: str) -> "tuple[str, str]":
    import win32security  # type: ignore
    name, domain, _t = win32security.LookupAccountSid(None, win32security.ConvertStringSidToSid(user_sid))
    return name, domain


def register(install_dir: str, user_sid: str, sched=None, resolve=_account_of) -> int:
    """--register. 0 on success; 1 when anything could not be done or verified."""
    install_dir = norm(install_dir)
    # ---- phase A: build and validate, touch nothing ----
    if not _SID_RE.match(user_sid or ""):
        log.error("not a person's SID: %r", user_sid)
        return 1
    try:
        name, domain = resolve(user_sid)
        log.info("tasks run for %s\\%s (%s)", domain, name, user_sid)
    except Exception as e:
        log.error("the owner SID %s does not resolve to an account: %r", user_sid, e)
        return 1
    plan = []
    for t in TASKS:
        exe = os.path.join(install_dir, t.exe)
        if not os.path.isfile(exe):
            log.error("missing executable: %s", exe)
            return 1
        plan.append((t, task_xml(t, install_dir, user_sid)))
    sched = sched or Scheduler()
    # ---- phase B: commit ----
    failed = False
    for t, xml in plan:
        try:
            sched.register(t.name, xml)
            log.info("registered %s -> %s (priority %d, restart %s)", t.name, t.exe, t.priority,
                     "3 x 1 min" if t.restart_on_failure else "none")
        except Exception as e:
            log.error("registering %s failed: %r", t.name, e)
            failed = True
    declared = {t.name for t in TASKS}
    for name in ours(sched.tasks(), install_dir):
        if name not in declared:
            try:
                sched.delete(name)
                log.info("removed our orphan task %s", name)
            except Exception as e:
                log.warning("removing orphan %s failed: %r", name, e)
    left = _stop_stack(sched, install_dir)
    if left:
        log.warning("%d old process(es) survived the stop; the new ones may exit as duplicates", left)
    for t, _xml in plan:
        try:
            sched.run(t.name)
            log.info("started %s", t.name)
        except Exception as e:
            log.error("starting %s failed: %r", t.name, e)
            failed = True
    time.sleep(1.0)
    for t, _xml in plan:
        st = sched.state(t.name)
        if st not in (TASK_STATE_READY, TASK_STATE_RUNNING):
            log.error("verification: %s is in state %r", t.name, st)
            failed = True
    log.info("register: %s", "FAILED" if failed else "ok")
    return 1 if failed else 0


def unregister(install_dir: str, sched=None) -> int:
    """--unregister: stop everything, remove our tasks, stop again, count survivors."""
    install_dir = norm(install_dir)
    sched = sched or Scheduler()
    _stop_stack(sched, install_dir)
    failed = False
    for name in ours(sched.tasks(), install_dir):
        try:
            sched.delete(name)
            log.info("unregistered %s", name)
        except Exception as e:
            log.error("unregistering %s failed: %r", name, e)
            failed = True
    left = kill_and_wait(install_dir)       # second pass: nothing can start them again now
    if left:
        log.error("%d process(es) still running after unregister", left)
    return 1 if (failed or left) else 0


def stop(install_dir: str, sched=None) -> int:
    install_dir = norm(install_dir)
    sched = sched or Scheduler()
    left = _stop_stack(sched, install_dir)
    log.info("stop: %d survivor(s); registrations kept", left)
    return 1 if left else 0


def start(install_dir: str, sched=None) -> int:
    sched = sched or Scheduler()
    failed = False
    for t in TASKS:
        try:
            sched.run(t.name)
        except Exception as e:
            log.error("starting %s failed: %r", t.name, e)
            failed = True
    return 1 if failed else 0


# ---- the install directory's ACL (R17: checked before the CP DLL is registered) --------------

_TRUSTED = {"S-1-5-18", "S-1-5-32-544",
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}   # TrustedInstaller
_CREATOR_OWNER = "S-1-3-0"
_WRITE_MASK = (0x00000002 | 0x00000004 | 0x00000040 | 0x00010000 | 0x00040000 | 0x00080000
               | 0x10000000 | 0x40000000)   # write/append data, delete child, DELETE, WRITE_DAC,
#                                             WRITE_OWNER, GENERIC_ALL, GENERIC_WRITE


def acl_problems(path: str) -> "list[str]":
    """Anything that lets a non-administrator change ``path``: a write-type ACE for a SID that is
    not SYSTEM / Administrators / TrustedInstaller (CREATOR OWNER only as inherit-only), or an
    owner outside that set. [] = only administrators can write it."""
    import win32security  # type: ignore
    import ntsecuritycon  # type: ignore
    out = []
    sd = win32security.GetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION)
    owner = win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())
    if owner not in _TRUSTED:
        out.append(f"{path}: owner {owner}")
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        return out + [f"{path}: no DACL (everyone has full access)"]
    for i in range(dacl.GetAceCount()):
        (ace_type, ace_flags), mask, sid = dacl.GetAce(i)
        if ace_type != ntsecuritycon.ACCESS_ALLOWED_ACE_TYPE or not (mask & _WRITE_MASK):
            continue
        s = win32security.ConvertSidToStringSid(sid)
        if s in _TRUSTED:
            continue
        if s == _CREATOR_OWNER and (ace_flags & ntsecuritycon.INHERIT_ONLY_ACE):
            continue
        out.append(f"{path}: {s} may write (mask 0x{mask:08x})")
    return out


def verify_install_acl(install_dir: str) -> int:
    """--verify-acl: the install directory, the CP folder and the CP DLL are writable by
    administrators only. 0 ok, 1 not."""
    targets = [install_dir, os.path.join(install_dir, "credential_provider"),
               os.path.join(install_dir, "credential_provider", "FaceCredentialProvider.dll")]
    problems = []
    for p in targets:
        if os.path.exists(p):
            try:
                problems += acl_problems(p)
            except Exception as e:
                problems.append(f"{p}: ACL unreadable ({e!r})")
    for pr in problems:
        log.error("ACL: %s", pr)
    if not problems:
        log.info("ACL: %s is writable by administrators only", install_dir)
    return 1 if problems else 0


# ---- entry point for the tray exe's flags ---------------------------------------------------

def main(argv: "list[str]") -> int:
    """``--register --user-sid S`` | ``--unregister`` | ``--stop`` | ``--start`` | ``--verify-acl``.
    Installed layout only: the install directory is the directory of this executable."""
    if not getattr(sys, "frozen", False):
        print("these options are for an installed copy; in a checkout use "
              "tools\\register_tasks.ps1 -Mode Dev", file=sys.stderr)
        return 2
    if sys.maxsize <= 2 ** 32:
        print("a 64-bit process is required", file=sys.stderr)     # F-234
        return 2
    install_dir = os.path.dirname(os.path.abspath(sys.executable))
    logs = os.path.join(install_dir, "logs")
    handler = None
    try:
        os.makedirs(logs, exist_ok=True)
        handler = logging.FileHandler(os.path.join(logs, "register_tasks.log"), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception:
        handler = None
    try:
        flag = argv[0] if argv else ""
        log.info("%s %s (install dir %s)", os.path.basename(sys.executable), " ".join(argv), install_dir)
        if flag == "--register":
            sid = argv[argv.index("--user-sid") + 1] if "--user-sid" in argv[:-1] else ""
            return register(install_dir, sid)
        if flag == "--unregister":
            return unregister(install_dir)
        if flag == "--stop":
            return stop(install_dir)
        if flag == "--start":
            return start(install_dir)
        if flag == "--verify-acl":
            return verify_install_acl(install_dir)
        return 2
    except Exception:
        log.exception("%s failed", argv[:1])
        return 1
    finally:
        if handler is not None:                     # F-226: the log is closed, always
            logging.getLogger().removeHandler(handler)
            handler.close()
