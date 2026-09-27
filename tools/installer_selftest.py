"""tools/installer_selftest.py -- Stage 9 (act 9b R17 / R18 / R19, §2.9-2.11) install, build, CI.

Nothing is installed, registered, signed or downloaded: the Task Scheduler is a fake, the PE
"signature" is synthetic, and every other check reads the repository.
  [1] task registration in the product exe (R17): the task XML (owner SID, logon trigger, hidden,
      no time limit, priority, restart policy, description); two-phase register with verification;
      only OUR tasks are touched (declared, our author tag, or an action inside the install
      directory -- never another "FaceUnlock-*"); the installed and dev declarations agree.
  [2] installer.iss: fixed Program Files directory, MinVersion, no PowerShell anywhere, models by
      consent + pins (/ACCEPTMODELLICENSE fails closed, /MODELZIP), stop through COM/WMI, exit
      codes, a scoped uninstaller (RunOnceId everywhere, no recursive delete of the directory,
      CP key fallback, restart-delete of busy files), the Start-menu AUMID.
  [3] the build: variants, the NVIDIA allowlist, no FFmpeg / TensorRT / sample media, pystray as
      replaceable sources + its license texts, version resources, the PE digest that ignores the
      certificate table (F-240), exact artefact names, the model pins passed to ISCC.
  [4] hashed locks and CI: every lock line hashed; a read-only workflow token, publish-only
      write, no persisted credentials, a draft release, no SignPath, the tag through the
      environment (F-222).
  [5] ORT telemetry off (§2.11), the product ignores FACE_UNLOCK_HOME (F-106), the custody
      self-check needs the build gate (D-73), version 0.2.0 everywhere, dev scripts guard R19.

Run:  python -m tools.installer_selftest      Exit 0 = all green.
"""
from __future__ import annotations

import os
import re
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_inst_")

REPO = Path(__file__).resolve().parents[1]
FAILS: list = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8-sig")


class FakeSched:
    def __init__(self, existing=()):
        self.reg = {n: (a, p) for n, a, p in existing}
        self.calls = []
        self.state_of = {}

    def register(self, name, xml):
        self.calls.append(("register", name))
        m = re.search(r"<Command>(.*?)</Command>", xml)
        self.reg[name] = ("WindowsFaceUnlock", m.group(1) if m else "")

    def tasks(self):
        return [(n, a, p) for n, (a, p) in self.reg.items()]

    def state(self, name):
        return self.state_of.get(name, 3 if name in self.reg else None)

    def stop(self, name):
        self.calls.append(("stop", name))

    def run(self, name):
        self.calls.append(("run", name))

    def delete(self, name):
        self.calls.append(("delete", name))
        self.reg.pop(name, None)


