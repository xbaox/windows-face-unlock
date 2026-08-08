"""Static gate over a PyInstaller build: prove the bundle can actually import itself.

Run after PyInstaller and BEFORE Inno Setup:

    .venv\\Scripts\\python.exe tools\\verify_frozen_entrypoints.py

Exit code 0 means the build is clear to go to ISCC. This does NOT replace the
operator dist-smoke -- it is the half of the gate that can be automated, and it
exists because two separate Stage-7 blocks shipped an unrunnable build that every
check of the day called green.

Four passes, each answering a question the others cannot:

  1 SOURCE  -- AST over the three scripts the spec names as Analysis targets: is
               there a relative import anywhere, including inside a function body?
               A frozen entry point is the top-level script `__main__` with no
               package context, so a leading dot there is an ImportError at
               runtime. 7d fixed what modulegraph COLLECTED by naming the modules
               as hidden imports and was read as having fixed this; it had not,
               and face_unlock_tray.exe died on every start.

  2 FROZEN  -- the same question asked of the ARTEFACT. Pulls the PYSOURCE entry
               out of each built EXE, unmarshals it, and disassembles every code
               object looking for IMPORT_NAME with a non-zero level operand.
               Immune to a stale source tree, a stale build, or a spec pointing
               somewhere unexpected -- and it also asserts each EXE was built from
               the file the spec claims.

  3 PYZ     -- the absolute targets those entry points name are really in the
               archive, so an import that now resolves also finds something.

  4 NATIVE  -- the one that no amount of PYZ reading would have caught. numpy's C
               extension imports Python modules BY NAME from C, which modulegraph
               cannot see and warn-*.txt never mentions. In 7g the bundle had a
               byte-perfect _multiarray_umath.pyd, a complete numpy.libs, every PE
               dependency resolvable -- and no numpy._core._exceptions, so the
               frozen service died with "Importing the numpy C-extensions failed"
               ninety seconds in, the first time recognition touched numpy. This
               pass reads the module-like strings embedded in the extension,
               keeps the ones that are real modules on disk, and requires each to
               be in the PYZ.

The lesson pass 4 encodes: verifying a bundle by reading its PYZ proves what was
collected, never that the result runs. Native extensions fail on their own terms.
"""
from __future__ import annotations

import ast
import dis
import hashlib
import marshal
import re
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist" / "WindowsFaceUnlock"
INTERNAL = DIST / "_internal"
BUILD = REPO / "build" / "windows_face_unlock"

# The spec's three Analysis targets, paired with the EXE each one produces.
ENTRIES = (
    ("face_service/__main__.py", "face_service.exe"),
    ("presence_monitor/__main__.py", "face_unlock_tray.exe"),
    ("tools/watchdog.py", "face_unlock_watchdog.exe"),
)

# Absolute targets the entry points import by name.
REQUIRED_MODULES = (
    "face_service.service",
    "presence_monitor.monitor",
    "presence_monitor.enroll_gui",
    "presence_monitor.password_gui",
    "tools.pipe_client",
)

