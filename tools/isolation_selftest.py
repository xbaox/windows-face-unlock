#!/usr/bin/env python3
"""tools/isolation_selftest.py -- Stage 9 (act 9b R20; B14 N-01, N-02): the selftests cannot touch
the real data directory, and they clean up after themselves.

  [1] static: every tools/*_selftest.py calls tools.testhome.isolate() / own_root() at module level
      BEFORE its first import of the product (face_service, presence_monitor, installer, tools.*).
  [2] testhome.check_home truth table: the real data directory, a folder inside it, a folder that
      contains it, the temporary directory itself and anything outside it are refused.
  [3] N-01 canary: the destructive selftests (credentials, pipe_hardening, adaptive, stage4) are
      started with USERPROFILE pointing at a fake profile whose .face-unlock holds canary files and
      FACE_UNLOCK_HOME naming that folder -> each exits 2 and every canary byte is unchanged; a
      home outside the temporary directory -> exit 2 and the folder is never created; a
      face_service.config imported before isolation -> exit 2.
  [4] N-02 temp hygiene: the tests that used to leak %TEMP%\\faceunlock_* folders (credentials,
      pipe_hardening, gesture_round, pose_coach, watchdog) run with a fresh TEMP and no
      FACE_UNLOCK_HOME -> nothing named faceunlock_* is left behind.

Real data, the real pipe and the camera are never used; every child gets its own temporary TEMP.
Run from the repo root:  python -m tools.isolation_selftest
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_isolation_")

FAILS: list[str] = []
PRODUCT = ("face_service", "presence_monitor", "installer")
HELPERS = {"testhome"}


def check(name: str, cond: bool, detail=None) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond or detail is None else f" -- {detail}"))
    if not cond:
        FAILS.append(name)


def _is_isolation_call(node: ast.stmt) -> bool:
    call = getattr(node, "value", None)
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return False
    f = call.func
    return isinstance(f.value, ast.Name) and f.value.id == "testhome" and f.attr in ("isolate", "own_root")


def _product_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] in PRODUCT or
                   (a.name.startswith("tools.") and a.name.split(".")[1] not in HELPERS)
                   for a in node.names)
    if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        top = node.module.split(".")[0]
        if top in PRODUCT:
            return True
        if node.module == "tools":
            return any(a.name not in HELPERS for a in node.names)
        if top == "tools":
            return node.module.split(".")[1] not in HELPERS
    return False


def test_static() -> None:
    print("[1] every selftest isolates before its first product import")
    files = sorted((REPO / "tools").glob("*_selftest.py"))
    check(f"{len(files)} selftests found", len(files) >= 38, len(files))
    for f in files:
        body = ast.parse(f.read_text(encoding="utf-8")).body
        iso = next((i for i, n in enumerate(body) if _is_isolation_call(n)), None)
        imp = next((i for i, n in enumerate(body) if _product_import(n)), None)
        ok = iso is not None and (imp is None or iso < imp)
        check(f"{f.name}", ok, f"isolation stmt #{iso}, first product import #{imp}")


def test_check_home() -> None:
    print("[2] testhome.check_home")
    real = testhome.real_data_dir()
    tmp = Path(tempfile.gettempdir())
    check("the real data directory is refused", testhome.check_home(real) is not None)
    check("a folder inside it is refused", testhome.check_home(real / "sub") is not None)
    check("a folder that contains it is refused", testhome.check_home(real.parent) is not None)
    check("the temporary directory itself is refused", testhome.check_home(tmp) is not None)
    check("a folder outside the temporary directory is refused",
          testhome.check_home(REPO / "not-a-temp-home") is not None)
    check("a folder inside the temporary directory is accepted",
          testhome.check_home(tmp / "faceunlock_x" / "home") is None)


def _child_env(temp: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "FACE_UNLOCK_HOME"}
    env["TEMP"] = env["TMP"] = str(temp)
    env.update(extra)
    return env


def _snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_canary(work: Path) -> None:
    print("[3] N-01: a canary data directory is refused and left byte-identical")
    profile = work / "fakeprofile"
    canary = profile / ".face-unlock"
    canary.mkdir(parents=True)
    (canary / "config.toml").write_text('liveness_mode = "paranoid"\n', encoding="utf-8")
    (canary / "credentials.bin").write_bytes(b"CANARY-CREDENTIALS")
    (canary / "pipe_entropy.bin").write_bytes(b"CANARY-ENTROPY")
    (canary / "adaptive.npz").write_bytes(b"CANARY-ADAPTIVE")
    before = _snapshot(canary)
    temp = work / "t3"
    temp.mkdir()
    for mod in ("credentials_selftest", "pipe_hardening_selftest", "adaptive_selftest", "stage4_selftest"):
        env = _child_env(temp, USERPROFILE=str(profile), FACE_UNLOCK_HOME=str(canary))
        p = subprocess.run([sys.executable, "-m", f"tools.{mod}"], cwd=str(REPO), env=env,
                           capture_output=True, text=True, timeout=120)
        check(f"{mod}: FACE_UNLOCK_HOME = the (fake) real data dir -> exit 2, REFUSED",
              p.returncode == testhome.REFUSED_EXIT and "REFUSED" in p.stderr,
              (p.returncode, p.stderr[-300:]))
    check("every canary file is byte-identical and nothing was added", _snapshot(canary) == before)

    outside = REPO / "build-tests" / "never-a-home-9c6"
    env = _child_env(temp, FACE_UNLOCK_HOME=str(outside))
    p = subprocess.run([sys.executable, "-m", "tools.credentials_selftest"], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=120)
    check("a home outside the temporary directory -> exit 2", p.returncode == testhome.REFUSED_EXIT,
          (p.returncode, p.stderr[-300:]))
    check("... and that folder was never created", not outside.exists())

    code = ("import os, sys; sys.path.insert(0, r'%s'); "
            "os.environ['FACE_UNLOCK_HOME'] = r'%s'; import face_service.config; "
            "os.environ['FACE_UNLOCK_HOME'] = r'%s'; "
            "from tools import testhome; testhome.isolate('x_')") % (REPO, temp / "a", temp / "b")
    p = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=_child_env(temp),
                       capture_output=True, text=True, timeout=60)
    check("face_service.config imported before isolation -> exit 2",
          p.returncode == testhome.REFUSED_EXIT and "imported before isolation" in p.stderr,
          (p.returncode, p.stderr[-300:]))


def test_temp_hygiene(work: Path) -> None:
    print("[4] N-02: no faceunlock_* folder is left in TEMP")
    for mod in ("credentials_selftest", "pipe_hardening_selftest", "gesture_round_selftest",
                "pose_coach_selftest", "watchdog_selftest"):
        temp = work / f"t4-{mod}"
        temp.mkdir()
        p = subprocess.run([sys.executable, "-m", f"tools.{mod}"], cwd=str(REPO), env=_child_env(temp),
                           capture_output=True, text=True, timeout=300)
        left = sorted(x.name for x in temp.iterdir() if x.name.startswith("faceunlock_"))
        check(f"{mod}: passes and leaves no faceunlock_* in TEMP", p.returncode == 0 and not left,
              (p.returncode, left, p.stdout[-300:]))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="faceunlock_isowork_"))
    try:
        test_static()
        test_check_home()
        test_canary(work)
        test_temp_hygiene(work)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    if FAILS:
        print(f"\nISOLATION SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nISOLATION SELFTEST OK: every selftest isolates before importing the product; the real "
          "data directory, a home outside TEMP and a late isolation are refused with nothing "
          "touched; the tests that used to leak temp folders leave none.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
