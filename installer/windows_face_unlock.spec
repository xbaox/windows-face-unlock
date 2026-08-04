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

# insightface.utils.face_align does `from skimage import transform as trans` at
# import time, and scikit-image >= 0.20 routes its subpackages through
# lazy_loader, which defeats PyInstaller's static analysis.
HIDDEN += collect_submodules("skimage.transform")

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
# Two entry points.
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

# Share Python DLLs + site-packages between the two EXEs to avoid doubling
# the bundle size.
MERGE(
    (service_analysis, "face_service", "face_service"),
    (tray_analysis, "face_unlock_tray", "face_unlock_tray"),
)

service_pyz = PYZ(service_analysis.pure, service_analysis.zipped_data, cipher=BLOCK_CIPHER)
tray_pyz = PYZ(tray_analysis.pure, tray_analysis.zipped_data, cipher=BLOCK_CIPHER)

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

coll = COLLECT(
    service_exe,
    service_analysis.binaries,
    service_analysis.zipfiles,
    service_analysis.datas,
    tray_exe,
    tray_analysis.binaries,
    tray_analysis.zipfiles,
    tray_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WindowsFaceUnlock",
)
