"""Static gate over a PyInstaller build: prove the bundle can actually import itself.

Run after PyInstaller and BEFORE Inno Setup (installer/build.py runs it for you as
the gate step, and refuses to start ISCC unless it exits 0):

    .venv\\Scripts\\python.exe tools\\verify_frozen_entrypoints.py
    .venv\\Scripts\\python.exe tools\\verify_frozen_entrypoints.py --dist "C:\\Program Files\\WindowsFaceUnlock"

--dist points the gate at any onedir bundle: the fresh dist\\WindowsFaceUnlock
(default) or an installed copy. Every archive the gate reads is taken out of the
bundle itself -- the PYZ is read from inside each EXE, not from build\\ -- so the
answer describes the artefact that ships, never a neighbouring work directory.

Exit code 0 means the build is clear to go to ISCC. This does NOT replace the
operator dist-smoke -- it is the half of the gate that can be automated, and it
exists because Stage-7 blocks shipped unrunnable builds that every check of the
day called green (7c/7d: relative import in an entry script; 7g: numpy by-name
import from C; 7j/7k: the three "blind bundle" mines of KNOWN_ISSUES #5).

Six passes, each answering a question the others cannot:

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
               archive embedded in the EXEs, so an import that now resolves also
               finds something.

  4 NATIVE  -- numpy's C extension imports Python modules BY NAME from C, which
               modulegraph cannot see and warn-*.txt never mentions. In 7g the
               bundle had a byte-perfect _multiarray_umath.pyd, a complete
               numpy.libs, every PE dependency resolvable -- and no
               numpy._core._exceptions, so the frozen service died with
               "Importing the numpy C-extensions failed" ninety seconds in.

  5 MINES   -- explicit asserts for the three KNOWN_ISSUES #5 mines (7k), each of
               which made FaceAnalysis.get() fail on every frame while the service
               looked healthy ("no face", d=1.000):
                 1 scipy._cyutility -- imported by name from inside 51 scipy .pyd;
                 2 scipy._external  -- array_api_compat imports its submodules by
                   COMPUTED name (__import__(__package__ + ".fft"));
                 3 _internal\\objects\\meanshape_68.pkl -- a DATA file that
                   insightface's get_object() looks up under sys._MEIPASS, not
                   in the package layout collect_data_files reproduces.

  6 CLASS   -- the class guard (8a D-09, prototype data in the 8a classguard scan).
               The mines were instances; this pass catches the class:
                 a native: EVERY bundled extension module (.pyd) is read as bytes,
                   the dotted names it embeds are resolved against the build
                   interpreter's site-packages, and any real Python module a .pyd
                   names that is NOT in the bundle fails the gate. This is pass 4
                   generalised to all ~250 extensions -- mine 1 was exactly this.
                 b vendor code: every third-party module in the bundle (PYZ and
                   on-disk .py) is scanned for sys.frozen / sys._MEIPASS branches
                   (mine 3's class) and for __import__ / import_module with a
                   COMPUTED module name (mine 2's class). Each hit must be listed
                   in REVIEWED below with the reason it is safe; a new one fails
                   the gate until somebody reviews it. A dependency bump that
                   adds such a branch therefore stops the build instead of
                   shipping a blind bundle.

The lesson passes 4-6 encode: verifying a bundle by reading its PYZ proves what
was collected, never that the result runs. Native extensions and frozen-aware
vendor code fail on their own terms.

--passes selects a subset (comma-separated: source,frozen,pyz,native,mines,class);
the default is all six. A subset run is a diagnostic, never a gate result.
"""
from __future__ import annotations

import argparse
import ast
import dis
import hashlib
import importlib.util
import marshal
import os
import re
import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist" / "WindowsFaceUnlock"
INTERNAL = DIST / "_internal"

# The spec's three Analysis targets, paired with the EXE each one produces.
ENTRIES = (
    ("face_service/__main__.py", "face_service.exe"),
    ("presence_monitor/__main__.py", "face_unlock_tray.exe"),
    ("tools/watchdog.py", "face_unlock_watchdog.exe"),
)

# The EXE that runs recognition: mines 1-3 all fired inside FaceAnalysis.get().
SERVICE_EXE = "face_service.exe"

# Absolute targets the entry points import by name.
REQUIRED_MODULES = (
    "face_service.service",
    "presence_monitor.monitor",
    "presence_monitor.enroll_gui",
    "presence_monitor.password_gui",
    "tools.pipe_client",
)