MODULE_NAME_RE = re.compile(rb"numpy(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def fail(self, msg: str) -> None:
        self.failures += 1
        print(f"  FAIL  {msg}")

    def detail(self, msg: str) -> None:
        print(f"          {msg}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --------------------------------------------------------------------------- 1
def source_relative_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        f":{n.lineno}  from {'.' * n.level}{n.module or ''} import ..."
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and (n.level or 0) > 0
    ]


def pass_source(rep: Report) -> None:
    header("PASS 1 -- SOURCE (AST over the spec's three Analysis targets)")
    for rel, _exe in ENTRIES:
        path = REPO / rel
        if not path.is_file():
            rep.fail(f"{rel}: missing")
            continue
        hits = source_relative_imports(path)
        if hits:
            rep.fail(rel)
            for h in hits:
                rep.detail(h)
        else:
            rep.ok(rel)


# --------------------------------------------------------------------------- 2
def walk_code(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_code"):
            yield from walk_code(const)


def bytecode_relative_imports(code) -> list[str]:
    """IMPORT_NAME whose level operand -- the first of the two preceding LOAD_CONSTs -- is non-zero."""
    found = []
    for block in walk_code(code):
        instrs = list(dis.get_instructions(block))
        for i, ins in enumerate(instrs):
            if ins.opname != "IMPORT_NAME":
                continue
            level = None
            for back in (i - 2, i - 1):
                if back >= 0 and instrs[back].opname == "LOAD_CONST" and isinstance(instrs[back].argval, int):
                    level = instrs[back].argval
                    break
            if level:
                found.append(f"{block.co_qualname} -> level={level} import {ins.argval}")
    return found


def frozen_entry_code(exe: Path):
    """Return (entry_name, code_object) for the EXE's entry script.

    The bootstrap and the rthooks are PYSOURCE entries too; the entry script is
    the last one, which is the order PyInstaller writes them in.
    """
    reader = CArchiveReader(str(exe))
    pysource = [name for name, meta in reader.toc.items() if meta[-1] == "s"]
    if not pysource:
        raise RuntimeError("no PYSOURCE entries in the archive")
    name = pysource[-1]
    return name, marshal.loads(reader.extract(name))


def pass_frozen(rep: Report) -> None:
    header("PASS 2 -- FROZEN (bytecode of the entry script inside each built EXE)")
    for rel, exe_name in ENTRIES:
        exe = DIST / exe_name
        if not exe.is_file():
            rep.fail(f"{exe_name}: not built")
            continue
        try:
            entry_name, code = frozen_entry_code(exe)
        except Exception as exc:  # noqa: BLE001 -- report, never mask
            rep.fail(f"{exe_name}: cannot read entry code ({exc})")
            continue
        hits = bytecode_relative_imports(code)
        origin = Path(code.co_filename).as_posix()
        if hits:
            rep.fail(f"{exe_name}  entry '{entry_name}'  <- {origin}")
            for h in hits:
                rep.detail(h)
        elif not origin.endswith(rel):
            rep.fail(f"{exe_name}: built from {origin}, expected {rel}")
        else:
            rep.ok(f"{exe_name}  entry '{entry_name}'  <- {origin}")


# --------------------------------------------------------------------------- 3
def pyz_paths() -> list[Path]:
    return sorted(BUILD.glob("PYZ-*.pyz"))


def pyz_modules() -> set[str]:
    names: set[str] = set()
    for pyz in pyz_paths():
        names |= set(ZlibArchiveReader(str(pyz)).toc.keys())
    return names


def pass_pyz(rep: Report, names: set[str]) -> None:
    header("PASS 3 -- PYZ (the absolute targets are present in the archive)")
    print(f"  read {len(pyz_paths())} PYZ archive(s), {len(names)} module names")
    for mod in REQUIRED_MODULES:
        if mod in names:
            rep.ok(mod)
        else:
            rep.fail(f"{mod} absent from PYZ")


# --------------------------------------------------------------------------- 4
def numpy_site_dir() -> Path | None:
    try:
        import numpy
    except ImportError:
        return None
    return Path(numpy.__file__).parent


def is_real_module(name: str, numpy_dir: Path) -> bool:
    """True if the dotted name maps to a .py on disk under the numpy package.

    The extension embeds ~120 module-like strings, but most are type names
    (numpy.ndarray, numpy.dtypes.BoolDType) rather than importable modules. The
    filesystem is the arbiter -- anything else would be guesswork.
    """
    parts = name.split(".")[1:]
    if not parts:
        return False
    base = numpy_dir.joinpath(*parts)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def pass_native(rep: Report, names: set[str]) -> None:
    header("PASS 4 -- NATIVE (numpy's C extension and what it imports by name)")
    numpy_dir = numpy_site_dir()
    if numpy_dir is None:
        rep.fail("numpy is not importable in this interpreter; cannot verify the bundle")
        return

    tag = f"cp{sys.version_info.major}{sys.version_info.minor}-win_amd64"
    ext_rel = Path("numpy") / "_core" / f"_multiarray_umath.{tag}.pyd"
    bundled = INTERNAL / ext_rel
    installed = numpy_dir / "_core" / f"_multiarray_umath.{tag}.pyd"

    if not bundled.is_file():
        rep.fail(f"{ext_rel.as_posix()} absent from the bundle")
        return
    rep.ok(f"{ext_rel.as_posix()} present ({bundled.stat().st_size:,} bytes)")

    if installed.is_file():
        h_b = hashlib.sha256(bundled.read_bytes()).hexdigest()
        h_i = hashlib.sha256(installed.read_bytes()).hexdigest()
        if h_b == h_i:
            rep.ok(f"identical to the installed numpy ({h_b[:16]}...)")
        else:
            rep.fail("the bundled extension differs from the installed one")

    libs = INTERNAL / "numpy.libs"
    if libs.is_dir() and any(libs.glob("*.dll")):
        rep.ok(f"numpy.libs present ({len(list(libs.glob('*.dll')))} DLL(s))")
    else:
        rep.fail("numpy.libs is missing or empty -- the BLAS backing the extension did not ship")

    blob = bundled.read_bytes()
    embedded = sorted({m.decode() for m in MODULE_NAME_RE.findall(blob)})
    real = [n for n in embedded if is_real_module(n, numpy_dir)]
    print(f"  {len(embedded)} module-like strings embedded, {len(real)} are real modules on disk")
    for mod in real:
        # The extension itself is a .pyd on disk, never a PYZ entry.
        if mod.endswith("._multiarray_umath") or mod.endswith("._multiarray_tests"):
            continue
        if mod in names:
            rep.ok(mod)
        else:
            rep.fail(f"{mod} is imported by the C extension but is NOT in the PYZ")


def main() -> int:
    rep = Report()
    pass_source(rep)
    pass_frozen(rep)
    names = pyz_modules()
    pass_pyz(rep, names)
    pass_native(rep, names)

    print()
    if rep.failures:
        print(f"RESULT: {rep.failures} failure(s). NOT clear to go to ISCC.")
        return 1
    print("RESULT: entry points import cleanly in source and in the built EXEs, every")
    print("absolute target is in the PYZ, and numpy's C extension has both its native")
    print("dependencies and every Python module it imports by name.")
    print()
    print("This is the static half of the gate. The operator dist-smoke is the other half.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
