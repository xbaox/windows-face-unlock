"""tools/datadir_acl_selftest.py -- Stage 8b data-directory custody proof (F-01, F-23, F-02, F-12).

Everything runs on a FACE_UNLOCK_HOME this test creates itself under %TEMP%; the real
~/.face-unlock is never read or touched.
  [1] heal: a tree seeded with BUILTIN\\Users:Modify (the 0.1.0 installer ACE) is reported unclean,
      the heal removes every foreign ACE, protects APP_DIR, re-locks the secrets to SELF+SYSTEM,
      the verification walk comes back clean, and a second heal rewrites nothing.
  [2] fail-closed: a heal that cannot write a descriptor reports ok=False, and every face function
      is refused with "custody" (Stage 9, R9 / D-46): unlock and unlock_gesture right after the
      SYSTEM and version gates -- BEFORE the lockout, the camera and the token -- without touching
      the lockout counter and without reaching _release_credentials; presence, verify and the
      enrollment commands too; ping reports {"state":"refusing","why":"custody"}.
  [3] reparse points: a junction at APP_DIR, enroll or debug_frames makes the heal fail without
      following it; the target's descriptor and files are left exactly as they were.
  [4] purge-on-start removes only dump-named files, never enters a junction, and removes the
      directory once it is empty; the ring prune of _maybe_dump_frame deletes dump names only.
  [5] remove_tree_no_follow unlinks a junction as a link and leaves its target intact.
  [6] Stage 8b-2: the ctypes path (GetFileInformationByHandleEx FileStandardInfo +
      FileAttributeTagInfo) reports directory / link count / reparse exactly as the check needs --
      a hard-linked file and a junction deep in the tree still fail the heal -- and a heal in a
      fresh interpreter never loads win32timezone (the module the frozen bundle lacked in 8b).

Run:  python -m tools.datadir_acl_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
_ROOT = testhome.own_root("faceunlock_datadir_")
from tools.testkit import patch, run_restoring  # noqa: E402  (D-142)

import win32security  # type: ignore

from face_service import datadir as D
from face_service import service as svc
from face_service.config import APP_DIR, Config
from face_service.service import FaceService, VerifyOutcome

FAILS: list[str] = []
USERS_SID = "S-1-5-32-545"


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


def _plant_users_modify(path: Path) -> None:
    subprocess.run(["icacls", str(path), "/grant", f"*{USERS_SID}:(OI)(CI)(M)"],
                   capture_output=True, check=True)


def _junction(link: Path, target: Path) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                   capture_output=True, check=True)


def _sddl(path: Path) -> str:
    sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        sd, win32security.SDDL_REVISION_1, win32security.DACL_SECURITY_INFORMATION)


def _seed(home: Path) -> None:
    (home / "enroll").mkdir(parents=True, exist_ok=True)
    (home / "debug_frames").mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "credentials.bin", "credentials.bin.tmp", "pipe_entropy.bin",
                 "embeddings.npz", "enroll/a.jpg", "debug_frames/20260815-155309-915_probe.npy"):
        (home / name).write_bytes(b"x")


# --- [1] heal --------------------------------------------------------------------------------

def test_heal():
    print("[1] heal a tree seeded with BUILTIN\\Users:Modify")
    home = APP_DIR
    _seed(home)
    _plant_users_modify(home)
    check("seed: Users ACE inherited by a secret", USERS_SID in _sddl(home / "credentials.bin")
          or "BU" in _sddl(home / "credentials.bin"), _sddl(home / "credentials.bin"))
    before = D.verify_data_dir(home)
    check("verification reports the seeded tree as unclean", len(before) > 0, before)

    rep = D.heal_data_dir(home)
    check("heal ok", rep.ok, rep.problems)
    check("heal removed foreign ACEs", rep.aces_removed > 0, rep.summary())
    check("heal re-locked the three secret files", rep.relocked == 3, rep.summary())
    check("verification walk is clean", D.verify_data_dir(home) == [], D.verify_data_dir(home))

    root = _sddl(home)
    check("APP_DIR DACL protected (D:P)", root.startswith("D:P"), root)
    check("APP_DIR carries no Users ACE", "BU" not in root and USERS_SID not in root, root)
    for secret in ("credentials.bin", "credentials.bin.tmp", "pipe_entropy.bin"):
        s = _sddl(home / secret)
        check(f"{secret}: protected, SELF+SYSTEM only",
              s.startswith("D:P") and "BU" not in s and "BA" not in s and s.count("(A;") == 2, s)
    for plain in ("config.toml", "enroll/a.jpg", "enroll"):
        s = _sddl(home / plain)
        check(f"{plain}: inherited copy only, no Users",
              "BU" not in s and "ID;" in s and not s.startswith("D:P"), s)

    again = D.heal_data_dir(home)
    check("second heal is ok and rewrites nothing", again.ok and again.rewritten == 0,
          again.summary())


# --- [2] fail-closed ------------------------------------------------------------------------

class _LockoutSpy:
    def __init__(self, remaining_s: float = 0.0):
        self.records: list = []
        self._remaining = remaining_s

    def remaining(self):
        return self._remaining

    def record(self, ok):
        self.records.append(ok)

    def status(self):
        return {}


class _AuditStub:
    def __init__(self):
        self.records: list = []

    def write(self, ev, rec):
        self.records.append((ev, rec))


def _svc(insecure: bool, remaining_s: float = 0.0, caller: str = "S-1-5-18"):
    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: caller       # Stage 9: stand in for the lock screen (SYSTEM)
    s.cfg = Config()
    s._lockout = _LockoutSpy(remaining_s)
    s._audit = _AuditStub()
    s._camera_paused_until = 0.0
    s._gesture_slot = None
    s._cam_lock = threading.Lock()
    s._data_dir_insecure = insecure
    s._capture_and_verify = lambda: VerifyOutcome(True, 0.05, True, {"verdict": "PASS"}, None, None)
    return s


def test_fail_closed():
    print("[2] a heal that cannot write fails closed")
    home = _ROOT / "broken"
    _seed(home)
    _plant_users_modify(home)
    real_set = D.win32security.SetKernelObjectSecurity

    def _refuse(*_a, **_k):
        raise D.pywintypes.error(5, "SetKernelObjectSecurity", "Access is denied.")

    D.win32security.SetKernelObjectSecurity = _refuse
    try:
        rep = D.heal_data_dir(home)
    finally:
        D.win32security.SetKernelObjectSecurity = real_set
    check("heal reports ok=False", rep.ok is False, rep.summary())
    check("the failure names what could not be re-secured",
          any("cannot re-secure" in p for p in rep.problems), rep.problems[:2])

    released = []
    patch(svc, "load_password", lambda: released.append(1) or {"u": "admin", "p": "pw", "d": "."})

    s = _svc(insecure=True)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("unlock -> custody", r == {"ok": False, "reason": "custody"}, r)
    check("no strike, no reset (lockout-neutral)", s._lockout.records == [], s._lockout.records)
    check("audit outcome custody",
          s._audit.records and s._audit.records[-1][1].get("outcome") == "custody",
          s._audit.records)
    check("_release_credentials never reached", released == [], released)

    # The SYSTEM gate and the protocol version still answer first.
    s = _svc(insecure=True, caller="S-1-5-21-1-2-3-1001")
    orig_diag = svc._pipe_client_diag
    svc._pipe_client_diag = lambda h: "stub"
    try:
        r = s._handle({"cmd": "unlock", "v": 2}, None)
        check("non-SYSTEM caller still gets not-authorized first",
              r.get("reason") == "not-authorized", r)
        r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": "a" * 32}, None)
        check("unlock_gesture: not-authorized first", r.get("reason") == "not-authorized", r)
    finally:
        svc._pipe_client_diag = orig_diag
    s = _svc(insecure=True)
    r = s._handle({"cmd": "unlock"}, None)
    check("a v1 request still gets version-mismatch first", r.get("reason") == "version-mismatch", r)
    # ...and custody now comes BEFORE the lockout, the camera and the token (D-46).
    s = _svc(insecure=True, remaining_s=42.0)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("custody before the lockout", r.get("reason") == "custody", r)
    s = _svc(insecure=True)
    opened = []
    s._capture_and_verify = lambda: opened.append(1) or VerifyOutcome(
        False, 1.0, False, {"verdict": "SKIPPED"}, camera_busy=True)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("custody before the camera (no burst at all)", r.get("reason") == "custody" and not opened,
          (r, opened))
    s = _svc(insecure=True)
    s._gesture_slot = {"token": "c" * 32, "kind": "nod", "expires": 1e18}
    ran = []
    s._run_challenge = lambda k, *, identity=False: ran.append(k) or {"ok": True}
    r = s._handle({"cmd": "unlock_gesture", "v": 2, "token": "c" * 32}, None)
    check("unlock_gesture -> custody, before the token and the round",
          r == {"ok": False, "reason": "custody"} and not ran and s._gesture_slot is not None,
          (r, ran))
    check("gesture path: no strike, no reset", s._lockout.records == [], s._lockout.records)
    check("gesture path: _release_credentials never reached", released == [], released)

    s = _svc(insecure=False)
    r = s._handle({"cmd": "unlock", "v": 2}, None)
    check("secure directory: unlock grants as before", r.get("ok") is True, sorted(r))

    s = _svc(insecure=True)
    s._presence_probe = lambda: ("present", True)
    for cmd in ("presence", "build_enrollment", "clear_enrollment"):
        r = s._handle({"cmd": cmd}, None)
        check(f"{cmd} refused with custody (R9)", r == {"ok": False, "reason": "custody"}, r)
    s._caller_sid = lambda h: svc.current_user_sid()
    r = s._handle({"cmd": "verify"}, None)
    check("verify refused with custody (R9)", r == {"ok": False, "reason": "custody"}, r)
    r = s._handle({"cmd": "ping"}, None)
    check("ping: alive, refusing, why=custody",
          r == {"ok": True, "pong": True, "state": "refusing", "why": "custody"}, r)


# --- [3] reparse points -----------------------------------------------------------------------

def test_reparse():
    print("[3] reparse points are unsafe and never followed")
    victim = _ROOT / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"k")
    _plant_users_modify(victim)
    victim_before = (_sddl(victim), _sddl(victim / "keep.txt"))

    for sub in ("debug_frames", "enroll"):
        home = _ROOT / f"rp_{sub}"
        home.mkdir()
        (home / "config.toml").write_bytes(b"x")
        _junction(home / sub, victim)
        rep = D.heal_data_dir(home)
        check(f"junction at {sub} -> heal fails", rep.ok is False, rep.summary())
        check(f"junction at {sub} is named in the problems",
              any(sub in p and "reparse" in p for p in rep.problems), rep.problems[:3])
        check(f"{sub}: target descriptor untouched",
              (_sddl(victim), _sddl(victim / "keep.txt")) == victim_before)
        n = D.purge_debug_frames(home)
        check(f"{sub}: purge does not enter the junction",
              n == 0 and (victim / "keep.txt").exists(), n)

    top = _ROOT / "rp_top"
    _junction(top, victim)
    rep = D.heal_data_dir(top)
    check("junction AS APP_DIR -> heal fails", rep.ok is False and rep.objects == 0,
          rep.summary())
    check("APP_DIR junction: target descriptor untouched",
          (_sddl(victim), _sddl(victim / "keep.txt")) == victim_before)


# --- [4] purge + ring prune --------------------------------------------------------------------

def test_purge():
    print("[4] purge-on-start and the ring prune delete dump names only")
    home = _ROOT / "purge"
    d = home / "debug_frames"
    d.mkdir(parents=True)
    for n in ("20260815-155309-915_probe.npy", "20260815-155309-915_probe.png",
              "20260815-173650-549_verify3.npy"):
        (d / n).write_bytes(b"f")
    (d / "notes.txt").write_bytes(b"keep")
    check("purge removes the three dump files", D.purge_debug_frames(home) == 3)
    check("purge keeps a non-dump file", (d / "notes.txt").exists())
    (d / "notes.txt").unlink()
    (d / "20260815-155309-915_probe.npy").write_bytes(b"f")
    check("purge removes the last dump", D.purge_debug_frames(home) == 1)
    check("empty debug_frames is removed", not d.exists())

    # Ring prune: real _maybe_dump_frame under the dump knob, on the test's own APP_DIR.
    ring = APP_DIR / "debug_frames"
    shutil.rmtree(ring, ignore_errors=True)
    ring.mkdir(parents=True)
    (ring / "keep-me.txt").write_bytes(b"k")
    import numpy as np
    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: "S-1-5-18"   # Stage 9: stand in for the lock screen (SYSTEM)
    s.cfg = Config(debug_dump_frames=True)
    old_max = svc.DEBUG_DUMP_RING_MAX
    svc.DEBUG_DUMP_RING_MAX = 2
    try:
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        for i in range(3):
            s._maybe_dump_frame(frame, f"verify{i}")
    finally:
        svc.DEBUG_DUMP_RING_MAX = old_max
    dumps = [p.name for p in ring.iterdir() if D.DUMP_NAME_RE.match(p.name)]
    check("ring keeps DEBUG_DUMP_RING_MAX dump files", len(dumps) == 2, dumps)
    check("ring prune leaves a non-dump file alone", (ring / "keep-me.txt").exists())


# --- [5] remove_tree_no_follow ------------------------------------------------------------------

def test_remove_tree():
    print("[5] remove_tree_no_follow")
    victim = _ROOT / "victim2"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"k")
    tree = _ROOT / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "a.jpg").write_bytes(b"a")
    _junction(tree / "sub" / "link", victim)
    removed, problems = D.remove_tree_no_follow(tree)
    check("tree removed", not tree.exists() and problems == [], problems)
    check("junction target intact", (victim / "keep.txt").exists())


# --- [6] ctypes facts (8b-2) ----------------------------------------------------------------

def _facts(path: Path):
    import win32con  # type: ignore
    h = D._open_no_follow(str(path), win32con.READ_CONTROL)
    try:
        return D._file_facts(h)
    finally:
        D.win32file.CloseHandle(h)


def test_ctypes_facts():
    print("[6] ctypes file facts (8b-2)")
    home = _ROOT / "facts"
    (home / "misc").mkdir(parents=True)
    f = home / "config.toml"
    f.write_bytes(b"x")
    attrs, links, is_dir = _facts(home)
    check("directory: is_dir, not reparse", is_dir and not attrs & D._FILE_ATTRIBUTE_REPARSE_POINT,
          (hex(attrs), links, is_dir))
    attrs, links, is_dir = _facts(f)
    check("plain file: 1 link, not a directory", links == 1 and not is_dir, (hex(attrs), links))
    check("heal of a plain tree is ok", D.heal_data_dir(home).ok)

    os.link(f, home / "misc" / "second-name.toml")
    _a, links, _d = _facts(f)
    check("hard link: link count 2", links == 2, links)
    rep = D.heal_data_dir(home)
    check("hard-linked file fails the heal", rep.ok is False
          and any("hard-linked" in p for p in rep.problems), rep.problems[:3])
    (home / "misc" / "second-name.toml").unlink()
    check("heal ok again once the link is gone", D.heal_data_dir(home).ok)

    victim = _ROOT / "facts_victim"
    victim.mkdir()
    _junction(home / "misc" / "deep-link", victim)
    attrs, _l, _d = _facts(home / "misc" / "deep-link")
    check("junction: reparse attribute seen through the no-follow handle",
          bool(attrs & D._FILE_ATTRIBUTE_REPARSE_POINT), hex(attrs))
    rep = D.heal_data_dir(home)
    check("junction deep in the tree fails the heal", rep.ok is False
          and any("reparse point" in p and "deep-link" in p for p in rep.problems), rep.problems[:3])

    fresh = _ROOT / "fresh"
    fresh.mkdir()
    code = ("import sys; sys.path.insert(0, %r); from face_service import datadir as D; "
            "r = D.heal_data_dir(%r); print(r.ok, 'win32timezone' in sys.modules)"
            % (str(Path(__file__).resolve().parents[1]), str(fresh)))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, FACE_UNLOCK_HOME=str(_ROOT / "fresh-app")))
    check("fresh interpreter: heal ok and win32timezone NOT loaded",
          out.stdout.strip() == "True False", (out.stdout.strip(), out.stderr[-300:]))


def main() -> int:
    try:
        run_restoring(
            test_heal,
            test_fail_closed,
            test_reparse,
            test_purge,
            test_remove_tree,
            test_ctypes_facts,
        )
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(_ROOT)], capture_output=True)
    if FAILS:
        print(f"\nDATADIR ACL SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nDATADIR ACL SELFTEST OK: a Users:Modify tree heals to SELF/SYSTEM/BA with the secrets "
          "re-locked and a clean verification walk; a failed heal refuses unlock / unlock_gesture "
          "with insecure-data-dir after the existing gates, lockout-neutral; reparse points fail "
          "the heal and are never followed; purge and prune delete dump names only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