def test_taskreg():
    print("[1] task registration (R17)")
    from face_service import taskreg as T
    inst = r"C:\Program Files\WindowsFaceUnlock"
    sid = "S-1-5-21-1-2-3-1001"
    x = T.task_xml(T.TASKS[0], inst, sid)
    check("XML: owner SID in principal and trigger", x.count(f"<UserId>{sid}</UserId>") == 2)
    check("XML: interactive token, least privilege", "<LogonType>InteractiveToken</LogonType>" in x
          and "<RunLevel>LeastPrivilege</RunLevel>" in x)
    check("XML: hidden, no time limit, priority 5 for the service", "<Hidden>true</Hidden>" in x
          and "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in x and "<Priority>5</Priority>" in x)
    check("XML: description registered (D-118)", "<Description>" in x and "<Author>WindowsFaceUnlock</Author>" in x)
    xt = T.task_xml(T.TASKS[1], inst, sid)
    check("XML: tray restarts on failure 3 x 1 min", "<Count>3</Count>" in xt and "<Interval>PT1M</Interval>" in xt)
    check("XML: paths escaped", "&amp;" in T.task_xml(T.TASKS[0], r"C:\A&B", sid))
    foreign = [("FaceUnlock-Other", "Somebody", r"C:\Other\x.exe"), ("Unrelated", "", r"C:\x.exe"),
               ("FaceUnlock-Old", "WindowsFaceUnlock", r"C:\gone\x.exe"),
               ("Mine-Renamed", "", inst + r"\face_service.exe")]
    got = T.ours(foreign, inst)
    check("only our tasks (F-239)", sorted(got) == ["FaceUnlock-Old", "Mine-Renamed"], got)
    orig = (T.stack_processes, T.kill_and_wait, T.graceful_shutdown, T.time.sleep)
    T.stack_processes = lambda d, own_pid=None: []
    T.kill_and_wait = lambda d, wait_s=10.0: 0
    T.graceful_shutdown = lambda: True
    T.time.sleep = lambda s: None
    try:
        with tempfile.TemporaryDirectory() as td:
            for t in T.TASKS:
                Path(td, t.exe).write_bytes(b"MZ")
            fake = FakeSched(existing=[("FaceUnlock-Other", "Somebody", r"C:\Other\x.exe"),
                                       ("FaceUnlock-Legacy", "", str(Path(td, "face_service.exe")))])
            ok_sid = lambda sid: ("alice", "PC1")
            here = lambda sid: True              # the owner is signed in (V-71 below covers not)
            rc = T.register(td, "S-1-5-21-11-22-33-1001", sched=fake, resolve=ok_sid, logged_on=here)
            check("register: 0 after verification", rc == 0, fake.calls)
            check("register: all three registered, then started",
                  [c for c in fake.calls if c[0] == "register"] == [("register", t.name) for t in T.TASKS]
                  and [c for c in fake.calls if c[0] == "run"] == [("run", t.name) for t in T.TASKS])
            check("register: our orphan removed, a foreign FaceUnlock-* kept",
                  ("delete", "FaceUnlock-Legacy") in fake.calls and ("delete", "FaceUnlock-Other") not in fake.calls)
            check("register: a bad SID touches nothing", T.register(td, "S-1-5-18", sched=FakeSched(), resolve=ok_sid) == 1)
            fake2 = FakeSched()
            fake2.state_of = {"FaceUnlock-Watchdog": 1}
            check("register: a task that is not ready fails the run (F-233)", T.register(td, "S-1-5-21-11-22-33-1001", sched=fake2, resolve=ok_sid, logged_on=here) == 1)
            Path(td, "face_unlock_watchdog.exe").unlink()
            f3 = FakeSched()
            check("phase A: a missing exe -> 1, nothing registered (F-232)",
                  T.register(td, "S-1-5-21-11-22-33-1001", sched=f3, resolve=ok_sid, logged_on=here) == 1
                  and not f3.calls)
            Path(td, "face_unlock_watchdog.exe").write_bytes(b"MZ")
            # 9d (V-71): /OWNER= names a user who is not signed in -- registered, not started, 0
            f5 = FakeSched()

            def _no_run(name):
                raise RuntimeError("0x80070520: a specified logon session does not exist")
            f5.run = _no_run
            import logging as _lg
            recs = []

            class _H(_lg.Handler):
                def emit(self, r):
                    recs.append(r.getMessage())
            _h = _H()
            T.log.addHandler(_h)
            T.log.setLevel(_lg.INFO)
            rc5 = T.register(td, "S-1-5-21-11-22-33-1001", sched=f5, resolve=ok_sid,
                             logged_on=lambda sid: False)
            check("W-37: the V-71 log line names the OWNER (PC1\\alice), not a task (no shadowing)",
                  any("the owner PC1\\alice is not signed in" in m for m in recs), recs[-6:])
            check("V-71: owner without a session -> tasks registered, not started, Ready accepted, 0",
                  rc5 == 0 and [c[0] for c in f5.calls].count("register") == len(T.TASKS)
                  and all(f5.state(t.name) == T.TASK_STATE_READY for t in T.TASKS), (rc5, f5.calls))
            f6 = FakeSched()
            f6.run = _no_run
            check("V-71: owner signed in and a start fails -> still 1",
                  T.register(td, "S-1-5-21-11-22-33-1001", sched=f6, resolve=ok_sid,
                             logged_on=lambda sid: True) == 1)
            # W-37: presence unknown -> the start is tried; a failure is logged and tolerated
            f7 = FakeSched()
            f7.run = _no_run
            del recs[:]
            rc7 = T.register(td, "S-1-5-21-11-22-33-1001", sched=f7, resolve=ok_sid,
                             logged_on=lambda sid: None)
            check("W-37: presence unknown and the start fails -> tried, logged, tolerated: 0",
                  rc7 == 0 and any("presence unknown -- tolerated" in m for m in recs), (rc7, recs[-4:]))
            f8 = FakeSched()
            rc8 = T.register(td, "S-1-5-21-11-22-33-1001", sched=f8, resolve=ok_sid,
                             logged_on=lambda sid: None)
            check("W-37: presence unknown and the start works -> started, 0",
                  rc8 == 0 and [c for c in f8.calls if c[0] == "run"] == [("run", t.name) for t in T.TASKS])
            T.log.removeHandler(_h)
            seen = {}

            class _Wts:
                WTS_CURRENT_SERVER_HANDLE, WTSActive, WTSDisconnected, WTSUserName, WTSDomainName = 0, 0, 4, 5, 7
                sessions = [{"SessionId": 0, "State": 6}, {"SessionId": 2, "State": 0}]
                users = {2: ("Alice", "PC1")}

                def WTSEnumerateSessions(self, h):
                    return self.sessions

                def WTSQuerySessionInformation(self, h, sid, what):
                    u, d = self.users.get(sid, ("", ""))
                    return u if what == self.WTSUserName else d

            class _Sec:
                accounts = {"PC1\\Alice": "S-1-5-21-11-22-33-1001"}

                def ConvertStringSidToSid(self, s):
                    return s

                def LookupAccountName(self, sysname, acct):
                    seen["acct"] = acct
                    if acct not in self.accounts:
                        raise RuntimeError("1332: no mapping")
                    return self.accounts[acct], "PC1", 1

                def EqualSid(self, a, b):
                    return a == b
            w, sx = _Wts(), _Sec()
            check("W-37: the owner found by SID (session user -> LookupAccountName -> EqualSid)",
                  T._owner_logged_on("S-1-5-21-11-22-33-1001", _wts=w, _sec=sx) is True
                  and seen.get("acct") == "PC1\\Alice")
            check("W-37: another signed-in user -> not present",
                  T._owner_logged_on("S-1-5-21-11-22-33-1002", _wts=w, _sec=sx) is False)
            w.users = {2: ("Bob", "PC1")}
            check("W-37: a session user that does not resolve and no match -> unknown (None)",
                  T._owner_logged_on("S-1-5-21-11-22-33-1001", _wts=w, _sec=sx) is None)

            class _WtsBroken(_Wts):
                def WTSEnumerateSessions(self, h):
                    raise OSError("RPC server unavailable")
            check("W-37: sessions cannot be listed -> unknown (None), never an exception",
                  T._owner_logged_on("S-1-5-21-11-22-33-1001", _wts=_WtsBroken(), _sec=sx) is None)
            # the real pywin32 path (it has no win32security.EqualSid: PySID == is EqualSid)
            from face_service import identity as _I
            if os.environ.get("CI"):
                print("  SKIP  W-37 live: no signed-in owner session on a CI runner")
            else:
                check("W-37 live: this signed-in user is present by SID; a made-up SID is not",
                      T._owner_logged_on(_I.current_user_sid()) is True
                      and T._owner_logged_on("S-1-5-21-1-2-3-1001") is False)
            f4 = FakeSched(existing=[(t.name, "WindowsFaceUnlock", str(Path(td, t.exe))) for t in T.TASKS])
            check("unregister removes ours and counts survivors", T.unregister(td, sched=f4) == 0
                  and not f4.reg)
    finally:
        T.stack_processes, T.kill_and_wait, T.graceful_shutdown, T.time.sleep = orig
    # 9d (V-70): the call form proven by the S-1 probe is pinned
    calls = []

    class _Root:
        def RegisterTask(self, *a):
            calls.append(a)
    sch = T.Scheduler.__new__(T.Scheduler)
    sch._root = _Root()
    sch.register("FaceUnlock-Service", "<xml/>")
    check("V-70: RegisterTask(name, xml, TASK_CREATE_OR_UPDATE=6, userId None, password None, "
          "TASK_LOGON_INTERACTIVE_TOKEN=3)", calls == [("FaceUnlock-Service", "<xml/>", 6, None, None, 3)],
          calls)
    psd1 = read("tools/tasks.psd1")
    for t in T.TASKS:
        check(f"dev declaration lists {t.name} with {t.exe} and {t.dev_args}",
              f"'{t.name}'" in psd1 and f"'{t.exe}'" in psd1 and f"'{t.dev_args}'" in psd1)
    router = read("presence_monitor/__main__.py")
    check("the tray exe routes --register/--unregister/--stop/--verify-acl",
          all(f'"{f}"' in router for f in ("--register", "--unregister", "--stop", "--verify-acl")))


class _FakeProc:
    def __init__(self, world, pid, name, exe):
        self.pid, self.info, self._w = pid, {"pid": pid, "name": name, "exe": exe}, world

    def kill(self):
        self._w.kills.append(self.info["exe"])
        if self.info["name"] in ("Registry", "MemCompression"):
            raise PermissionError(f"psutil.AccessDenied(pid={self.pid})")
        self._w.alive.discard(self.pid)


class _FakePsutil:
    """Just what taskreg uses: process_iter (attrs ignored: every info key is there), wait_procs."""

    def __init__(self, procs):
        self.procs = [_FakeProc(self, *p) for p in procs]
        self.alive = {p.pid for p in self.procs}
        self.kills = []

    def process_iter(self, attrs=None):
        return [p for p in self.procs if p.pid in self.alive]

    def wait_procs(self, procs, timeout=None):
        return ([p for p in procs if p.pid not in self.alive], [p for p in procs if p.pid in self.alive])


def test_f1_foreign():
    """9e (F1-01..F1-05): Setup runs the tray exe with the install directory as its CURRENT
    directory. Nothing that is not a fully qualified path inside it may be ours: not the OneDrive
    tasks ("%localappdata%\\..."), not cmd.exe / powershell.exe / rundll32.exe, not "C:x.exe", not the
    processes psutil reports as "Registry" / "MemCompression" (live 9e run on 325cb01)."""
    print("[1b] F1: foreign tasks and processes with the install directory as the current directory")
    import logging as _lg
    import shutil
    from face_service import taskreg as T
    here = os.getcwd()
    td = ""
    saved = (T.graceful_shutdown, T.time.sleep, sys.modules.get("psutil"))
    recs = []

    class _H(_lg.Handler):
        def emit(self, r):
            recs.append(r.getMessage())
    h = _H()
    T.log.addHandler(h)
    T.log.setLevel(_lg.INFO)
    try:
        td = tempfile.mkdtemp(prefix="fu_f1_inst_")
        inst = os.path.join(td, "WindowsFaceUnlock")
        os.makedirs(inst)
        for t in T.TASKS:
            Path(inst, t.exe).write_bytes(b"MZ")
        ms = "Microsoft Corporation"
        sid_tail = "S-1-5-21-11-22-33-1001"
        drive_rel = inst[:2] + "x.exe"                  # "C:x.exe" -- drive-relative
        foreign = [
            (f"OneDrive Reporting Task-{sid_tail}", ms,
             r"%localappdata%\Microsoft\OneDrive\OneDriveStandaloneUpdater.exe"),
            (f"OneDrive Standalone Update Task-{sid_tail}", ms,
             r"%localappdata%\Microsoft\OneDrive\OneDriveStandaloneUpdater.exe"),
            ("F1 cmd", "", "cmd.exe"), ("F1 powershell", "", "powershell.exe"),
            ("F1 rundll32", "", "rundll32.exe"), ("F1 parent", "", r"..\x.exe"),
            ("F1 drive-relative", "", drive_rel), ("F1 root-relative", "", r"\Windows\x.exe"),
            ("F1 unc", "", r"\\server\share\x.exe"), ("F1 empty", "", ""),
            ("F1 dotdot", "", inst + r"\..\..\Windows\System32\cmd.exe"),
            ("F1 foreign absolute", "Somebody", r"C:\Other\Tools\x.exe"),
        ]
        mine = [(t.name, T.AUTHOR, os.path.join(inst, t.exe)) for t in T.TASKS]
        orphans = [("FaceUnlock-Old", T.AUTHOR, r"C:\gone\x.exe"),
                   ("Mine-Renamed", "", os.path.join(inst, "face_service.exe"))]
        every = foreign + mine + orphans
        want_ours = sorted(n for n, _a, _p in mine + orphans)
        base = 7_000_000
        procs = [(base + 1, "Registry", "Registry"), (base + 2, "MemCompression", "MemCompression"),
                 (base + 3, "System", ""), (base + 4, "face_service.exe", "face_service.exe"),
                 (base + 5, "face_unlock_tray.exe", drive_rel.replace("x.exe", "face_unlock_tray.exe")),
                 (base + 6, "face_service.exe", r"C:\Other\WindowsFaceUnlock\face_service.exe"),
                 (base + 7, "unins000.exe", os.path.join(inst, "unins000.exe"))]
        our_procs = [(base + 11 + i, t.exe, os.path.join(inst, t.exe)) for i, t in enumerate(T.TASKS)]
        our_exes = sorted(p[2] for p in our_procs)
        os.chdir(inst)                                  # as Setup runs the tray exe (WorkingDir={app})
        check("F1-05: the current directory IS the install directory",
              os.path.normcase(os.getcwd()) == os.path.normcase(inst))
        got = sorted(T.ours(every, inst))
        check("F1-01: ours() = our three + the two orphans (author tag / full path inside)",
              got == want_ours, got)
        for n, a, p in foreign:
            check(f"F1-01: not ours: {n} -> {p!r}", n not in got)
        fake_ps = _FakePsutil(procs + our_procs)
        sys.modules["psutil"] = fake_ps
        gotp = sorted(p.info["exe"] for p in T.stack_processes(inst, own_pid=1))
        check("F1-02: stack processes = exactly our three (not Registry / MemCompression / empty / "
              "relative / C:x / another folder's face_service.exe / unins000.exe)", gotp == our_exes, gotp)

        def _graceful():                                # the service accepts the pipe shutdown
            fake_ps.alive.discard(our_procs[0][0])
            return True
        T.graceful_shutdown = _graceful
        T.time.sleep = lambda s: None
        fake = FakeSched(existing=every)
        del recs[:]
        rc = T.register(inst, sid_tail, sched=fake, resolve=lambda s: ("alice", "PC1"),
                        logged_on=lambda s: True)
        deleted = sorted(c[1] for c in fake.calls if c[0] == "delete")
        stopped = sorted(c[1] for c in fake.calls if c[0] == "stop")
        check("F1-03: register() deletes only the two orphans", deleted == ["FaceUnlock-Old", "Mine-Renamed"],
              deleted)
        check("F1-03: register() stops only our three", stopped == sorted(t.name for t in T.TASKS), stopped)
        check("F1-02: register() kills only our processes (never Registry / MemCompression)",
              sorted(fake_ps.kills) == sorted(p[2] for p in our_procs[1:]), fake_ps.kills)
        check("F1: register() -> 0, no survivor warning", rc == 0 and not any("survived" in m for m in recs),
              (rc, [m for m in recs if "survived" in m or "kill" in m]))
        check("F1: every foreign task is still registered after register()",
              all(n in fake.reg for n, _a, _p in foreign))
        check("F1-03: the orphan removal logs the task's author and action",
              any("removed our orphan task FaceUnlock-Old" in m and r"C:\\gone\\x.exe" in m for m in recs)
              and any("removed our orphan task Mine-Renamed" in m and "face_service.exe" in m for m in recs),
              [m for m in recs if "orphan" in m])
        check("F1-03: every stop logs the task and its action",
              all(any(f"stopping task {t.name}" in m and t.exe in m for m in recs) for t in T.TASKS),
              [m for m in recs if "stop" in m])
        fake_ps2 = _FakePsutil(procs + our_procs)
        sys.modules["psutil"] = fake_ps2
        T.graceful_shutdown = lambda: (fake_ps2.alive.discard(our_procs[0][0]), True)[1]
        fake2 = FakeSched(existing=every)
        rc2 = T.unregister(inst, sched=fake2)
        deleted2 = sorted(c[1] for c in fake2.calls if c[0] == "delete")
        check("F1-03: unregister() deletes only ours (our three + the two orphans)",
              deleted2 == want_ours, deleted2)
        check("F1-03: unregister() stops only ours",
              set(c[1] for c in fake2.calls if c[0] == "stop") <= set(want_ours),
              [c for c in fake2.calls if c[0] == "stop"])
        check("F1-02: unregister() kills only our processes", sorted(fake_ps2.kills)
              == sorted(p[2] for p in our_procs[1:]) and rc2 == 0, (rc2, fake_ps2.kills))
        fake_ps3 = _FakePsutil(procs + our_procs)
        sys.modules["psutil"] = fake_ps3
        T.graceful_shutdown = lambda: (fake_ps3.alive.discard(our_procs[0][0]), True)[1]
        fake3 = FakeSched(existing=every)
        rc3 = T.stop(inst, sched=fake3)
        check("F1: stop() stops only ours and kills only our processes",
              set(c[1] for c in fake3.calls if c[0] == "stop") <= set(want_ours) and rc3 == 0
              and sorted(fake_ps3.kills) == sorted(p[2] for p in our_procs[1:]),
              (rc3, fake3.calls, fake_ps3.kills))
        # an install directory that is not a full path touches nothing (it would follow the cwd)
        os.chdir(td)
        f4 = FakeSched(existing=every)
        rc4 = T.register("WindowsFaceUnlock", sid_tail, sched=f4, resolve=lambda s: ("alice", "PC1"),
                         logged_on=lambda s: True)
        rc5 = T.unregister("WindowsFaceUnlock", sched=f4)
        check("F1-01: a relative install directory -> 1 and nothing registered, stopped or deleted",
              rc4 == 1 and rc5 == 1 and not f4.calls, (rc4, rc5, f4.calls))
    finally:
        os.chdir(here)
        if td:
            shutil.rmtree(td, ignore_errors=True)
        T.graceful_shutdown, T.time.sleep = saved[0], saved[1]
        if saved[2] is None:
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = saved[2]
        T.log.removeHandler(h)


def test_iss():
    print("[2] installer.iss (R17, R7, R14)")
    s = read("installer/installer.iss")
    code = "\n".join(ln for ln in s.splitlines() if not ln.lstrip().startswith(";"))
    # 9d (V-62..V-66)
    prep = code.split("function PrepareToInstall", 1)[1].split("\nend;", 1)[0]
    nbc = code.split("function NextButtonClick", 1)[1].split("\nend;", 1)[0]
    check("V-62: the interactive download runs only when not silent (NextButtonClick runs silent too)",
          "and not WizardSilent()" in nbc and "DownloadPage.Download" in nbc)
    check("V-62: the silent download lives in PrepareToInstall and returns no error string",
          "and WizardSilent()" in prep and "ModelsDownloadError := GetExceptionMessage" in prep
          and "Result := FmtMessage(CustomMessage('ModelsDownloadFailed')" not in prep)
    check("V-62: a failed silent download ends with 23 through Fail (no message box when silent)",
          "if ModelsDownloadError <> '' then\n      Fail(23," in code
          and "if not WizardSilent() then\n    MsgBox(Msg" in code)
    reg = code.split("procedure RegisterProvider", 1)[1].split("\nend;", 1)[0]
    acl = reg.split("if not AclOk then", 1)[1].split("exit;", 1)[0]
    check("W-36: the GPU hint for the NVIDIA wheels installs with --no-deps",
          "--no-deps -r requirements-gpu.lock (F-219" in read("installer/build.py").replace('"\n                         "', ""))
    check("V-63: an ACL failure unregisters the old provider (regsvr32 /u + keys) BEFORE code 22",
          acl.find("/u /s") < acl.find("RegDeleteKeyIncludingSubkeys") < acl.find("Fail(22")
          and acl.count("RegDeleteKeyIncludingSubkeys") == 2)
    step = code.split("procedure CurStepChanged", 1)[1].split("if CurStep = ssPostInstall", 1)[0]
    check("V-64: {app} is checked against the fixed folder at ssInstall (covers /LOADINF Dir=)",
          "CurStep = ssInstall" in step and "ExpandConstant('{app}')" in step and "ExpectedAppDir()" in step
          and "Abort;" in step)
    check("V-66: the 0.1.x Start-menu shortcuts are removed",
          all(f'Type: files; Name: "{{group}}\\{{#MyAppName}}{x}.lnk"' in s
              for x in ("", " - Uninstall", " — Uninstall")))
    for ps_rel in ("tools/uninstall.ps1", "tools/clean_restart.ps1"):
        ps = read(ps_rel)
        check(f"V-65: {ps_rel} runs the windowed tray exe with Start-Process -Wait -PassThru",
              "-Wait -PassThru" in ps and not re.search(r"^\s*&\s*\$fu(Tray)?Exe\b", ps, re.M))
    ins = read("INSTALL.md")
    check("V-62: INSTALL.md lists the exit codes (1, 3, 7, 21, 22, 23)",
          all(f"| {c} |" in ins for c in (1, 3, 7, 21, 22, 23)))
    check("no PowerShell in install or uninstall", "powershell.exe" not in code.lower()
          and "-executionpolicy" not in code.lower()
          and not re.search(r"(Filename|Exec\w*)\W[^\n]*\.ps1", code))
    check("fixed directory", "UsePreviousAppDir=no" in s and "DisableDirPage=yes" in s
          and "DirRefused" in s and r"DefaultDirName={autopf}\{#MyAppShortName}" in s)
    check("MinVersion 10.0.19045 + a warning below 26100", "MinVersion=10.0.19045" in s and "26100" in s)
    check("x64 OS only", "ArchitecturesAllowed=x64os" in s)
    check("models: consent page, silent flag, offline zip, pinned hashes",
          "/ACCEPTMODELLICENSE" in s and "/MODELZIP" in s and "{#BuffaloSHA256}" in s
          and "ModelsValidIn" in s and "ArchiveExtraction=full" in s)
    check("stop through COM / WMI", "Schedule.Service" in s and "WbemScripting.SWbemLocator" in s)
    check("registration through the product exe", "--register --user-sid" in s
          and '"--unregister"' in s and "--verify-acl" in s)
    check("exit codes for a failed silent install", "GetCustomSetupExitCode" in s and "Fail(21" in s)
    runs = re.findall(r"^Filename:.*$(?:\n  .*)*", s.split("[UninstallRun]", 1)[1].split("[UninstallDelete]", 1)[0], re.M)
    check("RunOnceId on every [UninstallRun] entry", runs and all("RunOnceId" in r for r in runs), runs)
    ud = s.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
    check("no recursive delete of the install directory (F-209)",
          'Name: "{app}"' in ud and 'Type: filesandordirs; Name: "{app}"' not in ud)
    check("CP keys removed as a fallback", "uninsdeletekey dontcreatekey" in s)
    check("busy files handled", "uninsrestartdelete" in s and "restartreplace" in s)
    check("Start-menu AUMID", 'AppUserModelID: "WindowsFaceUnlock.Tray"' in s)
    check("no data removal without a recorded owner (F-216)", "(UninstallUserSid <> '')" in s)
    unst = code.split("procedure CurUninstallStepChanged", 1)[1].split("\nend;", 1)[0]
    check("W-06: the uninstaller finds the owner's data by the CANONICAL SID (leading zeros)",
          "DataDirFor(CanonicalSid(UninstallUserSid))" in unst
          and "DataDirFor(UninstallUserSid)" not in unst)
    check("enroll offered unchecked when a profile exists (F-192)", "EnrollmentExists" in s)
    en = read("installer/lang/en.isl")
    ru = read("installer/lang/ru.isl")
    keys = lambda t: {ln.split("=", 1)[0] for ln in t.splitlines() if "=" in ln and not ln.startswith(";")}
    used = set(re.findall(r"\{cm:(\w+)\}", s)) | set(re.findall(r"CustomMessage\('(\w+)'\)", s))
    check("every message used exists in EN and RU", used <= keys(en) and keys(en) == keys(ru), used - keys(en))
    check("the InsightFace terms are quoted on the page", "non-commercial research purposes only" in en)
    # 9d-r2 (W-35): one wording everywhere -- the README of the PINNED insightface 1.0.1 on PyPI
    models_q = "The pretrained models we provided with this library are available for non-commercial research purposes only, including both auto-downloading models and manual-downloading models."
    link = "https://pypi.org/project/insightface/1.0.1/"
    notices = read("THIRD_PARTY_NOTICES.md")
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("_notices", REPO / "installer" / "notices.py")
    _N = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_N)
    check("W-35: THIRD_PARTY_NOTICES, notices.py and en.isl quote the 1.0.1 README with its PyPI link",
          all(models_q in x and link in x for x in (notices, _N.INSIGHTFACE_TEXT, en)))
    check("W-35: ru.isl carries a translation marked as such, the original and the link",
          "(перевод)" in ru and models_q in ru and link in ru)
    check("W-35: the requirements pin insightface 1.0.1 (the quoted README's version)",
          "insightface==1.0.1" in read("requirements.lock") and "insightface==1.0.1" in read("requirements-gpu.lock"))
    check("W-35: the ONNX Runtime row names the CPU package and licenses\\onnxruntime\\",
          "package `onnxruntime`" in notices and "licenses\\onnxruntime\\` (CPU variant)" in notices)


