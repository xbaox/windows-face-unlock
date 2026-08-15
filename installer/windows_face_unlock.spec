# PyInstaller spec — builds face_service.exe and face_unlock_tray.exe into a
# single shared dist folder. Run from the repo root:
#     pyinstaller installer/windows_face_unlock.spec --noconfirm --clean
#
# The two entry points share runtime binaries via MERGE so the ONNX runtime and
# the CUDA libraries are only laid down once.

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)
from PyInstaller.building.api import PYZ, EXE, COLLECT, MERGE
from PyInstaller.building.build_main import Analysis

REPO_ROOT = Path(SPECPATH).resolve().parent
BLOCK_CIPHER = None

# ---------------------------------------------------------------------------
# Hidden imports + data collection.
#
# The engine is InsightFace buffalo_l on onnxruntime-gpu. The collectors that
# used to sit here targeted the Stage-1 ML stack, which was removed in Stage 2
# and is not installed in any current environment.
# ---------------------------------------------------------------------------
HIDDEN = []

# insightface resolves model classes through model_zoo at runtime, so static
# analysis alone misses them. Two subpackages are deliberately left out:
#   .gui        -- a PySide desktop app we never launch
#   .thirdparty -- face3d mesh rendering, the only thing that wants matplotlib
HIDDEN += collect_submodules(
    "insightface",
    filter=lambda name: ".gui" not in name and ".thirdparty" not in name,
)
HIDDEN += collect_submodules("onnxruntime")

# numpy's C extension imports Python modules BY NAME, and modulegraph cannot read
# inside a .pyd, so those imports are invisible to analysis -- they do not even
# reach warn-*.txt. PyInstaller's own hook-numpy.py names the class exactly
# ("Submodules PyInstaller cannot detect (probably because they are only imported
# by extension modules, which PyInstaller cannot read)") and then lists two:
# numpy._core._dtype_ctypes and numpy._core._multiarray_tests.
#
# That list is short by one for numpy 2.4.4. _multiarray_umath imports
# numpy._core._exceptions during initialisation, no .py file in numpy imports it
# (every hit for the name in the package is a test function), and PyInstaller
# 6.11.1's hook predates this numpy. The module was therefore absent from all
# three PYZ archives while the .pyd sat on disk intact, byte-identical to the
# venv copy, with every PE dependency resolvable -- so numpy's own guard fired
# "Importing the numpy C-extensions failed" about ninety seconds into the first
# frozen service run, which is when recognition first touches numpy.
#
# Collecting the whole of numpy._core rather than naming _exceptions closes the
# class instead of the instance: everything the C layer reaches for by name lives
# there, and the next numpy release that adds one will not need a fourth block to
# discover it. Tests are filtered out; they are the only bulky part.
HIDDEN += collect_submodules(
    "numpy._core",
    filter=lambda name: ".tests" not in name,
)

# insightface.utils.face_align does `from skimage import transform as trans` at
# import time, and scikit-image >= 0.20 routes its subpackages through
# lazy_loader, which defeats PyInstaller's static analysis.
HIDDEN += collect_submodules("skimage.transform")

# Same class as the numpy block above: 51 of the 91 bundled scipy extension
# modules import scipy._cyutility BY NAME from inside the .pyd (Cython emits the
# call), no .py in scipy names it, and modulegraph cannot read extension
# internals -- so it is dropped silently and warn-*.txt shows nothing. Without it
# scipy's import guard raises "seems to be broken" at the first touch of
# skimage.transform inside FaceAnalysis.get(), so every probe answers none while
# the detector is healthy. KNOWN_ISSUES #5, 7j; evidence C:\dev\d-series\7j-diag.
HIDDEN += ["scipy._cyutility"]

# Mine 2. scipy vendors array_api_compat under scipy._external and imports its submodules by
# COMPUTED name at first use -- numpy/__init__.py runs __import__(__package__ + ".fft")
# -- modulegraph never sees them, so the frozen build died on the first FaceAnalysis.get()
# with: No module named 'scipy._external.array_api_compat.numpy.fft'. Collect the whole tree;
# it is pure .py, no binaries. KNOWN_ISSUES #5 mine 2, 7j; evidence C:\dev\d-series\7j-fix.
HIDDEN += collect_submodules("scipy._external")

