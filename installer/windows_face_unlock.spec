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
