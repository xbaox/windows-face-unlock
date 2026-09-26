#!/usr/bin/env python3
"""tools/verifier_selftest.py -- Stage 9 (D-125, B12a-11): verify_frozen_entrypoints passes 1-6 on
fixtures, no build needed (pass 7 is covered by frozen_custody_selftest).

  [1] SOURCE: a relative import inside a function body is found; an absolute one is not; the real
      three entry scripts are clean.
  [2] FROZEN: the bytecode reader finds a level>0 IMPORT_NAME in a nested code object and nothing
      in absolute imports.
  [3] PYZ: every required module in its own EXE's PYZ -> clean; one missing, an unreadable PYZ and
      an empty PYZ -> one failure each.
  [4] NATIVE: a fixture numpy extension that names numpy._core._exceptions -> clean when the PYZ
      has it, a failure when it does not; no numpy.libs -> a failure.
  [5] MINES: a fixture bundle with all three mines covered -> clean; removing _cyutility, one
      scipy._external anchor or meanshape_68.pkl adds exactly one failure each.
  [6] CLASS: the vendor scan flags sys.frozen / _MEIPASS / __import__(<computed>) and not a constant
      import_module; REVIEWED wildcards match sub-modules only; on a fixture bundle an unreviewed
      frozen branch in vendor code fails, a clean vendor module passes, and an extension naming a
      real module that did not ship fails.

Private temp directories only. Run:  python -m tools.verifier_selftest
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_verifier_")

from tools import verify_frozen_entrypoints as V  # noqa: E402

FAILS: list[str] = []
TAG = f"cp{sys.version_info.major}{sys.version_info.minor}-win_amd64"


def check(name, cond, got=None):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


class Stub:
    """The attributes of V.Bundle the passes read."""

    def __init__(self, root: Path, pyz_by_exe=None, pyz_errors=None):
        self.dist = root
        self.internal = root / "_internal"
        self.pyz_by_exe = dict(pyz_by_exe or {})
        self.pyz_errors = dict(pyz_errors or {})
        self.pyz_all = set().union(*self.pyz_by_exe.values()) if self.pyz_by_exe else set()
        self.pyds = sorted(self.internal.rglob("*.pyd")) if self.internal.is_dir() else []


def _failures(fn, *args) -> int:
    rep = V.Report()
    fn(rep, *args)
    return rep.failures


def test_source(tmp: Path) -> None:
    print("[1] pass 1 SOURCE")
    bad = tmp / "entry_bad.py"
    bad.write_text("import os\n\ndef f():\n    from .sibling import thing\n    return thing\n", encoding="utf-8")
    good = tmp / "entry_good.py"
    good.write_text("import os\nfrom face_service import config\n", encoding="utf-8")
    hits = V.source_relative_imports(bad)
    check("a relative import inside a function body is found", len(hits) == 1 and ":4" in hits[0], hits)
    check("absolute imports are not flagged", V.source_relative_imports(good) == [])
    check("the spec's three entry scripts are clean", _failures(lambda rep: V.pass_source(rep)) == 0)


def test_frozen() -> None:
    print("[2] pass 2 FROZEN (bytecode)")
    code = compile("import os\ndef outer():\n    def inner():\n        from . import x\n    return inner\n",
                   "fixture.py", "exec")
    hits = V.bytecode_relative_imports(code)
    check("a level-1 import in a nested code object is found", len(hits) == 1 and "level=1" in hits[0], hits)
    clean = compile("import os\nfrom json import dumps\n", "fixture.py", "exec")
    check("absolute imports have level 0", V.bytecode_relative_imports(clean) == [])


def test_pyz(tmp: Path) -> None:
    print("[3] pass 3 PYZ")
    full = {exe: {m for m, e in V.REQUIRED_MODULES.items() if e == exe} | {"json"}
            for _r, exe in V.ENTRIES}
    check("every required module in its own EXE -> clean", _failures(V.pass_pyz, Stub(tmp, full)) == 0)
    missing = {k: set(v) for k, v in full.items()}
    missing["face_unlock_tray.exe"].discard("presence_monitor.password_gui")
    check("one module missing from its EXE -> 1 failure", _failures(V.pass_pyz, Stub(tmp, missing)) == 1)
    moved = {k: set(v) for k, v in full.items()}
    moved["face_unlock_tray.exe"].discard("face_service.taskreg")
    moved["face_service.exe"].add("face_service.taskreg")
    check("present only in ANOTHER EXE's PYZ -> still a failure (D-121)",
          _failures(V.pass_pyz, Stub(tmp, moved)) == 1)
    errs = {k: v for k, v in full.items() if k != "face_service.exe"}
    n = _failures(V.pass_pyz, Stub(tmp, errs, {"face_service.exe": "bad archive"}))
    check("an unreadable PYZ -> a failure (plus its required module)", n == 2, n)
    empty = dict(full)
    empty["face_unlock_watchdog.exe"] = set()
    n = _failures(V.pass_pyz, Stub(tmp, empty))
    check("an empty PYZ -> a failure (plus its required module)", n == 2, n)


def test_native(tmp: Path) -> None:
    print("[4] pass 4 NATIVE")
    root = tmp / "native"
    core = root / "_internal" / "numpy" / "_core"
    core.mkdir(parents=True)
    (core / f"_multiarray_umath.{TAG}.pyd").write_bytes(b"MZ..numpy._core._exceptions..numpy.ndarray..")
    libs = root / "_internal" / "numpy.libs"
    libs.mkdir()
    (libs / "openblas.dll").write_bytes(b"MZ")
    orig = V.pe_content_digest
    V.pe_content_digest = lambda path: "same"          # the fixture is not a real PE
    try:
        have = {"face_service.exe": {"numpy._core._exceptions"}}
        check("the extension's by-name import is in the PYZ -> clean",
              _failures(V.pass_native, Stub(root, have)) == 0)
        check("... missing from the PYZ -> 1 failure",
              _failures(V.pass_native, Stub(root, {"face_service.exe": {"json"}})) == 1)
        shutil.rmtree(libs)
        check("no numpy.libs -> a failure", _failures(V.pass_native, Stub(root, have)) == 1)
    finally:
        V.pe_content_digest = orig


def _mines_fixture(root: Path, dirs):
    internal = root / "_internal"
    scipy = internal / "scipy"
    scipy.mkdir(parents=True)
    (scipy / f"_cyutility.{TAG}.pyd").write_bytes(b"MZ")
    (scipy / f"_user.{TAG}.pyd").write_bytes(b"MZ..scipy._cyutility..")
    objects = internal / "objects"
    objects.mkdir()
    src_init = V.resolve_source("insightface.data", dirs)
    if src_init is not None and (src_init.parent / "objects").is_dir():
        for f in (src_init.parent / "objects").iterdir():
            if f.is_file():
                shutil.copyfile(f, objects / f.name)
    if not (objects / V.MINE3_FILE).is_file():
        (objects / V.MINE3_FILE).write_bytes(b"pkl")
    tree = V.resolve_package_dir("scipy._external", dirs)
    names = set(V.MINE2_MODULES) | {V.MINE3_READER}
    if tree is not None:
        for p in tree.rglob("*.py"):
            parts = list(p.relative_to(tree.parent.parent).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            names.add(".".join(parts))
    return {"face_service.exe": names}


def test_mines(tmp: Path) -> None:
    print("[5] pass 5 MINES")
    dirs = V.site_dirs()
    root = tmp / "mines"
    pyz = _mines_fixture(root, dirs)
    base = _failures(V.pass_mines, Stub(root, pyz), dirs)
    check("all three mines covered -> clean", base == 0, base)
    anchor = V.MINE2_MODULES[0]
    no2 = {"face_service.exe": pyz["face_service.exe"] - {anchor}}
    n = _failures(V.pass_mines, Stub(root, no2), dirs)
    check(f"mine 2: {anchor} missing -> failures", n >= 1, n)
    mine3 = root / "_internal" / "objects" / V.MINE3_FILE
    kept = mine3.read_bytes()
    mine3.unlink()
    n = _failures(V.pass_mines, Stub(root, pyz), dirs)
    check("mine 3: meanshape_68.pkl missing -> failures", n >= 1, n)
    mine3.write_bytes(kept)
    for f in (root / "_internal" / "scipy").glob("_cyutility.*"):
        f.unlink()
    n = _failures(V.pass_mines, Stub(root, pyz), dirs)
    check("mine 1: _cyutility missing while an extension names it -> exactly 1 failure", n == 1, n)


def test_class(tmp: Path) -> None:
    print("[6] pass 6 CLASS")
    src = ("import sys, importlib\n"
           "if getattr(sys, 'frozen', False):\n    base = sys._MEIPASS\n"
           "mod = __import__(__package__ + '.fft')\n"
           "ok = importlib.import_module('json')\n")
    kinds = sorted({k for _l, k in V.scan_vendor_source(src, "x.py")})
    check("sys.frozen, _MEIPASS and a computed __import__ are flagged; a constant import_module is not",
          kinds == ["_MEIPASS", "__import__", "frozen"], kinds)
    k, keys = V.reviewed_kinds("sympy.core.basic")
    check("a REVIEWED wildcard (sympy.*) covers its sub-modules", bool(k) and keys == ["sympy.*"], keys)
    k, keys = V.reviewed_kinds("sympyx")
    check("... but not a module that merely shares the prefix", keys == [], keys)
    check("an unlisted module is not reviewed", V.reviewed_kinds("fu_fixture_vendor_9c6") == (frozenset(), []))

    dirs = V.site_dirs()
    root = tmp / "class"
    internal = root / "_internal"
    vend = internal / "fu_fixture_vendor_9c6"
    vend.mkdir(parents=True)
    (vend / "__init__.py").write_text("import json\nVALUE = 1\n", encoding="utf-8")
    bundle = V.Bundle(root)
    check("a clean vendor module in the bundle -> clean", _failures(V.pass_class, bundle, dirs) == 0)
    (vend / "__init__.py").write_text("import sys\nFROZEN = getattr(sys, 'frozen', False)\n",
                                      encoding="utf-8")
    bundle = V.Bundle(root)
    check("an unreviewed frozen branch in vendor code -> a failure",
          _failures(V.pass_class, bundle, dirs) == 1)
    (vend / "__init__.py").write_text("import json\nVALUE = 1\n", encoding="utf-8")
    sub = internal / "scipy" / "special"
    sub.mkdir(parents=True)
    (sub / f"_fixture.{TAG}.pyd").write_bytes(b"MZ\x00scipy.special._ufuncs\x00")
    bundle = V.Bundle(root)
    real = V.is_real_vendor_module("scipy.special._ufuncs", dirs)
    n = _failures(V.pass_class, bundle, dirs)
    check("an extension naming a real module that did not ship -> a failure",
          real and n >= 1, (real, n))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="faceunlock_verifier_fx_"))
    try:
        test_source(tmp)
        test_frozen()
        test_pyz(tmp)
        test_native(tmp)
        test_mines(tmp)
        test_class(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nVERIFIER SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nVERIFIER SELFTEST OK: passes 1-6 of the frozen gate each fail on the defect they exist "
          "for and stay clean on a correct fixture.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