CP_CLSID = "{8414D7B6-D536-461B-B31B-ADF77B3A8974}"


def test_iss_preprocessed():
    """9d-r2 (W-30): the key names the fallback deletes, as ISPP hands them to the compiler --
    read from the PREPROCESSED script (ISPP SaveToFile), not grepped from the source."""
    print("[2b] installer.iss after the preprocessor (W-30)")
    import shutil
    import subprocess
    sys.path.insert(0, str(REPO / "installer"))
    import build as B
    try:
        iscc = B.find_iscc()
    except Exception as e:
        print(f"  SKIP  ISCC not found ({e}) -- the preprocessed check needs Inno Setup")
        return
    work = Path(tempfile.mkdtemp(prefix="fu_ispp_"))
    try:
        shutil.copytree(REPO / "installer", work / "installer",
                        ignore=shutil.ignore_patterns("__pycache__", "*.exe"))
        iss = work / "installer" / "installer.iss"
        pp = work / "installer" / "installer.pp.iss"
        src = iss.read_text(encoding="utf-8")
        # forward slashes: ISPP string literals take a backslash as it is or as an escape
        # depending on its parser options -- a path with "/" means the same either way
        iss.write_text(src + '\n#expr SaveToFile("' + pp.as_posix() + '")\n', encoding="utf-8")
        r = subprocess.run([iscc, "/O-", "/Q", *B.iscc_defines("cpu"), str(iss)],
                           capture_output=True, text=True, timeout=300)
        check("W-30: ISPP wrote the preprocessed script", pp.is_file(),
              (r.returncode, (r.stdout + r.stderr)[-600:]))
        if not pp.is_file():
            return
        out = pp.read_text(encoding="utf-8-sig", errors="replace")
        code = out.split("[Code]", 1)[1]
        reg = code.split("procedure RegisterProvider", 1)[1].split("\nend;", 1)[0]
        keys = re.findall(r"RegDeleteKeyIncludingSubkeys\(HKLM64,\s*'([^']*)'\)", reg)
        want = [r"SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers" + "\\" + CP_CLSID,
                r"SOFTWARE\Classes\CLSID" + "\\" + CP_CLSID]
        check("W-30: the fallback deletes exactly the provider's two keys, single braces",
              keys == want, keys)
        check("W-30: no '{{' reaches a [Code] string", "{{" not in code.split("procedure RegisterProvider", 1)[1]
              .split("\nend;", 1)[0])
        sect = out.split("[Code]", 1)[0]
        check("W-30: the [Registry] entries keep the escaped '{{' (a constant brace in a section)",
              sect.count("{{" + CP_CLSID[1:]) == 2, sect.count("{{" + CP_CLSID[1:]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _pascal_function(src: str, name: str) -> "str | None":
    """The text of ``function <name>(`` ... the first column-0 ``end;`` of installer.iss."""
    m = re.search(r"^function " + re.escape(name) + r"\(.*?^end;[ \t]*$", src, re.M | re.S)
    return m.group(0) if m else None


def test_iss_pascal_paths():
    """9e (F1-01 / F1-04): the installer's OWN Pascal path checks, executed: IsFullLocalPath,
    PathUnder and DataDirRemovable are cut out of installer.iss verbatim, compiled by ISCC into a
    probe whose InitializeSetup runs the cases, writes the answers and returns False (the probe
    installs nothing: no files, no uninstaller, lowest privileges)."""
    print("[2c] installer.iss path checks, compiled and run (F1-01, F1-04)")
    import shutil
    import subprocess
    sys.path.insert(0, str(REPO / "installer"))
    import build as B
    src = read("installer/installer.iss")
    code = src.split("[Code]", 1)[1]
    names = ("IsFullLocalPath", "PathUnder", "DataDirRemovable")
    funcs = {n: _pascal_function(code, n) for n in names}
    for n in names:
        check(f"F1: installer.iss defines {n}", funcs[n] is not None)
    unst = code.split("procedure CurUninstallStepChanged", 1)[1].split("\nend;", 1)[0]
    i_guard, i_ask, i_rm = unst.find("DataDirRemovable("), unst.find("WantsDataRemoved("), unst.find("rmdir")
    i_exists = unst.find("DirExists(DataDir)")
    check("F1-04: the uninstaller checks DataDirRemovable before it asks, looks (DirExists) or runs rmdir",
          -1 < i_guard < min(i_ask, i_rm, i_exists), (i_guard, i_ask, i_exists, i_rm))
    check("F1-04: ... and the profile it compares with is the owner's (canonical SID)",
          "DataDirRemovable(DataDir, ProfileDirOf(CanonicalSid(UninstallUserSid)))" in unst)
    try:
        iscc = B.find_iscc()
    except Exception as e:
        print(f"  SKIP  ISCC not found ({e}) -- the compiled probe needs Inno Setup")
        return
    d = r"C:\Program Files\WindowsFaceUnlock"
    under_cases = [  # (path, dir, expected)
        (d + r"\face_service.exe", d, True), (d.lower() + r"\FACE_UNLOCK_TRAY.EXE", d, True),
        ("", d, False), ("cmd.exe", d, False), (r"..\x.exe", d, False), ("C:x.exe", d, False),
        (r"\Windows\x.exe", d, False), (r"\\server\share\x.exe", d, False),
        ("\\\\?\\" + d + r"\face_service.exe", d, False),
        (r"%localappdata%\Microsoft\OneDrive\OneDriveStandaloneUpdater.exe", d, False),
        ('"' + d + r'\face_service.exe"', d, False), (d + r"\..\..\Windows\System32\cmd.exe", d, False),
        (d + r"\.\face_service.exe", d, False), (d + r"\face_service.exe --x", d, False),
        (d + "/face_service.exe", d, False), (d + r"X\a.exe", d, False), (d, d, False),
        (r"WindowsFaceUnlock\face_service.exe", "WindowsFaceUnlock", False),
        (r"C:WindowsFaceUnlock\face_service.exe", "C:WindowsFaceUnlock", False),
    ]
    data_cases = [  # (data dir, profile, expected)
        (r"C:\Users\alice\.face-unlock", r"C:\Users\alice", True),
        (r"D:\Profiles\O'Brien\.face-unlock", r"D:\Profiles\O'Brien", True),
        ("", "", False), (r"Users\alice\.face-unlock", r"Users\alice", False),
        (r"%USERPROFILE%\.face-unlock", "%USERPROFILE%", False),
        (r"\\server\home\alice\.face-unlock", r"\\server\home\alice", False),
        (r"C:\\.face-unlock", "C:\\", False), (r"C:\.face-unlock", "C:", False),
        (r"C:\Users\alice\..\bob\.face-unlock", r"C:\Users\alice\..\bob", False),
        (r"C:\Users\alice\.face-unlock", r"C:\Users\bob", False),
        (r'C:\Users\al"ice\.face-unlock', r'C:\Users\al"ice', False),
        (r"C:\Users\alice\Documents", r"C:\Users\alice", False),
        (r"C:\Users\alice\.face-unlock", r"C:\Users\alice\\", False),
    ]
    work = Path(tempfile.mkdtemp(prefix="fu_f1_pas_"))
    try:
        out = work / "answers.txt"
        q = lambda s: "'" + s.replace("'", "''") + "'"
        lines = []
        if funcs["PathUnder"]:
            lines += [f"  S := S + 'U{i}=' + B(PathUnder({q(p)}, {q(dd)})) + #13#10;"
                      for i, (p, dd, _w) in enumerate(under_cases)]
        if funcs["DataDirRemovable"]:
            lines += [f"  S := S + 'D{i}=' + B(DataDirRemovable({q(p)}, {q(pr)})) + #13#10;"
                      for i, (p, pr, _w) in enumerate(data_cases)]
        body = "\n\n".join(f for f in (funcs[n] for n in names) if f)
        probe = (
            "[Setup]\nAppName=F1PathProbe\nAppVersion=1\nCreateAppDir=no\nUninstallable=no\n"
            "PrivilegesRequired=lowest\nOutputDir=" + str(work) + "\nOutputBaseFilename=f1probe\n"
            "\n[Code]\n" + body + "\n\n"
            "function B(V: Boolean): string;\nbegin\n  if V then Result := '1' else Result := '0';\nend;\n\n"
            "function InitializeSetup(): Boolean;\nvar\n  S: string;\nbegin\n  S := '';\n"
            + "\n".join(lines) + "\n"
            "  SaveStringToFile(" + q(str(out)) + ", S, False);\n  Result := False;\nend;\n")
        (work / "f1probe.iss").write_text(probe, encoding="utf-8-sig")
        r = subprocess.run([iscc, "/Q", str(work / "f1probe.iss")], capture_output=True, text=True, timeout=300)
        exe = work / "f1probe.exe"
        check("F1: the probe compiles", exe.is_file(), (r.returncode, (r.stdout + r.stderr)[-800:]))
        if not exe.is_file():
            return
        subprocess.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=120)
        ans = dict(ln.split("=", 1) for ln in out.read_text(encoding="utf-8", errors="replace").split()
                   if "=" in ln) if out.is_file() else {}
        check("F1: the probe answered", bool(ans))
        if funcs["PathUnder"]:
            for i, (p, dd, w) in enumerate(under_cases):
                check(f"F1-04: PathUnder({p!r}, {dd!r}) = {w}", ans.get(f"U{i}") == ("1" if w else "0"),
                      ans.get(f"U{i}"))
        if funcs["DataDirRemovable"]:
            for i, (p, pr, w) in enumerate(data_cases):
                check(f"F1-04: DataDirRemovable({p!r}, {pr!r}) = {w}", ans.get(f"D{i}") == ("1" if w else "0"),
                      ans.get(f"D{i}"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_uninstall_ps1_home():
    """9e (F1-04): the dev uninstaller uses FACE_UNLOCK_HOME only when it is a full local path
    (-RemoveData deletes what it names). Its own Test-FuFullPath is cut out of the script with the
    PowerShell parser and run; the guard sits before the first use of the directory."""
    print("[2d] tools/uninstall.ps1: FACE_UNLOCK_HOME must be a full local path (F1-04)")
    import json
    import shutil
    import subprocess
    s = read("tools/uninstall.ps1")
    i_guard = s.find("-not (Test-FuFullPath $env:FACE_UNLOCK_HOME)")
    i_use = s.find("$fuDataDir = ")
    check("F1-04: the guard (exit 1) precedes the data directory's first use",
          -1 < i_guard < i_use and "exit 1" in s[i_guard:i_use], (i_guard, i_use))
    cases = [(r"C:\Users\alice\.face-unlock", True), (r"D:\x\home", True), ("", False), ("home", False),
             (r".\home", False), (r"..\home", False), ("C:home", False), (r"\Users\x", False),
             (r"\\server\share\home", False), (r"%TEMP%\home", False), (r'"C:\x"', False),
             (r"C:\x\..\y", False), (r" C:\x", False), (r"C:\x -Force", False)]
    work = Path(tempfile.mkdtemp(prefix="fu_f1_ps_"))
    try:
        (work / "cases.json").write_text(json.dumps([{"i": i, "p": p} for i, (p, _w) in enumerate(cases)]),
                                         encoding="utf-8")
        (work / "probe.ps1").write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            + "'" + str(REPO / "tools" / "uninstall.ps1") + "', [ref]$null, [ref]$null)\n"
            "$f = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]"
            " -and $n.Name -eq 'Test-FuFullPath' }, $true)\n"
            "if (-not $f) { 'MISSING'; exit 3 }\n"
            ". ([scriptblock]::Create($f.Extent.Text))\n"
            "foreach ($c in (Get-Content -Raw -LiteralPath '" + str(work / "cases.json") + "' | ConvertFrom-Json)) {\n"
            "    '{0}={1}' -f $c.i, [int](Test-FuFullPath $c.p)\n}\n", encoding="utf-8")
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(work / "probe.ps1")], capture_output=True, text=True, timeout=120)
        ans = dict(ln.split("=", 1) for ln in r.stdout.split() if "=" in ln)
        check("F1-04: uninstall.ps1 defines Test-FuFullPath", r.returncode == 0 and "MISSING" not in r.stdout,
              (r.returncode, r.stdout[-300:], r.stderr[-300:]))
        for i, (p, w) in enumerate(cases):
            check(f"F1-04: Test-FuFullPath({p!r}) = {w}", ans.get(str(i)) == ("1" if w else "0"), ans.get(str(i)))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pe_with_cert(body: bytes) -> "tuple[bytes, bytes]":
    """A minimal PE32+ header + body, and the same file 'signed': checksum set, security directory
    pointing at an appended certificate table after 8-byte alignment padding."""
    hdr = bytearray(0x200)
    struct.pack_into("<I", hdr, 0x3C, 0x80)
    hdr[0x80:0x84] = b"PE\0\0"
    opt = 0x80 + 24
    struct.pack_into("<H", hdr, opt, 0x20B)
    unsigned = bytes(hdr) + body
    signed = bytearray(unsigned)
    pad = (-len(signed)) % 8
    signed += b"\0" * pad
    cert_off = len(signed)
    cert = b"CERTIFICATE-TABLE" * 5
    signed += cert
    struct.pack_into("<I", signed, opt + 64, 0x1234ABCD)
    struct.pack_into("<II", signed, opt + 112 + 4 * 8, cert_off, len(cert))
    return unsigned, bytes(signed)


