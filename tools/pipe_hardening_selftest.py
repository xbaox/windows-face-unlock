"""tools/pipe_hardening_selftest.py -- Stage 4 channel-perimeter proof (no service, no camera).

Reproducible checks for the Batch-1/2 channel hardening:
  [1] hardened pipe descriptor: _build_pipe_sa(cfg) yields the expected SDDL -- SELF + SYSTEM, no
      Everyone ACE, Medium mandatory label (NoReadUp/NoWriteUp); the legacy toggle -> NULL DACL.
  [2]/[3] SID helpers on a live in-process pipe: pipe_client._server_sid_string reads the SERVER SID
      and service._pipe_client_sid_string reads the CLIENT SID -- both == SELF; the allow/reject
      policy accepts SELF and SYSTEM and rejects a foreign SID; an unusable handle -> None (no raise).
  [4] unlock SID-gate: require_system=True + a non-SYSTEM caller -> not-authorized BEFORE verify /
      load_password (short-circuit, no camera); require_system=False -> the branch runs as before.
  [5] posture ratchet (7e-2): a reload_config carrying pipe_unlock_require_system / pipe_hardened_sd
      = false CANNOT weaken a service that booted hardened -- each key stays True, one WARNING per
      refused key, the rest of the reload still applies. False -> True is allowed, and raises the
      floor so the pair cannot be walked back down in two steps.

Uses an isolated FACE_UNLOCK_HOME and FaceService.__new__ (no heavy init) so it touches no real
state and no camera/engine.
Run:  python -m tools.pipe_hardening_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import logging
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

from face_service.config import CONFIG_PATH, Config
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
    def reconfigure(self, *a): pass      # _reload_config calls this


class _AuditStub:
    def __init__(self): self.records = []
    def write(self, ev, rec): self.records.append((ev, rec))
    def reconfigure(self, *a): pass      # _reload_config calls this


class _RecogStub:
    """_reload_config re-points the recognizer at the new config; that is all it needs here."""
    def __init__(self): self.cfg = None


def _svc(cfg):
    s = FaceService.__new__(FaceService)
    s.cfg = cfg
    s._lockout = _LockoutSpy()
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s.recog = _RecogStub()
    return s


class _WarnSpy(logging.Handler):
    """Collect WARNING+ messages from face_service.service for the duration of a block."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())

    def __enter__(self):
        logging.getLogger("face_service.service").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("face_service.service").removeHandler(self)
        return False


def _write_cfg(**kv):
    """Write a minimal config.toml into the ISOLATED FACE_UNLOCK_HOME. Keys not named here fall
    back to Config defaults on load, so both posture keys are always written explicitly."""
    lines = []
    for k, v in kv.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        else:
            lines.append(f"{k} = {v}")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def test_posture_ratchet():
    print("[5] posture ratchet on reload (7e-2)")

    # --- a hardened service is offered a downgraded config on disk -------------------
    hardened = _svc(Config(pipe_unlock_require_system=True, pipe_hardened_sd=True))
    _write_cfg(pipe_unlock_require_system=False, pipe_hardened_sd=False,
               presence_interval_s=77)
    with _WarnSpy() as spy:
        resp = hardened._reload_config()
    refusals = [m for m in spy.msgs if "posture downgrade refused" in m]
    check("reload still succeeds (refusal is per-key, not a rejected reload)",
          resp.get("ok") is True, resp)
    check("pipe_unlock_require_system stays True",
          hardened.cfg.pipe_unlock_require_system is True)
    check("pipe_hardened_sd stays True", hardened.cfg.pipe_hardened_sd is True)
    check("reply reports the values that actually took effect",
          resp["config"]["pipe_unlock_require_system"] is True
          and resp["config"]["pipe_hardened_sd"] is True, resp.get("config"))
    check("the rest of the reload applied normally (presence_interval_s 60 -> 77)",
          hardened.cfg.presence_interval_s == 77, hardened.cfg.presence_interval_s)
    check("one loud warning per refused key", len(refusals) == 2, spy.msgs)
    check("each warning names its key",
          any("pipe_unlock_require_system" in m for m in refusals)
          and any("pipe_hardened_sd" in m for m in refusals), refusals)

    # --- tightening at run time is allowed, and raises the floor --------------------
    soft = _svc(Config(pipe_unlock_require_system=False, pipe_hardened_sd=False))
    _write_cfg(pipe_unlock_require_system=True, pipe_hardened_sd=True)
    with _WarnSpy() as spy_up:
        soft._reload_config()
    check("False -> True applied (tightening is not refused)",
          soft.cfg.pipe_unlock_require_system is True and soft.cfg.pipe_hardened_sd is True)
    check("tightening logs no refusal",
          not [m for m in spy_up.msgs if "posture downgrade refused" in m], spy_up.msgs)
    _write_cfg(pipe_unlock_require_system=False, pipe_hardened_sd=False)
    soft._reload_config()
    check("no two-step walk-down: after tightening, a downgrade is refused too",
          soft.cfg.pipe_unlock_require_system is True and soft.cfg.pipe_hardened_sd is True)

    # --- a service that BOOTED soft is not silently hardened behind the operator ----
    # (the dev workflow in credential_provider/tests/unlock_harness.cpp: the gate is turned
    # off in config and the service is RESTARTED, so the boot value is the floor.)
    dev = _svc(Config(pipe_unlock_require_system=False, pipe_hardened_sd=False))
    _write_cfg(pipe_unlock_require_system=False, pipe_hardened_sd=False)
    with _WarnSpy() as spy_dev:
        dev._reload_config()
    check("boot-soft stays soft (ratchet never invents hardening)",
          dev.cfg.pipe_unlock_require_system is False and dev.cfg.pipe_hardened_sd is False)
    check("boot-soft reload logs no refusal",
          not [m for m in spy_dev.msgs if "posture downgrade refused" in m], spy_dev.msgs)


def main() -> int:
    test_descriptor()
    test_sid_helpers()
    test_unlock_gate()
    test_posture_ratchet()
    if FAILS:
        print(f"\nPIPE-HARDENING SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nPIPE-HARDENING SELFTEST OK: hardened DACL + mandatory label; server/client SID reads; "
          "unlock SID-gate short-circuits a non-SYSTEM caller; reload cannot weaken the posture keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
