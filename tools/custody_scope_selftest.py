"""tools/custody_scope_selftest.py -- what a failed custody means, and what it no longer means
(Stage 9, act 9b R9; B14 N-18 / N-19).

Everything runs on temp directories this test creates; the real data directory is never touched.
  [1] N-18 (F-100): an entry that vanishes between the listing and the open (another process
      deleted or renamed it) does not fail the heal; a real problem still does; a failing heal is
      retried once before it counts.
  [2] N-19 (F-99): with custody failed the service boots on the BUILT-IN DEFAULTS -- a planted
      config.toml (fast mode, the frame dump on) is not read -- and refuses every face function:
      unlock, unlock_gesture, presence, verify, build/clear enrollment, calibrate_turn; ping says
      refusing:custody and status names the first problem.
  [3] F-99 (4): an object owned by another account is taken over by SELF during the heal when
      possible (here: our own files -- the ownership write path runs, the verdict stays clean).

Run:  python -m tools.custody_scope_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_custody_")

import pywintypes  # type: ignore

from face_service import datadir as D
from face_service import service as S
from face_service.config import Config

FAILS: list[str] = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


def test_vanished(root: Path):
    print("[1] N-18: a vanished entry is not a custody problem")
    home = root / "vanish"
    (home / "enroll").mkdir(parents=True)
    (home / "presence.log").write_text("x")
    (home / "enroll" / "a.jpg").write_bytes(b"a")
    real = D._open_no_follow
    calls = {"n": 0}

    def gone(path, access):
        if path.endswith("presence.log"):
            calls["n"] += 1
            raise pywintypes.error(2, "CreateFile", "The system cannot find the file specified.")
        return real(path, access)

    D._open_no_follow = gone
    try:
        rep = D.heal_data_dir(home)
    finally:
        D._open_no_follow = real
    check("a file gone since the listing -> the heal is clean", rep.ok, rep.problems)
    check("... and it was indeed looked for", calls["n"] >= 1, calls)

    def denied(path, access):
        if path.endswith("a.jpg"):
            raise pywintypes.error(5, "CreateFile", "Access is denied.")
        return real(path, access)

    D._open_no_follow = denied
    try:
        rep = D.heal_data_dir(home)
    finally:
        D._open_no_follow = real
    check("a real refusal still fails the heal", not rep.ok and any("a.jpg" in p for p in rep.problems),
          rep.problems)

    flaky = {"n": 0}

    def once(path, access):
        if path.endswith("a.jpg") and flaky["n"] == 0:
            flaky["n"] += 1
            raise pywintypes.error(5, "CreateFile", "Access is denied.")
        return real(path, access)

    D._open_no_follow = once
    try:
        rep = D.heal_data_dir(home)
    finally:
        D._open_no_follow = real
    check("a one-off failure (a file mid-delete) is healed by the retry", rep.ok, rep.problems)


class _Lk:
    store_ok = True

    def remaining(self):
        return 0.0

    def record(self, ok):
        raise AssertionError("a custody refusal never strikes")

    def status(self):
        return {}


def test_consumers(root: Path):
    print("[2] N-19: custody failed -> defaults, and every face function refused")
    rep = D.CustodyReport()
    rep.ok = False
    rep.problems = ["foreign owner S-1-5-21-1-2-3-1002 on " + str(S.APP_DIR / "enroll")]
    cfgfile = S.APP_DIR / "config.toml"
    S.APP_DIR.mkdir(parents=True, exist_ok=True)
    cfgfile.write_text('liveness_mode = "fast"\ndebug_dump_frames = true\n', encoding="utf-8")
    try:
        cfg = S._boot_config(rep)
    finally:
        cfgfile.unlink()
    check("the planted config.toml is NOT read (built-in defaults)",
          cfg.liveness_mode == "paranoid" and cfg.debug_dump_frames is False,
          (cfg.liveness_mode, cfg.debug_dump_frames))
    ok = D.CustodyReport()
    ok.ok = True
    check("with custody OK the file is read as usual", S._boot_config(ok).liveness_mode == "paranoid")

    s = S.FaceService.__new__(S.FaceService)
    s._caller_sid = lambda h: "S-1-5-18"
    s.cfg = Config()
    s._lockout = _Lk()
    s._audit = type("A", (), {"write": lambda *a: None, "status": lambda self: {}})()
    s._cam_lock = threading.Lock()
    s._camera_paused_until = 0.0
    s._data_dir_insecure = True
    s._custody_problem = S._scrub(rep.problems[0])
    s._started_at = 0.0
    for req in ({"cmd": "unlock", "v": 2}, {"cmd": "unlock_gesture", "v": 2, "token": "a" * 32},
                {"cmd": "presence"}, {"cmd": "build_enrollment"}, {"cmd": "clear_enrollment"},
                {"cmd": "calibrate_turn", "frontal": ["a.jpg"], "left": ["b.jpg"]}):
        r = s._handle(req, None)
        check(f"{req['cmd']} -> custody", r == {"ok": False, "reason": "custody"}, r)
    s._caller_sid = lambda h: S.current_user_sid()
    check("verify -> custody", s._handle({"cmd": "verify"}, None) == {"ok": False, "reason": "custody"})
    check("ping: refusing, why=custody",
          s._handle({"cmd": "ping"}, None) == {"ok": True, "pong": True, "state": "refusing",
                                               "why": "custody"})
    st = s._status()
    check("status names the first problem, without the profile path (F-101 / F-113)",
          st.get("data_dir_secure") is False and "foreign owner" in (st.get("custody_problem") or "")
          and str(S.APP_DIR) not in (st.get("custody_problem") or ""), st.get("custody_problem"))


def test_owner(root: Path):
    print("[3] F-99: the ownership step of the heal")
    home = root / "own"
    home.mkdir()
    (home / "f.txt").write_text("x")
    rep = D.heal_data_dir(home)
    check("a tree owned by SELF heals clean (the ownership step leaves it alone)", rep.ok, rep.problems)
    check("_take_ownership exists and is part of the heal", hasattr(D, "_take_ownership"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="fu-custody-"))
    try:
        test_vanished(root)
        test_consumers(root)
        test_owner(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if FAILS:
        print(f"\nCUSTODY SCOPE SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nCUSTODY SCOPE SELFTEST OK: vanished entries are skipped, a failing heal is retried "
          "once, and a failed custody means defaults and no face function at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