def test_build():
    print("[3] the build (R17, R18)")
    spec = read("installer/windows_face_unlock.spec")
    allow = spec.split("NVIDIA_ALLOWLIST", 1)[1].split("}", 1)[0]
    check("NVIDIA allowlist: no nvJitLink / curand / cufftw / nvblas / nvrtc.alt",
          not any(n in allow for n in ("nvJitLink", "curand", "cufftw", "nvblas", ".alt.dll")))
    check("NVIDIA files only in the GPU variant", 'if VARIANT == "gpu":' in spec and "raise SystemExit" in spec)
    check("no FFmpeg, no TensorRT provider", "opencv_videoio_ffmpeg" in spec and "providers_tensorrt" in spec)
    check("no sample media", 'excludes=["data/images/**", "gui/**"]' in spec and '"skimage", excludes=["data/**"]' in spec)
    check("the binary filter runs on every Analysis' final TOCs, not only on BINARIES (9c-BUILD)",
          "_analysis.binaries = [b for b in _analysis.binaries if _keep_binary(b)]" in spec
          and "_analysis.datas = [d for d in _analysis.datas if _keep_binary(d)]" in spec
          and spec.index("for _analysis in") < spec.index("coll = COLLECT("))
    sys.path.insert(0, str(REPO / "installer"))
    import build as _build
    allow = _build.nvidia_allowlist(spec)
    check("the gate reads the spec's NVIDIA allowlist (16 DLLs, cudart..cuDNN; not nvJitLink/curand)",
          len(allow) == 16 and "cudart64_12.dll" in allow and "cudnn_ops64_9.dll" in allow
          and not any("nvJitLink" in n or "curand" in n for n in allow), sorted(allow))
    check("pystray collected as .py (LGPL, F-261)", 'COLLECTION_MODE = {"pystray": "py"}' in spec
          and spec.count("module_collection_mode=COLLECTION_MODE") == 3)
    check("unused packages excluded (F-51)", all(f'"{m}"' in spec for m in ("pandas", "sympy", "pip", "setuptools")))
    check("version resources on all three exes", spec.count("version=_version_info(") == 3)
    check("no dead cipher option (D-109)", "cipher=" not in spec)
    sys.path.insert(0, str(REPO / "tools"))
    import verify_frozen_entrypoints as V
    unsigned, signed = pe_with_cert(os.urandom(4096))
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td, "a.pyd"), Path(td, "b.pyd")
        a.write_bytes(unsigned)
        b.write_bytes(signed)
        check("PE digest ignores the certificate table, the entry and the checksum (F-240)",
              V.pe_content_digest(a) == V.pe_content_digest(b))
        c = Path(td, "c.pyd")
        tampered = bytearray(signed)
        tampered[0x300] ^= 0xFF
        c.write_bytes(bytes(tampered))
        check("...but not a changed byte of the body", V.pe_content_digest(c) != V.pe_content_digest(a))
    import importlib.util
    spec_b = importlib.util.spec_from_file_location("fu_build", REPO / "installer" / "build.py")
    B = importlib.util.module_from_spec(spec_b)
    spec_b.loader.exec_module(B)
    check("artefact names carry version and variant", B.artefact_name("cpu") == f"WindowsFaceUnlock-Setup-{B.VERSION}-cpu.exe")
    d = B.iscc_defines("gpu")
    from face_service.model_pins import BUFFALO_ZIP_SHA256
    check("the model pins travel to ISCC from model_pins", f"/DBuffaloSHA256={BUFFALO_ZIP_SHA256}" in d
          and "/DVariant=gpu" in d and sum(x.startswith("/DModelSHA") for x in d) == 5)
    src = read("installer/build.py")
    check("signing is loudly skipped without credentials", "SIGNING SKIPPED" in src and "AZURE_TENANT_ID" in src)
    check("NVIDIA files are never re-signed", "never NVIDIA's" in src and "nvidia = {f.resolve()" in src)
    check("no dev thumbprint in the build (D-155, F-244)", "13F9BB62" not in src and "SIGN_CP" not in src)


