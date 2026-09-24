"""tools/credentials_selftest.py -- Stage 4 credential-custody proof (temp home; no camera).

Isolated FACE_UNLOCK_HOME so the real ~/.face-unlock is never touched.
  [f] fresh save -> v2 blob + pipe_entropy.bin locked to SELF+SYSTEM (no Everyone) + round-trip
  [g] legacy v1 blob -> load migrates to v2 once (plaintext correct; second load reads v2 directly)
  [h] corrupt / missing blob -> load_password returns None (never raises) -> unlock degrades cleanly
  [i] pipe_entropy.bin descriptor grants only SELF + SYSTEM
  [j] Stage 8b (F-23): "locked" now means PROTECTED (the P flag) and no BUILTIN\\Users ACE -- also
      for a file that ALREADY EXISTED under a directory carrying the 0.1.0 Users:Modify ACE, where
      CREATE_ALWAYS used to keep the old, inherited descriptor

Run:  python -m tools.credentials_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FACE_UNLOCK_HOME", tempfile.mkdtemp(prefix="faceunlock_cred_"))

import win32api      # type: ignore
import win32con      # type: ignore
import win32crypt    # type: ignore
import win32security  # type: ignore

from face_service import credentials as C

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got})" if got is not None else ""))
        FAILS.append(name)


def _self_sid():
    th = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(
        win32security.GetTokenInformation(th, win32security.TokenUser)[0])


def _sddl_of(path):
    sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, win32security.DACL_SECURITY_INFORMATION)


def _entropy_sddl():
    return _sddl_of(C.ENTROPY_PATH)


def _dacl_self_system_only(sddl, ss):
    # Stage 8b (F-23): the protected flag is REQUIRED -- without it the parent's inheritable ACEs
    # flow in -- and BUILTIN\Users is rejected by name and by SID (the 0.1.0 installer ACE).
    return (sddl.startswith("D:P") and ss in sddl and ("SY" in sddl or "S-1-5-18" in sddl)
            and "WD" not in sddl and "AU" not in sddl and "S-1-1-0" not in sddl
            and "BU" not in sddl and "S-1-5-32-545" not in sddl)


def _reset():
    for p in (C.CREDS_PATH, C.ENTROPY_PATH):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def main() -> int:
    ss = _self_sid()

    print("[f] fresh save -> v2 + locked entropy + round-trip")
    _reset()
    C.save_password("admin", "secretpw", ".")
    blob = C.CREDS_PATH.read_bytes()
    check("blob is v2 (prefix)", blob.startswith(b"v2:"))
    check("pipe_entropy.bin created", C.ENTROPY_PATH.exists())
    sddl_f = _entropy_sddl()
    check("entropy DACL = SELF+SYSTEM only", _dacl_self_system_only(sddl_f, ss), sddl_f)
    check("round-trips", C.load_password() == {"u": "admin", "p": "secretpw", "d": "."})

    print("[g] legacy v1 -> migrate to v2 once")
    _reset()
    plain = json.dumps({"u": "u1", "p": "p1", "d": "."}).encode("utf-8")
    v1 = win32crypt.CryptProtectData(plain, "face-unlock", C.ENTROPY, None, None, 0)
    C.CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    C.CREDS_PATH.write_bytes(v1)
    check("crafted blob is v1 (no marker)", not C.CREDS_PATH.read_bytes().startswith(b"v2:"))
    first = C.load_password()
    check("v1 load returns correct plaintext", first == {"u": "u1", "p": "p1", "d": "."}, first)
    check("blob migrated to v2 on disk", C.CREDS_PATH.read_bytes().startswith(b"v2:"))
    check("entropy secret generated during migration", C.ENTROPY_PATH.exists())
    check("second load reads v2 directly (same value)", C.load_password() == first)

    print("[h] corrupt / missing -> None (no crash)")
    _reset()
    check("missing blob -> None", C.load_password() is None)
    C.CREDS_PATH.write_bytes(os.urandom(64))
    check("corrupt v1 blob -> None", C.load_password() is None)
    C.CREDS_PATH.write_bytes(b"v2:" + os.urandom(64))
    check("corrupt v2 blob -> None", C.load_password() is None)

    print("[i] entropy descriptor SELF+SYSTEM only")
    _reset()
    C.save_password("x", "y", ".")
    sddl_i = _entropy_sddl()
    print(f"    SDDL: {sddl_i}")
    check("entropy DACL = SELF+SYSTEM, no Everyone", _dacl_self_system_only(sddl_i, ss), sddl_i)

    print("[j] pre-existing files under a Users:Modify directory are re-locked")
    _reset()
    subprocess.run(["icacls", str(C.CREDS_PATH.parent), "/grant", "*S-1-5-32-545:(OI)(CI)(M)"],
                   capture_output=True, check=True)
    C.ENTROPY_PATH.write_bytes(os.urandom(32))
    C.CREDS_PATH.write_bytes(b"stale")
    seeded = _sddl_of(C.ENTROPY_PATH)
    check("seed: the existing entropy file inherited the Users ACE",
          "BU" in seeded or "S-1-5-32-545" in seeded, seeded)
    check("the checker rejects that seeded descriptor", not _dacl_self_system_only(seeded, ss))
    C._write_locked_file(C.ENTROPY_PATH, os.urandom(32))
    sddl_j = _entropy_sddl()
    check("existing entropy file: P flag, SELF+SYSTEM, no Users", _dacl_self_system_only(sddl_j, ss),
          sddl_j)
    C.save_password("x", "y", ".")
    sddl_c = _sddl_of(C.CREDS_PATH)
    check("credentials.bin over an existing file: P flag, SELF+SYSTEM, no Users",
          _dacl_self_system_only(sddl_c, ss), sddl_c)
    check("no credentials.bin.tmp left behind",
          not C.CREDS_PATH.with_suffix(C.CREDS_PATH.suffix + ".tmp").exists())
    check("round-trips after the re-lock", C.load_password() == {"u": "x", "p": "y", "d": "."})

    if FAILS:
        print(f"\nCREDENTIALS SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nCREDENTIALS SELFTEST OK: v2 per-install entropy; one-time v1->v2 migration; corrupt/"
          "missing degrade to None; the entropy file and the blob are locked to SELF+SYSTEM "
          "(protected, no BUILTIN\\Users), including over files that already existed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
