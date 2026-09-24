"""tools/frozen_custody_selftest.py -- Stage 8b-2 proof for the frozen-custody fix (no build needed).

  [1] verify_frozen_entrypoints pass 7 ("lazy"): pywin32's native lazy imports must be in every
      EXE's PYZ. A fixture bundle WITHOUT win32timezone fails (and main() returns rc=1); the same
      fixture with it passes; the native module's claim is checked against the DLL bytes.
  [2] the spec names win32timezone and the three new 8b modules in the hidden imports shared by
      all three EXEs.
  [3] the hidden mode `python -m face_service --selfcheck-custody <dir> --out <file>` in SOURCE:
      a Users:Modify tree with fake secrets -> exit 0, ok, no foreign ACE, secrets SELF+SYSTEM;
      the same with a junction inside -> exit 1, ok false; bad usage -> exit 2; FACE_UNLOCK_HOME
      is never created (the mode acts on its argument only).

Private temp directories only. Run:  python -m tools.frozen_custody_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import verify_frozen_entrypoints as V

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


class _StubBundle:
    def __init__(self, root: Path, pyz: dict):
        self.internal = root / "_internal"
        self.pyz_by_exe = pyz


def test_lazy_pass(tmp: Path):
    print("[1] pass 7 (lazy pywin32 imports)")
    native = tmp / "_internal" / "pywin32_system32"
    native.mkdir(parents=True)
    (native / "pywintypes312.dll").write_bytes(b"MZ....win32timezone....")
    exes = [name for _rel, name in V.ENTRIES]
    good = {e: {"win32timezone", "json"} for e in exes}
    rep = V.Report()
    V.pass_lazy(rep, _StubBundle(tmp, good))
    check("fixture WITH win32timezone in every PYZ -> 0 failures", rep.failures == 0, rep.failures)
    missing = dict(good)
    missing[exes[0]] = {"json"}
    rep = V.Report()
    V.pass_lazy(rep, _StubBundle(tmp, missing))
    check("fixture WITHOUT win32timezone in one EXE -> failure", rep.failures == 1, rep.failures)
    orig = V.Bundle
    V.Bundle = lambda dist: _StubBundle(tmp, missing)
    try:
        rc = V.main(["--dist", str(tmp), "--passes", "lazy"])
    finally:
        V.Bundle = orig
    check("main() on that fixture -> rc=1", rc == 1, rc)
    (native / "pywintypes312.dll").unlink()
    rep = V.Report()
    V.pass_lazy(rep, _StubBundle(tmp, good))
    check("no pywintypes*.dll in the bundle -> failure", rep.failures == 1, rep.failures)


def test_spec():
    print("[2] spec hidden imports")
    spec = (REPO / "installer" / "windows_face_unlock.spec").read_text(encoding="utf-8")
    head = spec.split("service_analysis = Analysis(", 1)[0]
    for mod in ("win32timezone", "face_service.datadir", "face_service.pipe_io",
                "face_service.selfcheck"):
        check(f'HIDDEN names "{mod}"', f'"{mod}"' in head)
    check("all three Analyses use HIDDEN", spec.count("hiddenimports=HIDDEN") == 3,
          spec.count("hiddenimports=HIDDEN"))


def _seed(home: Path, junction_to: "Path | None" = None):
    (home / "enroll").mkdir(parents=True)
    (home / "credentials.bin").write_bytes(os.urandom(96))
    (home / "pipe_entropy.bin").write_bytes(os.urandom(32))
    subprocess.run(["icacls", str(home), "/grant", "*S-1-5-32-545:(OI)(CI)(M)"],
                   capture_output=True, check=True)
    if junction_to is not None:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "enroll" / "link"), str(junction_to)],
                       capture_output=True, check=True)


def _run(args, never: Path):
    env = dict(os.environ, FACE_UNLOCK_HOME=str(never), PYTHONUTF8="1")
    return subprocess.run([sys.executable, "-m", "face_service", *args], cwd=str(REPO), env=env,
                          capture_output=True, text=True, timeout=120)


def test_mode(tmp: Path):
    print("[3] --selfcheck-custody in source")
    never = tmp / "app-dir-must-not-appear"
    clean = tmp / "clean"
    _seed(clean)
    out = tmp / "clean.json"
    p = _run(["--selfcheck-custody", str(clean), "--out", str(out)], never)
    data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    check("clean: exit 0", p.returncode == 0, (p.returncode, p.stderr[-300:]))
    check("clean: ok, heal ok, relocked 2",
          data.get("ok") is True and data["heal"]["ok"] and data["heal"]["relocked"] == 2,
          data.get("heal"))
    check("clean: no foreign ACE, no verify problem",
          data.get("foreign_ace_problems") == 0 and data.get("verify_problems") == [],
          data.get("verify_problems"))
    check("clean: both secrets protected SELF+SYSTEM",
          sorted(data.get("secrets", {})) == ["credentials.bin", "pipe_entropy.bin"]
          and all(v["self_system_only"] for v in data["secrets"].values()), data.get("secrets"))

    victim = tmp / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_bytes(b"k")
    junc = tmp / "junction"
    _seed(junc, junction_to=victim)
    out = tmp / "junction.json"
    p = _run(["--selfcheck-custody", str(junc), "--out", str(out)], never)
    data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    check("junction: exit 1, ok false", p.returncode == 1 and data.get("ok") is False,
          (p.returncode, data.get("ok")))
    check("junction: named in the problems",
          any("reparse point" in x for x in data.get("heal", {}).get("problems", [])),
          data.get("heal", {}).get("problems"))
    check("junction target untouched", (victim / "keep.txt").exists())

    p = _run(["--selfcheck-custody", str(clean)], never)
    check("usage error (no --out) -> exit 2", p.returncode == 2, p.returncode)
    check("FACE_UNLOCK_HOME never created", not never.exists())


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="faceunlock_frozencustody_"))
    try:
        test_lazy_pass(tmp / "lazy")
        test_spec()
        test_mode(tmp / "mode")
    finally:
        subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", str(tmp)], capture_output=True)
    if FAILS:
        print(f"\nFROZEN CUSTODY SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nFROZEN CUSTODY SELFTEST OK: pass 7 fails a bundle without win32timezone (rc=1) and "
          "passes one with it; the spec names it for all three EXEs; --selfcheck-custody heals and "
          "locks a seeded tree (exit 0), refuses a tree with a junction (exit 1), acts on its "
          "argument only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