def test_locks_ci():
    print("[4] hashed locks and CI (R18)")
    for rel in ("requirements.lock", "requirements-gpu.lock", "installer/requirements-build.txt"):
        text = read(rel)
        pins = re.findall(r"^[A-Za-z0-9_.\-]+==\S+ \\$", text, re.M)
        blocks = re.split(r"\n(?=[A-Za-z0-9_.\-]+==)", text.split("\n", 5)[-1])
        check(f"{rel}: every pin hashed", pins and all("--hash=sha256:" in b for b in blocks if "==" in b), len(pins))
    check("the GPU lock adds the NVIDIA wheels", "nvidia-cudnn-cu12==" in read("requirements-gpu.lock")
          and "nvidia-" not in read("requirements.lock"))
    # 9d-r2 (W-38): charset-normalizer 3.4.8 was yanked on PyPI; both locks pin 3.5.1 (the newest
    # release requests' charset_normalizer<4 allows), with every file hash PyPI publishes for it
    for rel in ("requirements.lock", "requirements-gpu.lock"):
        block = read(rel).split("charset-normalizer==", 1)[1].split("\n", 1)
        n_hashes = block[1].split("\n" + next(ln for ln in block[1].splitlines()
                                             if ln and not ln.startswith(" ")) + "\n", 1)[0].count("--hash=")
        check(f"W-38: {rel} pins charset-normalizer 3.5.1 (not the yanked 3.4.8), {n_hashes} hashes",
              block[0].startswith("3.5.1 ") and "charset-normalizer==3.4.8" not in read(rel) and n_hashes == 172)
    y = read(".github/workflows/release.yml")
    top = y.split("jobs:", 1)[0]
    check("workflow token read-only by default", "permissions:\n  contents: read" in top)
    jobs = y.split("\njobs:", 1)[1]
    check("only publish can write", jobs.count("contents: write") == 1
          and "contents: write" in jobs.split("\n  publish:", 1)[1])
    check("no persisted checkout credentials", y.count("actions/checkout@") == y.count("persist-credentials: false"))
    check("installs are hash-checked", y.count("--require-hashes") >= 4)
    # 9d (A-6, V-60): the CPU lock carries onnxruntime, the GPU lock onnxruntime-gpu with --no-deps,
    # and every install is followed by tools/lock_check.py
    cpu, gpu = read("requirements.lock"), read("requirements-gpu.lock")
    check("V-60: requirements.lock pins onnxruntime (CPU), not onnxruntime-gpu",
          re.search(r"^onnxruntime==1\.26\.0 ", cpu, re.M) and "onnxruntime-gpu" not in cpu)
    check("V-60: requirements-gpu.lock pins onnxruntime-gpu, not onnxruntime",
          re.search(r"^onnxruntime-gpu==1\.26\.0 ", gpu, re.M) and not re.search(r"^onnxruntime==", gpu, re.M))
    check("V-60: the GPU installs use --no-deps and every job runs lock_check",
          y.count("--no-deps -r $lock") == 2 and y.count("tools.lock_check") == 3)
    ps = read("setup.ps1")
    check("V-60: setup.ps1 -Gpu installs with --no-deps and checks with lock_check",
          "--no-deps -r $lock" in ps and "lock_check" in ps)
    check("W-31: setup.ps1 runs lock_check by its absolute path (any current directory)",
          '& $py "$root\\tools\\lock_check.py" --lock $lock' in ps and "-m tools.lock_check" not in ps)
    import subprocess as _sp
    with tempfile.TemporaryDirectory() as _td:
        _r = _sp.run([sys.executable, str(REPO / "tools" / "lock_check.py"), "--help"], cwd=_td,
                     capture_output=True, text=True, timeout=60)
    check("W-31: tools/lock_check.py runs as a script from another directory", _r.returncode == 0
          and "--lock" in _r.stdout, (_r.returncode, _r.stderr[-300:]))
    # 9d (V-72): signing only with every Azure variable, never an unsigned "signed" artefact, and
    # publish takes signed + Valid installers only
    sign = jobs.split("\n  sign:", 1)[1].split("\n  publish:", 1)[0]
    pub = jobs.split("\n  publish:", 1)[1]
    check("V-72: the sign job runs only with every Azure variable set",
          all(f"vars.{v} != ''" in sign for v in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID",
                                                  "AZURE_CODESIGN_ENDPOINT", "AZURE_CODESIGN_ACCOUNT",
                                                  "AZURE_CODESIGN_PROFILE")))
    check("V-72: the sign job installs the pinned Inno Setup (SHA-256) and uses --require-signed",
          "Install Inno Setup (pinned)" in sign and "INNO_SHA256" in sign
          and sign.count("--require-signed") == 2)
    check("V-72: publish needs a successful sign job, downloads signed artefacts only, checks Valid",
          "needs.sign.result == 'success'" in pub and "installer-*-signed" in pub
          and "unsigned" not in pub and "Get-AuthenticodeSignature" in pub and "'Valid'" in pub)
    check("draft release, not latest (F-223, F-195)", "draft: true" in y and "make_latest: false" in y)
    check("no SignPath", "signpath" not in y.lower())
    check("the tag reaches the shell through the environment (F-222)", "REF_NAME: ${{ github.ref_name }}" in y
          and '"${{ github.ref_name }}"' not in y)
    check("tests run in CI (D-115)", "test_parser" in y and "selftest" in y)