HIDDEN += [
    "onnxruntime.capi._pybind_state",
    "nvidia",                    # see the CUDA block below
    "requests", "tqdm",          # insightface.utils.download (fetches buffalo_l)
    "pystray._win32",
    "PIL._tkinter_finder",
    "win32api", "win32security", "win32file", "win32pipe",
    "win32event", "win32process", "winerror",
    "pywintypes",
    "presence_monitor.enroll_gui",
    "presence_monitor.gui",
    "presence_monitor.updater",
    "presence_monitor.widgets",
    # THE ENTRY-POINT MODULES THEMSELVES. Kept deliberately, and the history is
    # worth writing down because it cost two blocks.
    #
    # PyInstaller analyses an entry point as a top-level SCRIPT with no package
    # context -- PKG-0{0,1}.toc record both as ('__main__', ..., 'PYSOURCE') --
    # so a leading-dot import in one of them is unresolvable. Both __main__.py
    # files used to have exactly that, and it broke the build in TWO independent
    # ways that look alike and are not.
    #
    # The first is a BUILD defect: modulegraph cannot follow the dot, so it drops
    # the target silently, with nothing in warn-*.txt. The first build of this
    # spec shipped a face_service.exe whose bundle held face_service.config and
    # face_service.i18n (they arrive through the hidden imports below) but NOT
    # face_service.service. Naming the modules here fixed that, and these lines
    # are why the whole transitive graph -- recognizer, camera, liveness, lockout,
    # audit -- is in the bundle.
    #
    # The second is a RUNTIME defect, and naming a module here never addressed it:
    # a module present in the PYZ does not make `from .service import main`
    # legal in a script whose __package__ is empty. That import raised ImportError
    # on every start, which is how face_unlock_tray.exe died at 7f acceptance and
    # how face_service.exe would have died had anything launched it directly. The
    # fix belongs in the entry points, and lives there now: both use the absolute
    # form. See the comment in each file before shortening one back to a dot.
    #
    # These entries stay regardless. presence_monitor.monitor is reached only from
    # inside a function body, and the graph should not depend on bytecode scanning
    # finding it.
    "face_service.service",
    "presence_monitor.monitor",
    "face_service.i18n",
    "face_service.detector",
    "face_service._version",
    "face_service.logging_setup",
    "presence_monitor.password_gui",
    # Reached only through presence_monitor/__main__.py --pipe-shutdown, which the installed
    # register_tasks.ps1 calls instead of `python -m tools.pipe_client shutdown` (there is no
    # tools/ tree and no interpreter under {app}). Self-contained: stdlib + pywin32 +
    # face_service.config, all of which are already here.
    "tools.pipe_client",
]

DATAS = []
DATAS += collect_data_files("insightface")
DATAS += collect_data_files("onnxruntime")
DATAS += collect_data_files("skimage")
DATAS += collect_data_files("cv2")

BINARIES = []
BINARIES += collect_dynamic_libs("onnxruntime")   # onnxruntime.dll + CUDA/TensorRT providers
BINARIES += collect_dynamic_libs("cv2")

# Bundle the YuNet detector. It is the only model committed to the repo.
DATAS += [
    (str(REPO_ROOT / "models" / "face_detection_yunet_2023mar.onnx"), "models"),
]

# ---------------------------------------------------------------------------
# The buffalo_l recognition pack (Stage 7d-F).
#
# Until now this was the "known gap": FaceAnalysis was constructed with no root=,
# so a freshly installed machine looked in an empty %USERPROFILE%\.insightface
# and downloaded ~290 MB over plain HTTP at the first unlock attempt -- unpinned,
# unchecksummed, and impossible offline. The spec even shipped requests+tqdm to
# make that download work, which is the packaging equivalent of paving the cow
# path. Ship the models instead.
#
# Layout must satisfy insightface's own resolver, which is <root>/models/<name>:
# face_service.recognizer.model_root() returns <bundle>/insightface_home when
# frozen, so the pack lands in insightface_home/models/buffalo_l. The folder is
# NOT called "insightface" -- that name belongs to the package itself in the
# bundle.
#
# Exactly the five files recognizer.MODEL_FILES lists. The ~275 MB buffalo_l.zip
# that insightface leaves behind after extracting is deliberately NOT shipped:
# it is a download artefact, nothing reads it, and it would nearly double the
# installer for no reason.
_INSIGHTFACE_PACK = Path.home() / ".insightface" / "models" / "buffalo_l"
_PACK_DEST = "insightface_home/models/buffalo_l"
_PACK_FILES = (
    "det_10g.onnx",
    "w600k_r50.onnx",
    "2d106det.onnx",
    "1k3d68.onnx",
    "genderage.onnx",
)
_pack_missing = [n for n in _PACK_FILES if not (_INSIGHTFACE_PACK / n).is_file()]
if _pack_missing:
    raise SystemExit(
        f"buffalo_l is incomplete in {_INSIGHTFACE_PACK}: missing {', '.join(_pack_missing)}.\n"
        "The installer must ship the recognition models -- a build without them produces an "
        "installer whose users cannot sign in offline.\n"
        "Populate the pack first (run the service once online, or copy the five .onnx files "
        "there), then rebuild. installer/build.py checks this before PyInstaller is invoked."
    )
DATAS += [(str(_INSIGHTFACE_PACK / n), _PACK_DEST) for n in _PACK_FILES]