MODULE_NAME_RE = re.compile(rb"numpy(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

# Any dotted identifier embedded in a binary. Filtered afterwards by root and by
# the filesystem, so the regex itself may be generous.
DOTTED_RE = re.compile(rb"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

ALL_PASSES = ("source", "frozen", "pyz", "native", "mines", "class")

# Roots whose code is ours (reviewed in-repo) or PyInstaller's own runtime, which
# is frozen-aware by definition. Everything else in the bundle is vendor code.
PROJECT_ROOTS = frozenset({"face_service", "presence_monitor", "tools"})
PYINSTALLER_ROOT_PREFIXES = ("pyimod", "pyi_", "_pyi_", "PyInstaller")

# --------------------------------------------------------------------------- mine data
MINE1_MODULE = "scipy._cyutility"
MINE2_MODULES = (
    "scipy._external",
    "scipy._external.array_api_compat",
    "scipy._external.array_api_compat.numpy",
    "scipy._external.array_api_compat.numpy.fft",      # the exact 7j crash site
    "scipy._external.array_api_compat.numpy.linalg",
)
# array_api_compat backends for array libraries that are not installed in the build
# interpreter. collect_submodules() skips their .fft/.linalg (importing them needs the
# library), and at runtime they are reached only for cupy/dask/torch arrays, which
# cannot exist here. Exempted only while the library is absent from the interpreter.
MINE2_OPTIONAL_BACKENDS = ("cupy", "dask", "torch")
MINE3_READER = "insightface.data.pickle_object"
MINE3_FILE = "meanshape_68.pkl"

# --------------------------------------------------------------------------- class-guard review table
# Every vendor frozen/_MEIPASS branch or computed import found in the bundle must be
# listed here, with the reason it cannot blind the frozen build. Keys:
#   exact module name              -> applies to that module only
#   "pkg.*"                        -> pkg and every submodule; allowed ONLY for the
#                                     computed-import kinds, never for frozen/_MEIPASS:
#                                     a frozen branch is a data-path decision and is
#                                     reviewed one module at a time (that is mine 3).
# Kinds: "frozen", "_MEIPASS", "__import__", "import_module".
# Source of the reasons: 8a work\classguard.md (bundled hits, cross-checked against
# the spec and against the runtime path).
FROZEN_KINDS = frozenset({"frozen", "_MEIPASS"})
IMPORT_KINDS = frozenset({"__import__", "import_module"})

REVIEWED: dict[str, tuple[frozenset, str]] = {
    # ---- frozen / _MEIPASS branches (exact modules only)
    "insightface.data.pickle_object": (
        FROZEN_KINDS,
        "mine 3: resolves objects/ under sys._MEIPASS; spec ships the bundle-root "
        "objects/ copy and pass 5 asserts it"),
    "PIL.ImageShow": (
        FROZEN_KINDS, "macOS Preview.app viewer branch; never reached on Windows"),
    "pywintypes": (
        frozenset({"frozen"}),
        "frozen branch searches sys.path for pywintypes312.dll; bundled in "
        "pywin32_system32 and put on the path by the pyi_rth_pywintypes rthook"),
    "imageio.core.util": (
        frozenset({"frozen"}),
        "appdata/resources next to sys.executable; imageio (via skimage.io) is not "
        "on the runtime path"),
    # ---- computed imports on or near the runtime path (exact modules)
    "insightface.utils.filesystem": (
        IMPORT_KINDS, "try_import/import_try_install: no caller in insightface or here"),
    "cv2": (
        IMPORT_KINDS,
        "cv2/__init__ loads its on-disk submodule dirs (shipped as DATA); cv2.gapi "
        "absent and its ImportError is caught"),
    "numpy.core": (IMPORT_KINDS, "legacy shim forwarding to numpy._core (collected whole)"),
    "numpy.lib._utils_impl": (IMPORT_KINDS, "np.info/lookfor helper; not on the runtime path"),
    "numpy._core.function_base": (
        IMPORT_KINDS, "add_newdoc with literal numpy._core.* places (collected whole)"),
    "scipy": (
        IMPORT_KINDS,
        "lazy scipy.<sub> attribute access; runtime users import subpackages statically"),
    "scipy.sparse": (IMPORT_KINDS, "lazy scipy.sparse.<name>; not on the skimage.transform path"),
    "scipy.ndimage._support_alternative_backends": (IMPORT_KINDS, "cupyx branch; cupy absent"),
    "scipy.signal._support_alternative_backends": (IMPORT_KINDS, "cupyx branch; cupy absent"),
    "scipy.optimize._optimize": (IMPORT_KINDS, "show_options() docs helper"),
    "scipy._external.array_api_compat._internal": (
        IMPORT_KINDS, "mine 2 tree: collected whole by the spec, asserted by pass 5"),
    "scipy._external.array_api_compat.numpy": (
        IMPORT_KINDS, "mine 2 crash site: .fft/.linalg asserted by pass 5"),
    "scipy._external.array_api_compat.cupy": (IMPORT_KINDS, "mine 2 tree; cupy absent"),
    "scipy._external.array_api_compat.dask.array": (IMPORT_KINDS, "mine 2 tree; dask absent"),
    "scipy._external.array_api_compat.torch": (IMPORT_KINDS, "mine 2 tree; torch absent"),
    "scipy._lib.deprecation": (IMPORT_KINDS, "fires only on deprecated scipy names"),
    "scipy._lib._uarray._backend": (IMPORT_KINDS, "unpickling uarray backends; no pickling here"),
    "skimage.io.manage_plugins": (IMPORT_KINDS, "skimage.io plugins; the project never calls skimage.io"),
    "skimage._shared.version_requirements": (IMPORT_KINDS, "optional-dependency probe; ImportError -> False"),
    "skimage._vendored.numpy_lookfor": (IMPORT_KINDS, "lookfor() docs helper"),
    "lazy_loader": (
        IMPORT_KINDS,
        "skimage lazy attach; skimage.transform (the only runtime user) is collected "
        "whole by the spec"),
    "PIL.features": (IMPORT_KINDS, "feature probes; ImportError handled"),
    "PIL.Image": (IMPORT_KINDS, "*ImagePlugin modules collected by PyInstaller's hook-PIL.Image"),
    "requests.compat": (IMPORT_KINDS, "literal candidates chardet/charset_normalizer; handled"),
    "requests.packages": (IMPORT_KINDS, "aliases urllib3/idna/charset_normalizer, all bundled"),
    "charset_normalizer.utils": (IMPORT_KINDS, "encodings.* from base_library.zip (stdlib, complete)"),
    "six": (IMPORT_KINDS, "six.moves lazy aliases; dateutil (pandas) only"),
    "dateutil": (IMPORT_KINDS, "pandas dependency; not on the runtime path"),
    # ---- stacks swept in by collect_submodules("onnxruntime") / insightface's dead pip
    #      fallback (8a F17). Not on the runtime path; computed imports only.
    "pip.*": (IMPORT_KINDS, "bundled only via insightface/utils/filesystem.py dead code"),
    "setuptools.*": (IMPORT_KINDS, "setuptools/pkg_resources transitive; not on the runtime path"),
    "pkg_resources.*": (IMPORT_KINDS, "imported at start only by the pyi_rth_pkgres rthook"),
    "sympy.*": (IMPORT_KINDS, "via onnxruntime.* (spec collect_submodules); unused at runtime"),
    "mpmath.*": (IMPORT_KINDS, "via sympy; unused at runtime"),
    "pandas.*": (IMPORT_KINDS, "via onnxruntime.* and tqdm; unused at runtime"),
    "fsspec.*": (IMPORT_KINDS, "via pandas; unused at runtime"),
    "jinja2.*": (IMPORT_KINDS, "via the pandas chain; unused at runtime"),
    "imageio.*": (IMPORT_KINDS, "via skimage.io; unused at runtime"),
    "google.protobuf.*": (IMPORT_KINDS, "protobuf backend selection via onnx; unused at runtime"),
}

# Names a bundled .pyd embeds that are real modules in site-packages but are
# deliberately absent from the bundle. Each needs the reason it is never imported.
NATIVE_REVIEWED: dict[str, str] = {
    "numpy.core._multiarray_umath":
        "fallback name in numpy's C-API import; numpy._core._multiarray_umath is tried "
        "first and is bundled",
}


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

    def info(self, msg: str) -> None:
        print(f"  ..    {msg}")


def header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- site-packages resolver
def site_dirs() -> list[Path]:
    """Where the BUILD interpreter's third-party sources live (incl. pywin32's win32\\lib)."""
    out = []
    for p in sys.path:
        if p and "site-packages" in p.lower() and Path(p).is_dir():
            out.append(Path(p))
    return out


def _exact_child(parent: Path, name: str) -> Path | None:
    """Case-EXACT lookup. NTFS is case-insensitive, so plain is_file() turns the class
    name scipy.spatial.cKDTree into the module scipy/spatial/ckdtree.py (8a false positive)."""
    try:
        return parent / name if name in os.listdir(parent) else None
    except OSError:
        return None


def resolve_source(module: str, dirs: list[Path]) -> Path | None:
    """The .py a dotted name maps to in site-packages, or None (case-exact)."""
    parts = module.split(".")
    for base in dirs:
        cur = base
        ok = True
        for part in parts[:-1]:
            nxt = _exact_child(cur, part)
            if nxt is None or not nxt.is_dir():
                ok = False
                break
            cur = nxt
        if not ok:
            continue
        leaf = parts[-1]
        f = _exact_child(cur, leaf + ".py")
        if f is not None and f.is_file():
            return f
        d = _exact_child(cur, leaf)
        if d is not None and d.is_dir():
            init = _exact_child(d, "__init__.py")
            if init is not None:
                return init
    return None


def resolve_package_dir(module: str, dirs: list[Path]) -> Path | None:
    """The directory of a (possibly namespace) package in site-packages, or None (case-exact)."""
    for base in dirs:
        cur: Path | None = base
        for part in module.split("."):
            cur = _exact_child(cur, part)
            if cur is None or not cur.is_dir():
                cur = None
                break
        if cur is not None:
            return cur
    return None


def site_roots(dirs: list[Path]) -> set[str]:
    """Top-level importable names in site-packages (packages, modules, extensions)."""
    roots: set[str] = set()
    for base in dirs:
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for e in entries:
            if e.endswith(".py"):
                name = e[:-3]
            elif e.endswith(".pyd"):
                name = e.split(".")[0]
            elif (base / e).is_dir():
                name = e
            else:
                continue
            if name.isidentifier():
                roots.add(name)
    return roots


def is_real_vendor_module(module: str, dirs: list[Path]) -> bool:
    """True if the dotted name is an importable module/package/extension in site-packages."""
    if resolve_source(module, dirs) is not None:
        return True
    parts = module.split(".")
    for base in dirs:
        cur = base
        ok = True
        for part in parts[:-1]:
            nxt = _exact_child(cur, part)
            if nxt is None or not nxt.is_dir():
                ok = False
                break
            cur = nxt
        if not ok:
            continue
        try:
            entries = os.listdir(cur)
        except OSError:
            continue
        if any(e.startswith(parts[-1] + ".") and e.endswith(".pyd") for e in entries):
            return True
    return False


# --------------------------------------------------------------------------- bundle model
class Bundle:
    """What the bundle itself says it contains. Built once, read by every pass."""

    def __init__(self, dist: Path) -> None:
        self.dist = dist
        self.internal = dist / "_internal"
        self.pyz_by_exe: dict[str, set[str]] = {}
        self.pyz_pkgs: set[str] = set()
        self.pyz_errors: dict[str, str] = {}
        for _rel, exe_name in ENTRIES:
            exe = dist / exe_name
            if not exe.is_file():
                continue
            try:
                reader = CArchiveReader(str(exe))
                names: set[str] = set()
                for name, meta in reader.toc.items():
                    if meta[-1] == "z":
                        pyz = reader.open_embedded_archive(name)
                        for mod, (typecode, *_rest) in pyz.toc.items():
                            names.add(mod)
                            if typecode in (1, 3):   # PYZ_ITEM_PKG / PYZ_ITEM_NSPKG
                                self.pyz_pkgs.add(mod)
                self.pyz_by_exe[exe_name] = names
            except Exception as exc:  # noqa: BLE001 -- reported by the passes
                self.pyz_errors[exe_name] = str(exc)
        self.pyz_all: set[str] = set().union(*self.pyz_by_exe.values()) if self.pyz_by_exe else set()

        # Extension modules and on-disk (DATA) Python sources.
        self.pyds: list[Path] = sorted(self.internal.rglob("*.pyd")) if self.internal.is_dir() else []
        self.ext_modules: set[str] = set()
        for p in self.pyds:
            rel = p.relative_to(self.internal).with_suffix("")
            parts = list(rel.parts)
            parts[-1] = parts[-1].split(".")[0]            # _cyutility.cp312-win_amd64 -> _cyutility
            dotted = ".".join(parts)
            self.ext_modules.add(dotted)
            # pywin32 keeps its extensions in win32\ (a sys.path entry, not a package):
            # the importable name is the leaf.
            if len(parts) > 1 and ".".join(parts[:-1]) not in self.pyz_pkgs:
                self.ext_modules.add(parts[-1])
        self.data_py: dict[str, Path] = {}
        if self.internal.is_dir():
            for p in sorted(self.internal.rglob("*.py")):
                rel = p.relative_to(self.internal).with_suffix("")
                parts = list(rel.parts)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if parts:
                    self.data_py[".".join(parts)] = p
        self.known = self.pyz_all | self.ext_modules | set(self.data_py)
        self.roots = {n.split(".")[0] for n in self.known}

    def vendor_root(self, root: str) -> bool:
        if root in PROJECT_ROOTS or root in sys.stdlib_module_names:
            return False
        if root.startswith(PYINSTALLER_ROOT_PREFIXES):
            return False
        return True


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


def pass_frozen(rep: Report, bundle: Bundle) -> None:
    header("PASS 2 -- FROZEN (bytecode of the entry script inside each built EXE)")
    for rel, exe_name in ENTRIES:
        exe = bundle.dist / exe_name
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
def pass_pyz(rep: Report, bundle: Bundle) -> None:
    header("PASS 3 -- PYZ (the absolute targets are present in the archive inside each EXE)")
    for _rel, exe_name in ENTRIES:
        if exe_name in bundle.pyz_errors:
            rep.fail(f"{exe_name}: cannot read the embedded PYZ ({bundle.pyz_errors[exe_name]})")
        elif exe_name in bundle.pyz_by_exe and not bundle.pyz_by_exe[exe_name]:
            rep.fail(f"{exe_name}: no embedded PYZ -> nothing for the entry point to import")
        elif exe_name in bundle.pyz_by_exe:
            print(f"  {exe_name}: {len(bundle.pyz_by_exe[exe_name])} module names")
        else:
            rep.fail(f"{exe_name}: not built, no PYZ to read")
    names = bundle.pyz_all
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


def pass_native(rep: Report, bundle: Bundle) -> None:
    header("PASS 4 -- NATIVE (numpy's C extension and what it imports by name)")
    names = bundle.pyz_all
    numpy_dir = numpy_site_dir()
    if numpy_dir is None:
        rep.fail("numpy is not importable in this interpreter; cannot verify the bundle")
        return

    tag = f"cp{sys.version_info.major}{sys.version_info.minor}-win_amd64"
    ext_rel = Path("numpy") / "_core" / f"_multiarray_umath.{tag}.pyd"
    bundled = bundle.internal / ext_rel
    installed = numpy_dir / "_core" / f"_multiarray_umath.{tag}.pyd"

    if not bundled.is_file():
        rep.fail(f"{ext_rel.as_posix()} absent from the bundle")
        return
    rep.ok(f"{ext_rel.as_posix()} present ({bundled.stat().st_size:,} bytes)")

    if installed.is_file():
        h_b = sha256_file(bundled)
        h_i = sha256_file(installed)
        if h_b == h_i:
            rep.ok(f"identical to the installed numpy ({h_b[:16]}...)")
        else:
            rep.fail("the bundled extension differs from the installed one")

    libs = bundle.internal / "numpy.libs"
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


# --------------------------------------------------------------------------- 5
def pass_mines(rep: Report, bundle: Bundle, dirs: list[Path]) -> None:
    header("PASS 5 -- MINES (KNOWN_ISSUES #5, the three defects behind the blind frozen service)")

    # Mine 1 -- scipy._cyutility, imported by name from inside scipy's Cython .pyd.
    print("  mine 1: scipy._cyutility (by-name import from inside Cython extensions)")
    scipy_dir = bundle.internal / "scipy"
    scipy_pyds = [p for p in bundle.pyds if scipy_dir in p.parents]
    cyutil = [p for p in scipy_pyds if p.parent == scipy_dir and p.name.startswith("_cyutility.")]
    if not scipy_pyds:
        rep.fail("no scipy extension in the bundle -- skimage.transform (insightface "
                 "face_align) cannot import; the recognizer would be dead")
    else:
        users = sum(1 for p in scipy_pyds if b"scipy._cyutility" in p.read_bytes())
        if cyutil:
            rep.ok(f"_internal\\scipy\\{cyutil[0].name} present "
                   f"({users} of {len(scipy_pyds)} scipy extensions import it by name)")
        else:
            rep.fail(f"_internal\\scipy\\_cyutility.*.pyd ABSENT while {users} of "
                     f"{len(scipy_pyds)} scipy extensions import it by name -> scipy raises "
                     "'seems to be broken' at the first skimage.transform touch -> every "
                     "frame reads as 'no face'. Fix: spec HIDDEN += ['scipy._cyutility'].")

    # Mine 2 -- scipy._external.array_api_compat, submodules imported by COMPUTED name.
    print("  mine 2: scipy._external (array_api_compat, __import__(__package__ + '.fft'))")
    checked_any = False
    for _rel, exe_name in ENTRIES:
        names = bundle.pyz_by_exe.get(exe_name)
        if names is None:
            continue
        must = exe_name == SERVICE_EXE or "scipy" in names
        if not must:
            continue
        checked_any = True
        missing = [m for m in MINE2_MODULES if m not in names]
        if missing:
            rep.fail(f"{exe_name}: PYZ lacks {', '.join(missing)} -> 'No module named "
                     f"{missing[-1]}' at the first FaceAnalysis.get(). Fix: spec "
                     "HIDDEN += collect_submodules('scipy._external').")
        else:
            rep.ok(f"{exe_name}: {len(MINE2_MODULES)} scipy._external anchors in the PYZ")
        # The whole vendored tree (a namespace package), against the build interpreter's copy.
        tree = resolve_package_dir("scipy._external", dirs)
        if tree is None:
            rep.fail("scipy._external not found in the build interpreter's site-packages -- "
                     "cannot compare the bundled tree against it")
        else:
            expected = set()
            for p in tree.rglob("*.py"):
                rel = p.relative_to(tree.parent.parent).with_suffix("")   # scipy/_external/...
                parts = list(rel.parts)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                expected.add(".".join(parts))
            absent = [b for b in MINE2_OPTIONAL_BACKENDS if importlib.util.find_spec(b) is None]
            exempt = tuple(f"scipy._external.array_api_compat.{b}." for b in absent)
            gap = sorted(m for m in expected if m not in names and not m.startswith(exempt))
            skipped = sorted(m for m in expected if m not in names and m.startswith(exempt))
            if gap:
                rep.fail(f"{exe_name}: {len(gap)} of {len(expected)} scipy._external modules "
                         f"missing from the PYZ (first: {', '.join(gap[:3])})")
            else:
                rep.ok(f"{exe_name}: {len(expected) - len(skipped)} of {len(expected)} "
                       "scipy._external modules of the build interpreter are in the PYZ; "
                       f"{len(skipped)} exempt (backends not installed: {', '.join(absent)})")
    if not checked_any:
        rep.fail(f"{SERVICE_EXE}: no PYZ to check mine 2 against")

    # Mine 3 -- objects/ at the bundle root, where get_object() looks under sys._MEIPASS.
    print("  mine 3: _internal\\objects\\meanshape_68.pkl (DATA looked up under sys._MEIPASS)")
    root_objects = bundle.internal / "objects"
    target = root_objects / MINE3_FILE
    if target.is_file() and target.stat().st_size > 0:
        rep.ok(f"_internal\\objects\\{MINE3_FILE} present ({target.stat().st_size:,} bytes)")
    else:
        rep.fail(f"_internal\\objects\\{MINE3_FILE} ABSENT -> get_object() returns None under "
                 "sys.frozen -> Landmark.mean_lmk is None for 1k3d68 -> every "
                 "FaceAnalysis.get() dies on 'NoneType' has no attribute 'shape' and reads as "
                 "'no face'. Fix: the spec's bundle-root 'objects' DATAS entry.")
    # Every object insightface ships must be at the root copy, byte-identical.
    pkg_objects = bundle.internal / "insightface" / "data" / "objects"
    src_objects = None
    src_init = resolve_source("insightface.data", dirs)
    if src_init is not None:
        src_objects = src_init.parent / "objects"
    for label, ref_dir in (("bundle package copy", pkg_objects), ("build interpreter", src_objects)):
        if ref_dir is None or not ref_dir.is_dir():
            rep.info(f"{label}: no insightface/data/objects to compare against")
            continue
        for ref in sorted(p for p in ref_dir.iterdir() if p.is_file()):
            got = root_objects / ref.name
            if not got.is_file():
                rep.fail(f"_internal\\objects\\{ref.name} missing ({label} has it)")
            elif sha256_file(got) != sha256_file(ref):
                rep.fail(f"_internal\\objects\\{ref.name} differs from the {label}")
            else:
                rep.ok(f"_internal\\objects\\{ref.name} == {label}")
    if MINE3_READER in bundle.pyz_by_exe.get(SERVICE_EXE, set()):
        rep.ok(f"{SERVICE_EXE}: {MINE3_READER} (the reader) is in the PYZ")
    else:
        rep.fail(f"{SERVICE_EXE}: {MINE3_READER} absent from the PYZ")


# --------------------------------------------------------------------------- 6
FROZEN_LINE_RE = re.compile(
    r"getattr\(\s*sys\s*,\s*['\"]frozen['\"]|hasattr\(\s*sys\s*,\s*['\"]frozen['\"]|\bsys\.frozen\b")
MEIPASS_RE = re.compile(r"_MEIPASS")
PREFILTER_RE = re.compile(r"frozen|_MEIPASS|__import__|import_module")


def scan_vendor_source(text: str, filename: str) -> list[tuple[int, str]]:
    """(line, kind) for frozen/_MEIPASS references and computed-name imports."""
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if FROZEN_LINE_RE.search(line):
            hits.append((i, "frozen"))
        if MEIPASS_RE.search(line):
            hits.append((i, "_MEIPASS"))
    try:
        tree = ast.parse(text, filename=filename)
    except (SyntaxError, ValueError):
        hits.append((0, "unparseable"))
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name not in ("__import__", "import_module"):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            continue
        hits.append((node.lineno, name))
    return hits


def reviewed_kinds(module: str) -> tuple[frozenset, list[str]]:
    kinds: set[str] = set()
    keys: list[str] = []
    for key, (ks, _reason) in REVIEWED.items():
        if key.endswith(".*"):
            base = key[:-2]
            if module == base or module.startswith(base + "."):
                kinds |= ks
                keys.append(key)
        elif key == module:
            kinds |= ks
            keys.append(key)
    return frozenset(kinds), keys


def pass_class(rep: Report, bundle: Bundle, dirs: list[Path]) -> None:
    header("PASS 6 -- CLASS GUARD (every extension's by-name imports; vendor frozen/computed imports)")
    for key, (ks, _r) in REVIEWED.items():
        if key.endswith(".*") and ks & FROZEN_KINDS:
            rep.fail(f"REVIEWED[{key!r}] is a wildcard carrying a frozen kind -- review frozen "
                     "branches one module at a time")
    if not dirs:
        rep.fail("no site-packages on sys.path -- run the gate with the BUILD interpreter "
                 "(.venv\\Scripts\\python.exe), otherwise nothing can be resolved")
        return

    # ---- 6a: native by-name imports, all extensions.
    print(f"  6a native: {len(bundle.pyds)} extension module(s) (.pyd) in the bundle")
    # Roots of the bundle AND of site-packages: a .pyd that names a package which did not
    # ship at all is the same defect as one that names a missing submodule.
    vendor_roots = {r for r in bundle.roots | site_roots(dirs) if bundle.vendor_root(r)}
    referenced: dict[str, set[str]] = {}
    for pyd in bundle.pyds:
        try:
            blob = pyd.read_bytes()
        except OSError as exc:
            rep.fail(f"cannot read {pyd}: {exc}")
            continue
        for raw in set(DOTTED_RE.findall(blob)):
            try:
                name = raw.decode("ascii")
            except UnicodeDecodeError:
                continue
            if name.split(".")[0] not in vendor_roots:
                continue
            referenced.setdefault(name, set()).add(pyd.relative_to(bundle.internal).as_posix())
    real = {n: users for n, users in referenced.items() if is_real_vendor_module(n, dirs)}
    print(f"  {len(referenced)} dotted names under bundled vendor roots, "
          f"{len(real)} are real modules in site-packages")
    uncovered = 0
    for name in sorted(real):
        if name in bundle.known:
            continue
        users = sorted(real[name])
        if name in NATIVE_REVIEWED:
            rep.info(f"{name}: reviewed ({NATIVE_REVIEWED[name]})")
            continue
        uncovered += 1
        rep.fail(f"{name} is named by {len(users)} extension(s) but is NOT in the bundle "
                 f"-> ImportError from inside native code at first use (first: {users[0]})")
    if not uncovered:
        rep.ok(f"every real module named by a bundled extension is in the bundle "
               f"({len(real)} checked)")

    # ---- 6b: vendor frozen / computed-import tripwire.
    modules: dict[str, Path | None] = {}
    for name in sorted(bundle.pyz_all):
        if bundle.vendor_root(name.split(".")[0]):
            modules[name] = None
    for name, path in bundle.data_py.items():
        if bundle.vendor_root(name.split(".")[0]):
            modules[name] = path            # scan the copy that actually ships
    scanned = 0
    unresolved: list[str] = []
    hits_by_module: dict[str, list[tuple[int, str]]] = {}
    origin: dict[str, Path] = {}
    for name, path in modules.items():
        src = path if path is not None else resolve_source(name, dirs)
        if src is None:
            unresolved.append(name)     # namespace packages have no source to scan
            continue
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unresolved.append(name)
            continue
        scanned += 1
        if not PREFILTER_RE.search(text):
            continue
        hits = scan_vendor_source(text, str(src))
        if hits:
            hits_by_module[name] = hits
            origin[name] = src
    print(f"  6b vendor code: {len(modules)} vendor module(s) in the bundle, {scanned} scanned, "
          f"{len(unresolved)} without a source file (namespace packages): "
          f"{', '.join(unresolved[:8])}{' ...' if len(unresolved) > 8 else ''}")
    if scanned == 0:
        rep.fail("no vendor module could be matched to its source -- wrong interpreter?")
        return
    unreviewed = 0
    reviewed_hits = 0
    used_keys: set[str] = set()
    for name in sorted(hits_by_module):
        allowed, keys = reviewed_kinds(name)
        for line, kind in hits_by_module[name]:
            if kind in allowed:
                reviewed_hits += 1
                used_keys.update(keys)
                continue
            unreviewed += 1
            where = f"{origin[name]}:{line}"
            if kind in FROZEN_KINDS:
                rep.fail(f"{name}: unreviewed {kind} branch ({where}) -> vendor code that "
                         "resolves paths differently when frozen can miss its data exactly "
                         "like mine 3. Review it, cover it in the spec, then add it to REVIEWED.")
            elif kind == "unparseable":
                rep.fail(f"{name}: source does not parse ({origin[name]}) -- cannot be reviewed")
            else:
                rep.fail(f"{name}: unreviewed {kind}(<computed name>) ({where}) -> modulegraph "
                         "cannot follow it, so its targets may be missing like mine 2. Review "
                         "it, collect the targets in the spec, then add it to REVIEWED.")
    if not unreviewed:
        rep.ok(f"every vendor frozen/_MEIPASS branch and computed import is reviewed "
               f"({reviewed_hits} hit(s) in {len(hits_by_module)} module(s))")
    stale = sorted(k for k in REVIEWED if k not in used_keys)
    if stale:
        rep.info(f"{len(stale)} REVIEWED entr{'y' if len(stale) == 1 else 'ies'} matched nothing in "
                 f"this bundle (not an error): {', '.join(stale)}")


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--dist", type=Path, default=None,
                    help="onedir bundle root (default: dist\\WindowsFaceUnlock)")
    ap.add_argument("--passes", default=",".join(ALL_PASSES),
                    help="comma-separated subset of: " + ",".join(ALL_PASSES))
    args = ap.parse_args(argv)

    dist = (args.dist or DIST).resolve()
    passes = [p.strip() for p in args.passes.split(",") if p.strip()]
    bad = [p for p in passes if p not in ALL_PASSES]
    if bad:
        ap.error(f"unknown pass(es): {', '.join(bad)}")

    print(f"bundle: {dist}")
    print(f"interpreter: {sys.executable}")
    if not dist.is_dir():
        print(f"\nRESULT: no bundle at {dist}. NOT clear to go to ISCC.")
        return 1

    rep = Report()
    bundle = Bundle(dist)
    dirs = site_dirs()
    if "source" in passes:
        pass_source(rep)
    if "frozen" in passes:
        pass_frozen(rep, bundle)
    if "pyz" in passes:
        pass_pyz(rep, bundle)
    if "native" in passes:
        pass_native(rep, bundle)
    if "mines" in passes:
        pass_mines(rep, bundle, dirs)
    if "class" in passes:
        pass_class(rep, bundle, dirs)

    print()
    subset = set(passes) != set(ALL_PASSES)
    if rep.failures:
        print(f"RESULT: {rep.failures} failure(s). NOT clear to go to ISCC.")
        return 1
    if subset:
        print(f"RESULT: passes {', '.join(passes)} clean. SUBSET run -- not a gate result.")
        return 0
    print("RESULT: entry points import cleanly in source and in the built EXEs, every")
    print("absolute target is in the PYZ, numpy's C extension has its native dependencies")
    print("and every module it imports by name, mines 1-3 of KNOWN_ISSUES #5 are covered,")
    print("every extension's by-name imports resolve inside the bundle, and every vendor")
    print("frozen branch / computed import is reviewed.")
    print()
    print("This is the static half of the gate. The operator dist-smoke is the other half.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