def test_misc():
    print("[5] telemetry, data dir, self-check, version, dev guard")
    rec = read("face_service/recognizer.py")
    check("ORT telemetry off before the first session (§2.11)", "disable_ort_telemetry()" in rec
          and "disable_telemetry_events" in read("face_service/ort_privacy.py"))
    check("the wizard turns it off too", "disable_ort_telemetry()" in read("presence_monitor/enroll_gui.py"))
    import face_service.config as C
    sys.frozen = True
    try:
        check("frozen: FACE_UNLOCK_HOME ignored (F-106)", C._app_dir() == Path.home() / ".face-unlock")
    finally:
        del sys.frozen
    check("checkout: FACE_UNLOCK_HOME honoured", str(C._app_dir()) == os.environ["FACE_UNLOCK_HOME"])
    main_src = read("face_service/__main__.py")
    check("custody self-check only for the build gate (D-73)", 'os.environ.get("FU_BUILD_GATE") != "1"' in main_src)
    from face_service._version import __version__
    check("version 0.2.0 in the app and the CP", __version__ == "0.2.0" and '"0.2.0"' in read("credential_provider/version.h"))
    reg = read("tools/register_tasks.ps1")
    check("dev registrar refuses a tree non-admins can change (R19)", "Get-FuWritableByOthers" in reg
          and "act 9b R19" in reg)
    check("dev uninstall -Force refuses it too", "act 9b R19" in read("tools/uninstall.ps1"))
    check("installed mode is the exe's business", "managed by its own executable" in reg)