# ---------------------------------------------------------------------------
# CUDA runtime.
#
# face_service.recognizer._prep_cuda_dlls() walks nvidia.__path__, adds every
# <pkg>/bin to the DLL search path and ctypes-preloads the cuDNN DLLs by full
# path. That function is load-bearing (without it the CUDA provider silently
# falls back to CPU at the first Conv), so the bundle has to reproduce the
# layout it expects: `import nvidia` must work and nvidia/<pkg>/bin/*.dll must
# exist relative to the frozen package. Shipping these as datas rather than
# binaries is deliberate -- binaries land flat at the bundle root, which would
# break the directory walk.
#
# If nvidia-* is not installed in the build environment this collects nothing
# and the bundle is CPU-only, which _select_providers() already handles.
# ---------------------------------------------------------------------------
try:
    import nvidia as _nvidia

    for _root in _nvidia.__path__:
        _root = Path(_root)
        for _sub in sorted(p for p in _root.iterdir() if p.is_dir()):
            _bin = _sub / "bin"
            if _bin.is_dir():
                for _dll in sorted(_bin.glob("*.dll")):
                    DATAS.append((str(_dll), f"nvidia/{_sub.name}/bin"))
except ImportError:
    pass

EXCLUDES = [
    # Only insightface.thirdparty.face3d wants matplotlib, and only
    # insightface.gui wants Qt. Both are filtered out of HIDDEN above; excluding
    # them here keeps a stray transitive import from dragging the stacks in.
    "matplotlib",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "jupyter", "ipykernel", "notebook",
]

# ---------------------------------------------------------------------------
# Three entry points.
#
# The watchdog joined in Stage 7d-H. Without a frozen build of it, tasks.psd1
# had to declare InstalledExe = '' for FaceUnlock-Watchdog, and the registrar
# skipped that task -- so every installed machine ran with NO service supervisor
# at all. Its matcher is layout-aware as of 7d-D, so the exe is now the only
# thing that was still missing.
# ---------------------------------------------------------------------------
service_analysis = Analysis(
    [str(REPO_ROOT / "face_service" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

tray_analysis = Analysis(
    [str(REPO_ROOT / "presence_monitor" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN + ["tkinter", "tkinter.ttk", "tkinter.messagebox"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

watchdog_analysis = Analysis(
    [str(REPO_ROOT / "tools" / "watchdog.py")],
    pathex=[str(REPO_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    # tools/watchdog.py imports face_service.config, face_service.watchdog and
    # face_service.logging_setup INSIDE functions, so they are named explicitly
    # rather than trusted to bytecode scanning. It needs neither cv2 nor the
    # models -- it only pings a named pipe and shells out to powershell -- but
    # MERGE puts the shared payload in the first analysis anyway.
    hiddenimports=HIDDEN + [
        "face_service.config",
        "face_service.watchdog",
        "face_service.logging_setup",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    cipher=BLOCK_CIPHER,
    noarchive=False,
)

# Share Python DLLs + site-packages between the EXEs to avoid multiplying
# the bundle size. The first member owns the shared payload.
MERGE(
    (service_analysis, "face_service", "face_service"),
    (tray_analysis, "face_unlock_tray", "face_unlock_tray"),
    (watchdog_analysis, "watchdog", "face_unlock_watchdog"),
)

service_pyz = PYZ(service_analysis.pure, service_analysis.zipped_data, cipher=BLOCK_CIPHER)
tray_pyz = PYZ(tray_analysis.pure, tray_analysis.zipped_data, cipher=BLOCK_CIPHER)
watchdog_pyz = PYZ(watchdog_analysis.pure, watchdog_analysis.zipped_data, cipher=BLOCK_CIPHER)

service_exe = EXE(
    service_pyz,
    service_analysis.scripts,
    [],
    exclude_binaries=True,
    name="face_service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # service has no UI; log to file
    windowed=True,
    disable_windowed_traceback=False,
    icon=None,
)

tray_exe = EXE(
    tray_pyz,
    tray_analysis.scripts,
    [],
    exclude_binaries=True,
    name="face_unlock_tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    windowed=True,
    disable_windowed_traceback=False,
    icon=None,
)

watchdog_exe = EXE(
    watchdog_pyz,
    watchdog_analysis.scripts,
    [],
    exclude_binaries=True,
    name="face_unlock_watchdog",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed for the same reason as the other two: it runs from a Scheduled
    # Task with no interactive console, and it already spawns its powershell and
    # schtasks children with CREATE_NO_WINDOW so nothing flashes on a restart.
    console=False,
    windowed=True,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    service_exe,
    service_analysis.binaries,
    service_analysis.zipfiles,
    service_analysis.datas,
    tray_exe,
    tray_analysis.binaries,
    tray_analysis.zipfiles,
    tray_analysis.datas,
    watchdog_exe,
    watchdog_analysis.binaries,
    watchdog_analysis.zipfiles,
    watchdog_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WindowsFaceUnlock",
)
