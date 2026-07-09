"""tools/pipe_hardening_selftest.py -- Stage 4 channel-perimeter proof (no service, no camera).

Reproducible checks for the Batch-1/2 channel hardening:
  [1] hardened pipe descriptor: _build_pipe_sa(cfg) yields the expected SDDL -- SELF + SYSTEM, no
      Everyone ACE, Medium mandatory label (NoReadUp/NoWriteUp); the legacy toggle -> NULL DACL.
  [2]/[3] SID helpers on a live in-process pipe: pipe_client._server_sid_string reads the SERVER SID
      and service._pipe_client_sid_string reads the CLIENT SID -- both == SELF; the allow/reject
      policy accepts SELF and SYSTEM and rejects a foreign SID; an unusable handle -> None (no raise).
  [4] unlock SID-gate: require_system=True + a non-SYSTEM caller -> not-authorized BEFORE verify /
      load_password (short-circuit, no camera); require_system=False -> the branch runs as before.

Uses an isolated FACE_UNLOCK_HOME and FaceService.__new__ (no heavy init) so it touches no real
state and no camera/engine.
Run:  python -m tools.pipe_hardening_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FACE_UNLOCK_HOME", tempfile.mkdtemp(prefix="faceunlock_pipehard_"))

import win32con       # type: ignore
import win32file      # type: ignore
import win32pipe      # type: ignore
import win32security  # type: ignore

from face_service.config import Config
from face_service import service as svc
from face_service.service import FaceService, VerifyOutcome, _pipe_client_sid_string
from tools.pipe_client import _server_sid_allowed, _self_sid_string, _server_sid_string

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got})" if got is not None else ""))
        FAILS.append(name)


def _make_pipe(name, sa):
    return win32pipe.CreateNamedPipe(
        name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
        win32pipe.PIPE_UNLIMITED_INSTANCES, 65536, 65536, 0, sa)


def _read_sddl(handle, info):
    sd = win32security.GetSecurityInfo(handle, win32security.SE_KERNEL_OBJECT, info)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, info)


# lightweight FaceService (bypass heavy __init__: no camera/pywin32/model files) -- project convention
class _LockoutSpy:
    def __init__(self): self.records = []
    def remaining(self): return 0.0
    def record(self, ok): self.records.append(ok)
    def status(self): return {}


class _AuditStub:
    def __init__(self): self.records = []
    def write(self, ev, rec): self.records.append((ev, rec))


def _svc(cfg):
    s = FaceService.__new__(FaceService)
    s.cfg = cfg
    s._lockout = _LockoutSpy()
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    return s


def test_descriptor():
    print("[1] hardened pipe descriptor")
    self_sid = svc._current_user_sid_string()
    h = _make_pipe(r"\\.\pipe\FaceUnlockHardeningSelftest1", svc._build_pipe_sa(Config()))
    info = win32security.DACL_SECURITY_INFORMATION | win32security.LABEL_SECURITY_INFORMATION
    sddl = _read_sddl(h, info)
    win32file.CloseHandle(h)
    print(f"    SDDL: {sddl}")
    check("DACL grants SELF", self_sid in sddl, sddl)
    check("DACL grants SYSTEM", "SY" in sddl or "S-1-5-18" in sddl, sddl)
    check("no Everyone ACE", "WD" not in sddl and "AU" not in sddl and "S-1-1-0" not in sddl, sddl)
    check("Medium mandatory label (ME + NoRead/NoWrite-up)",
          "ME" in sddl and ("NWNR" in sddl or "NRNW" in sddl), sddl)
    hl = _make_pipe(r"\\.\pipe\FaceUnlockHardeningSelftest1L",
                    svc._build_pipe_sa(Config(pipe_hardened_sd=False)))
    sddl_l = _read_sddl(hl, win32security.DACL_SECURITY_INFORMATION)
    win32file.CloseHandle(hl)
    check("legacy toggle -> NULL DACL (allow all)", "NO_ACCESS_CONTROL" in sddl_l, sddl_l)


def test_sid_helpers():
    print("[2]/[3] SID allow-policy + live server/client SID read")
    self_sid = _self_sid_string()
    check("policy allows SELF", _server_sid_allowed(self_sid, self_sid))
    check("policy allows SYSTEM", _server_sid_allowed("S-1-5-18", self_sid))
    check("policy rejects a foreign SID", not _server_sid_allowed("S-1-5-21-1-2-3-4444", self_sid))

    name = r"\\.\pipe\FaceUnlockHardeningSelftest2"
    res = {}

    def _server():
        try:
            sh = _make_pipe(name, None)
            win32pipe.ConnectNamedPipe(sh, None)
            win32file.ReadFile(sh, 64)                        # ImpersonateNamedPipeClient needs a prior read
            res["client_sid"] = _pipe_client_sid_string(sh)   # service reads the CLIENT SID (via impersonation)
            time.sleep(0.2)
            win32pipe.DisconnectNamedPipe(sh); win32file.CloseHandle(sh)
        except Exception as e:
            res["server_err"] = repr(e)

    th = threading.Thread(target=_server, daemon=True); th.start()
    time.sleep(0.3)
    ch = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                              0, None, win32con.OPEN_EXISTING, 0, None)
    res["server_sid"] = _server_sid_string(ch)                # client reads the SERVER SID
    win32file.WriteFile(ch, b"unlock-selftest")               # give the server a message to impersonate
    time.sleep(0.1)
    win32file.CloseHandle(ch)
    th.join(5)
    check("server-SID read on live connection == SELF", res.get("server_sid") == self_sid, res)
    check("client-SID read on live connection == SELF", res.get("client_sid") == self_sid, res)
    check("client-SID None on unusable handle (no raise)", _pipe_client_sid_string(None) is None)


def test_unlock_gate():
    print("[4] unlock SID-gate")
    svc.load_password = lambda: {"u": "admin", "p": "pw", "d": "."}
    calls = {"verify": 0}

    def forced():
        calls["verify"] += 1
        return VerifyOutcome(True, 0.1, True, {"verdict": "PASS"}, None, 100.0)

    on = _svc(Config(pipe_unlock_require_system=True))
    on._capture_and_verify = forced
    r_on = on._handle({"cmd": "unlock"}, None)   # handle None -> client SID None -> not SYSTEM
    check("require_system=True + non-SYSTEM -> not-authorized",
          r_on == {"ok": False, "reason": "not-authorized"}, r_on)
    check("gate short-circuits before verify (no camera path)", calls["verify"] == 0)

    off = _svc(Config(pipe_unlock_require_system=False))
    off._capture_and_verify = forced
    r_off = off._handle({"cmd": "unlock"}, None)
    check("require_system=False -> reaches load_password (unchanged)",
          r_off.get("ok") is True and r_off.get("username") == "admin", r_off)
    check("off path DID run verify once", calls["verify"] == 1)


def main() -> int:
    test_descriptor()
    test_sid_helpers()
    test_unlock_gate()
    if FAILS:
        print(f"\nPIPE-HARDENING SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nPIPE-HARDENING SELFTEST OK: hardened DACL + mandatory label; server/client SID reads; "
          "unlock SID-gate short-circuits a non-SYSTEM caller.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