def test_notices_and_docs():
    print("[6] third-party notices (F-260) and the end-user docs (9c-6)")
    import re
    import shutil
    import tempfile
    sys.path.insert(0, str(REPO / "installer"))
    import notices
    root = Path(tempfile.mkdtemp(prefix="faceunlock_notices_"))
    try:
        # 9d (A-6): stage for the variant THIS interpreter is (the CPU lock names onnxruntime, the
        # GPU lock onnxruntime-gpu); without ISCC only Inno Setup's text can be missing, and without a
        # bundle the GPU variant has no NVIDIA DLL to take texts for.
        from importlib import metadata as _md
        try:
            _md.version("onnxruntime-gpu")
            var = "gpu"
        except _md.PackageNotFoundError:
            var = "cpu"
        problems = notices.stage(root, var, None)
        check(f"stage(): every runtime package, Python, YuNet get their texts ({var})",
              [p_ for p_ in problems if "Inno" not in p_ and "inno-setup" not in p_
               and "NVIDIA" not in p_ and "nvidia" not in p_] == [], problems)
        check("V-37: the pywinrt packages get the MIT text of pywinrt",
              all((lic_ := root / "licenses" / f).is_dir() and "pywinrt" in (lic_ / "LICENSE.txt").read_text(
                  encoding="utf-8") for f in ("winrt-runtime", "winrt-windows-ui-notifications",
                                              "winrt-windows-data-xml-dom", "winrt-windows-foundation")))
        check("V-60: the lock follows the variant", notices.lock_for("cpu").name == "requirements.lock"
              and notices.lock_for("gpu").name == "requirements-gpu.lock"
              and not any(n_.startswith("nvidia") for n_ in notices.expected_folders("gpu")
                          if n_ != "nvidia"))
        lic = root / "licenses"
        check("pystray's LGPL + GPL texts are staged", (lic / "pystray" / "COPYING.LGPL").is_file()
              and (lic / "pystray" / "COPYING").is_file())
        check("the CPython license (OpenSSL, MSVC runtime conditions) is staged",
              (lic / "python" / "LICENSE.txt").is_file())
        check("packages without a license file get a stated text (insightface, flatbuffers)",
              (lic / "insightface" / "LICENSE.txt").is_file() and (lic / "flatbuffers" / "LICENSE.txt").is_file())
        check("no NVIDIA text in the CPU variant", not (lic / "nvidia").exists())
        shutil.rmtree(lic / "numpy")
        check("check(): an emptied package folder fails the gate",
              any("numpy" in p_ for p_ in notices.check(root, "cpu", notices_doc=False)))
        check("check(): the GPU variant insists on the NVIDIA texts",
              any("nvidia" in p_ for p_ in notices.check(root, "gpu", notices_doc=False)))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    build = read("installer/build.py")
    check("build.py stages the texts after PyInstaller and gates on them",
          "notices.stage(DIST_ROOT, variant, iscc)" in build and "notices.check(dist_root, variant)" in build)
    for doc in ("README.md", "INSTALL.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "CONTRIBUTING.md"):
        text = read(doc).replace("Русский", "")   # the language menu entry is named in its own script
        check(f"{doc}: English only, no internal references",
              not re.search("[\u0400-\u04FF]", text) and "KNOWN_ISSUES" not in text
              and "d-series" not in text and "docs/internal/" not in text or doc == "CONTRIBUTING.md")
    check("build.py ships SECURITY.md and THIRD_PARTY_NOTICES.md",
          '"SECURITY.md", "THIRD_PARTY_NOTICES.md"' in build)
    lic_text = read("LICENSE")
    check("LICENSE keeps the upstream line and adds the fork's (D-157)",
          "Copyright (c) 2026 Cao Chí Tâm" in lic_text and "Copyright (c) 2026 xbaox" in lic_text)
    check("README credits the upstream project", "caochitam/windows-face-unlock" in read("README.md"))
    for gone in ("KNOWN_ISSUES.md", "audit-notes.md", "face-unlock-MASTER-TZ.md", "stage-7-TZ.md",
                 "stage-7i-TZ.md", "stage1-benchmark.md"):
        check(f"internal {gone} lives in docs/internal/ (D-158)",
              not (REPO / gone).exists() and (REPO / "docs" / "internal" / gone).is_file())
    sec = read("SECURITY.md")
    check("SECURITY: the password-in-memory decision (D-43) and the print limitation (F-123)",
          "immutable" in sec and "Printed photo" in sec)
    check("SECURITY: fast mode signs in with no movement on a confident match (F-114)",
          "with\nno movement at all" in sec or "with no movement at all" in sec)
    check("SECURITY: privacy -- ORT telemetry off, the update request named",
          "switches it off" in sec and "api.github.com/repos/xbaox/windows-face-unlock" in sec)
    check("README: screen readers are a stated limitation (F-171)", "screen-reader" in read("README.md"))
    check("CONTRIBUTING: the R19 dev-tree restriction", "R19" in read("CONTRIBUTING.md")
          and "refuses" in read("CONTRIBUTING.md"))


def main() -> int:
    test_taskreg()
    test_f1_foreign()
    test_iss()
    test_iss_preprocessed()
    test_iss_pascal_paths()
    test_uninstall_ps1_home()
    test_build()
    test_locks_ci()
    test_misc()
    test_notices_and_docs()
    print()
    if FAILS:
        print(f"INSTALLER SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("INSTALLER SELFTEST OK: tasks through the product exe (only ours, verified), a fixed and "
          "checked install directory, models by consent and pins, no PowerShell, scoped uninstall, "
          "variants and NVIDIA allowlist, Authenticode-agnostic PE digest, hashed locks, least-"
          "privilege CI, telemetry off, R19 dev guard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
