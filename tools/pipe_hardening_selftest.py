"""tools/pipe_hardening_selftest.py -- pipe perimeter proof, Stage 4 as reworked in Stage 9 (R1, R2).

No service process, no camera. Private pipe names only.
  [1] descriptor: _build_pipe_sa() -- owner SELF, NETWORK denied FIRST, SELF + SYSTEM allowed, no
      Everyone ACE, Medium mandatory label (NoReadUp/NoWriteUp). No toggle, no legacy NULL DACL
      (F-55, F-68).
  [2] instances: the service creates at most two instances with the remote-reject flag; the name
      is claimed first-instance, a second claim fails and stops that service (F-54 / F-56).
  [3] always listening: after a connection is served the next instance already exists -- the name
      is never free between two clients (F-54); a client that came and went (ERROR_NO_DATA) is not
      a pipe error (F-59).
  [4] SID helpers on a live pipe: the service reads the CLIENT SID, the client reads the SERVER SID
      and the pipe OBJECT's owner -- all SELF. The client policy accepts SELF only (R1: the
      service always runs as the owner; SYSTEM is no longer a valid server) (F-64).
  [5] gates (R2): unlock / unlock_gesture / report_result need a SYSTEM caller whatever the
      config says, then protocol v2; verify needs SELF.
  [6] old config keys: a config.toml that still carries pipe_hardened_sd / pipe_first_instance /
      pipe_unlock_require_system loads with an "unknown key" warning and nothing else changes;
      a reload with them weakens nothing (the ratchet went with the keys, F-104).
  [7] owner (R1): owner_check() -- same SID ok, other SID / nothing recorded / a non-person SID
      refused; a not-owner service answers ping "refusing: not-owner" and refuses every face
      function.
  [8] F-66 / F-113: a handler fault answers "internal-error" (no exception text) and the unlock
      path audits it; profile paths are scrubbed from text that leaves over the pipe.

Uses an isolated FACE_UNLOCK_HOME and FaceService.__new__ (no heavy init).
Run:  python -m tools.pipe_hardening_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_pipehard_")
from tools.testkit import patch, run_restoring  # noqa: E402  (D-142)

import pywintypes     # type: ignore
import win32con       # type: ignore
import win32file      # type: ignore
import win32pipe      # type: ignore
import win32security  # type: ignore

from face_service.config import APP_DIR, CONFIG_PATH, Config
from face_service import identity as I
from face_service import pipe_io as P
from face_service import service as svc
from face_service.service import FaceService, VerifyOutcome, _pipe_client_sid_string

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got})" if got is not None else ""))
        FAILS.append(name)


def _read_sddl(handle, info):
    sd = win32security.GetSecurityInfo(handle, win32security.SE_KERNEL_OBJECT, info)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, info)


class _LockoutSpy:
    def __init__(self): self.records = []
    def remaining(self): return 0.0
    def record(self, ok): self.records.append(ok)
    def status(self): return {}
    def reconfigure(self, *a): pass


class _AuditStub:
    def __init__(self): self.records = []
    def write(self, ev, rec): self.records.append((ev, rec))
    def reconfigure(self, *a): pass
    def status(self): return {}


class _RecogStub:
    def __init__(self): self.cfg = None


def _svc(cfg=None, caller="S-1-5-18"):
    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: caller
    s.cfg = cfg or Config()
    s._lockout = _LockoutSpy()
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s._stop = threading.Event()
    s.recog = _RecogStub()
    return s


class _WarnSpy(logging.Handler):
    def __init__(self, logger):
        super().__init__(level=logging.WARNING)
        self.logger = logger
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())

    def __enter__(self):
        logging.getLogger(self.logger).addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger(self.logger).removeHandler(self)
        return False


def _private(tag):
    return r"\\.\pipe\FaceUnlockHardening-" + tag + "-" + uuid.uuid4().hex[:8]


def test_descriptor():
    print("[1] hardened pipe descriptor (always; no toggle)")
    self_sid = I.current_user_sid()
    sa = svc._build_pipe_sa()
    name = _private("sd")
    h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX,
                                  win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_WAIT, 1,
                                  4096, 4096, 0, sa)
    info = (win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
            | win32security.LABEL_SECURITY_INFORMATION)
    sddl = _read_sddl(h, info)
    win32file.CloseHandle(h)
    print(f"    SDDL: {sddl}")
    dacl = sddl.split("D:", 1)[1].split("S:", 1)[0]
    check("owner is SELF", sddl.startswith(f"O:{self_sid}"), sddl)
    check("first ACE denies NETWORK (NU) everything", dacl.lstrip("PAI").startswith("(D;;"), dacl)
    check("the deny ACE names NU", "(D;;" in dacl and ";;;NU)" in dacl, dacl)
    check("DACL allows SELF", self_sid in dacl, dacl)
    check("DACL allows SYSTEM", ";;;SY)" in dacl, dacl)
    check("no Everyone / Authenticated Users ACE", ";;;WD)" not in dacl and ";;;AU)" not in dacl, dacl)
    check("Medium mandatory label (ME + NoRead/NoWrite-up)",
          "ME" in sddl and ("NWNR" in sddl or "NRNW" in sddl), sddl)
    check("no legacy NULL-DACL builder left", not hasattr(svc, "_build_sa_everyone_legacy"))


def test_instances():
    print("[2] instances: two at most, remote clients rejected, name claimed first")
    orig = svc.PIPE_NAME
    svc.PIPE_NAME = _private("inst")
    try:
        a = _svc()
        check("bind claims the name", a._bind() is True and a._listen is not None)
        ok, flags, _o, _i, maxinst = (True, *win32pipe.GetNamedPipeInfo(a._listen))
        check("max instances == 2 (was unlimited)", maxinst == 2, maxinst)
        check("the flag constants are the documented values",
              svc.PIPE_REJECT_REMOTE_CLIENTS == 0x8 and svc.FILE_FLAG_FIRST_PIPE_INSTANCE == 0x80000)
        b = _svc()
        with _WarnSpy("face_service.service") as spy:
            bound = b._bind()
        check("a second service cannot claim the name -> refuses and stops",
              bound is False and b._stop.is_set(), (bound, b._stop.is_set()))
        check("... loudly (possible squatter)", any("possible squatter" in m for m in spy.msgs), spy.msgs)
        a._close_listen()
    finally:
        svc.PIPE_NAME = orig


def _client_roundtrip(name, payload=b'{"cmd":"ping"}'):
    deadline = time.monotonic() + 5
    while True:
        try:
            h = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                                     win32con.OPEN_EXISTING, 0, None)
            break
        except pywintypes.error:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    try:
        win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(h, payload)
        _hr, data = win32file.ReadFile(h, 65536)
        return data
    finally:
        win32file.CloseHandle(h)


def test_always_listening():
    print("[3] the name is never free between two connections")
    orig = svc.PIPE_NAME
    name = svc.PIPE_NAME = _private("listen")
    try:
        s = _svc()
        s._bind()
        th = threading.Thread(target=s._serve_one, daemon=True)
        th.start()
        data = _client_roundtrip(name)
        th.join(5)
        check("ping served", b'"pong": true' in data, data)
        check("after the served instance closed, a listening instance already exists",
              s._listen is not None and win32pipe.WaitNamedPipe(name, 1) is None)
        # F-59: a client that connects and leaves before the server reaches ConnectNamedPipe
        ch = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                                  win32con.OPEN_EXISTING, 0, None)
        win32file.CloseHandle(ch)
        errors = []
        try:
            s._serve_one()
        except Exception as e:
            errors.append(repr(e))
        check("a client that came and went is not a pipe error (F-59)", errors == [], errors)
        check("... and the name is still held afterwards", s._listen is not None)
        s._close_listen()
    finally:
        svc.PIPE_NAME = orig


def test_sid_helpers():
    print("[4] SID policy + live server / client / pipe-owner reads")
    me = P.self_sid_string()
    check("policy allows SELF", P.server_sid_allowed(me, me))
    check("policy rejects SYSTEM as a server (R1)", not P.server_sid_allowed("S-1-5-18", me))
    # The Python clients' rule (pipe_io). The Credential Provider has its own server check
    # (PipeClient.cpp), tested in credential_provider/tests (test_parser) -- D-143.
    check("Python pipe_io policy rejects a foreign SID",
          not P.server_sid_allowed("S-1-5-21-1-2-3-4444", me))
    check("policy rejects an empty SID", not P.server_sid_allowed("", me))

    name = _private("sid")
    res = {}
    # D-139 (B14-06): the steps are ordered by events, not by sleeps -- the client connects only
    # once the pipe exists and closes only once the server has read its SID.
    created, sid_read, client_closed = threading.Event(), threading.Event(), threading.Event()

    def _server():
        try:
            sh = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX,
                                           win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_WAIT, 1,
                                           4096, 4096, 0, svc._build_pipe_sa())
            created.set()
            win32pipe.ConnectNamedPipe(sh, None)
            win32file.ReadFile(sh, 64)
            res["client_sid"] = _pipe_client_sid_string(sh)
            sid_read.set()
            client_closed.wait(5.0)
            win32pipe.DisconnectNamedPipe(sh)
            win32file.CloseHandle(sh)
        except Exception as e:
            res["server_err"] = repr(e)
        finally:
            created.set()
            sid_read.set()

    th = threading.Thread(target=_server, daemon=True)
    th.start()
    created.wait(5.0)
    # B14 N-09: the client offers IDENTIFICATION only (as the CP and pipe_io do) and the service
    # still reads its SID.
    ch = win32file.CreateFile(name, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
                              win32con.OPEN_EXISTING,
                              P.SECURITY_SQOS_PRESENT | P.SECURITY_IDENTIFICATION, None)
    res["server_sid"] = P.server_sid_string(ch)
    res["pipe_owner"] = P.pipe_owner_sid_string(ch)
    win32file.WriteFile(ch, b"selftest")
    sid_read.wait(5.0)
    win32file.CloseHandle(ch)
    client_closed.set()
    th.join(5)
    check("server SID on a live connection == SELF", res.get("server_sid") == me, res)
    check("pipe OBJECT owner == SELF (F-64)", res.get("pipe_owner") == me, res)
    check("client SID read at IDENTIFICATION level == SELF (N-09)", res.get("client_sid") == me, res)
    check("client SID None on an unusable handle (no raise)", _pipe_client_sid_string(None) is None)

    # pipe_io: a foreign pipe owner is refused before anything is written
    name = _private("own")
    got = []

    def _srv2():
        h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX,
                                      win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
                                      | win32pipe.PIPE_WAIT, 1, 4096, 4096, 0, None)
        try:
            win32pipe.ConnectNamedPipe(h, None)
            try:
                _hr, data = win32file.ReadFile(h, 4096)
                got.append(data)
            except pywintypes.error:
                pass
        finally:
            win32file.CloseHandle(h)

    orig = P.pipe_owner_sid_string
    P.pipe_owner_sid_string = lambda h: "S-1-5-21-1-2-3-4444"
    try:
        th = threading.Thread(target=_srv2, daemon=True)
        th.start()
        resp, why = P.exchange({"cmd": "ping"}, 2.0, pipe_name=name)
        th.join(3)
    finally:
        P.pipe_owner_sid_string = orig
    check("pipe_io: foreign pipe owner -> untrusted-server, nothing written",
          resp is None and why == "untrusted-server" and got == [], (why, got))


def test_gates():
    print("[5] gates: SYSTEM + v2 for the credential commands, SELF for verify")
    patch(svc, "load_password", lambda: {"u": "admin", "p": "pw", "d": "."})
    calls = {"verify": 0}

    def forced():
        calls["verify"] += 1
        return VerifyOutcome(True, 0.1, True, {"verdict": "PASS"}, None, 100.0)

    user = _svc(caller="S-1-5-21-1-2-3-1001")
    user._capture_and_verify = forced
    orig_diag = svc._pipe_client_diag
    svc._pipe_client_diag = lambda h: "stub"
    try:
        for req in ({"cmd": "unlock", "v": 2}, {"cmd": "unlock_gesture", "v": 2, "token": "a" * 32},
                    {"cmd": "report_result", "v": 2, "grant_id": "ab", "ok": True}):
            r = user._handle(req, None)
            check(f"{req['cmd']}: non-SYSTEM caller -> not-authorized",
                  r == {"ok": False, "reason": "not-authorized"}, r)
        none = _svc(caller=None)
        r = none._handle({"cmd": "unlock", "v": 2}, None)
        check("unreadable caller SID -> not-authorized (fails closed)", r.get("reason") == "not-authorized", r)
    finally:
        svc._pipe_client_diag = orig_diag
    check("the gate short-circuits before any burst", calls["verify"] == 0)

    lock = _svc()
    lock._capture_and_verify = forced
    for bad in ({"cmd": "unlock"}, {"cmd": "unlock", "v": 1}, {"cmd": "unlock", "v": True},
                {"cmd": "unlock", "v": "2"}):
        r = lock._handle(bad, None)
        check(f"version gate: {bad} -> version-mismatch", r == {"ok": False, "reason": "version-mismatch"}, r)
    check("version gate short-circuits before any burst", calls["verify"] == 0)
    for bad in (0, -5, True, "100", 1.5, 10 ** 9):
        r = lock._handle({"cmd": "unlock", "v": 2, "budget_ms": bad}, None)
        check(f"budget_ms={bad!r} -> bad-request", r == {"ok": False, "reason": "bad-request"}, r)
    r = lock._handle({"cmd": "unlock", "v": 2, "budget_ms": 11500}, None)
    check("SYSTEM + v2 + budget -> reaches the grant", r.get("ok") is True and len(r.get("grant_id", "")) == 32, r)
    check("the budget is taken minus the 0.5 s reply reserve",
          abs(lock._client_budget_s - 11.0) < 1e-9, lock._client_budget_s)

    selfc = _svc(caller=I.current_user_sid())
    selfc._capture_and_verify = forced
    r = selfc._handle({"cmd": "verify"}, None)
    check("verify: SELF caller -> answered (diagnostic, no secret)",
          r.get("ok") is True and "password" not in r, r)
    sysc = _svc()
    sysc._capture_and_verify = forced
    svc._pipe_client_diag = lambda h: "stub"
    try:
        r = sysc._handle({"cmd": "verify"}, None)
    finally:
        svc._pipe_client_diag = orig_diag
    check("verify: SYSTEM caller -> not-authorized (SELF only, §2.3)",
          r == {"ok": False, "reason": "not-authorized"}, r)
    r = sysc._handle({"cmd": "challenge", "kind": "x" * 5000}, None)
    check("challenge command removed (F-67) -> unknown-command, nothing echoed",
          r == {"ok": False, "reason": "unknown-command"}, r)


def test_old_keys():
    print("[6] old perimeter keys in config.toml: unknown-key warning, nothing weakens")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("pipe_hardened_sd = false\npipe_first_instance = false\n"
                           "pipe_unlock_require_system = false\npresence_interval_s = 77\n",
                           encoding="utf-8")
    with _WarnSpy("face_service.config") as spy:
        cfg = Config.load()
    check("the rest of the file still applies", cfg.presence_interval_s == 77, cfg.presence_interval_s)
    check("one warning names the three unknown keys",
          any("unknown key" in m and "pipe_hardened_sd" in m and "pipe_first_instance" in m
              and "pipe_unlock_require_system" in m for m in spy.msgs), spy.msgs)
    s = _svc()
    r = s._reload_config()
    check("reload with the old keys succeeds and carries none of them",
          r.get("ok") is True and not any(k.startswith("pipe_") for k in r["config"]), r.get("config"))
    CONFIG_PATH.unlink()


def test_owner():
    print("[7] owner (R1)")
    me = I.current_user_sid()
    check("owner_check: same SID -> ok", I.owner_check(me, me) is None)
    check("owner_check: another person -> refused",
          I.owner_check("S-1-5-21-1-2-3-1001", me) is not None)
    check("owner_check: nothing recorded -> refused", I.owner_check("", me) is not None)
    check("owner_check: SYSTEM recorded -> refused", I.owner_check("S-1-5-18", me) is not None)
    check("is_user_sid: local / domain / Entra accepted",
          I.is_user_sid("S-1-5-21-1-2-3-1001") and I.is_user_sid("S-1-12-1-111-222-333-444"))
    check("is_user_sid: service SIDs and junk refused",
          not I.is_user_sid("S-1-5-18") and not I.is_user_sid("S-1-5-20")
          and not I.is_user_sid("S-1-5-21-1") and not I.is_user_sid(None))
    s = _svc()
    s._not_owner = True
    r = s._handle({"cmd": "ping"}, None)
    check("not-owner: ping alive, refusing, why=not-owner",
          r == {"ok": True, "pong": True, "state": "refusing", "why": "not-owner"}, r)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("not-owner: unlock refused (after SYSTEM + version)", r == {"ok": False, "reason": "not-owner"}, r)
    s._presence_probe = lambda: ("present", True)
    r = s._handle({"cmd": "presence"}, None)
    check("not-owner: presence refused", r == {"ok": False, "reason": "not-owner"}, r)
    st = _svc()
    st._audit = type("A", (), {"status": lambda self: {}, "write": lambda *a: None})()
    st._started_at = time.time()
    st._not_owner = True
    r = st._status()
    check("status carries state/why/protocol/password_rejected",
          r.get("state") == "refusing" and r.get("why") == "not-owner" and r.get("protocol") == 2
          and r.get("password_rejected") is False, {k: r.get(k) for k in ("state", "why", "protocol")})


def test_internal_error_and_scrub():
    print("[8] internal-error (F-66) and path scrubbing (F-113)")
    orig = svc.PIPE_NAME
    name = svc.PIPE_NAME = _private("err")
    try:
        s = _svc()

        def boom():
            raise RuntimeError(r"C:\Users\somebody\.face-unlock\embeddings.npz is broken")
        s._capture_and_verify = boom
        s._bind()
        th = threading.Thread(target=s._serve_one, daemon=True)
        th.start()
        data = _client_roundtrip(name, b'{"cmd":"unlock","v":2}')
        th.join(5)
        s._close_listen()
        check("a handler fault answers internal-error, no exception text",
              b'"internal-error"' in data and b"somebody" not in data and b"exception" not in data, data)
        check("the unlock path audits internal-error",
              s._audit.records and s._audit.records[-1] == ("unlock", {"outcome": "internal-error"}),
              s._audit.records[-1:])
    finally:
        svc.PIPE_NAME = orig
    text = f"cannot read {APP_DIR}\\enroll\\x.jpg under {os.path.expanduser('~')}\\Pictures"
    out = svc._scrub(text)
    check("scrub: data dir -> <data>, profile -> ~",
          "<data>" in out and str(APP_DIR) not in out and os.path.expanduser("~") not in out, out)


def main() -> int:
    run_restoring(
        test_descriptor,
        test_instances,
        test_always_listening,
        test_sid_helpers,
        test_gates,
        test_old_keys,
        test_owner,
        test_internal_error_and_scrub,
    )
    if FAILS:
        print(f"\nPIPE-HARDENING SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nPIPE-HARDENING SELFTEST OK: owner-pinned hardened pipe, NETWORK denied, two instances, "
          "always listening; SYSTEM + v2 gates with no config switch; owner refusal; no leaks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
