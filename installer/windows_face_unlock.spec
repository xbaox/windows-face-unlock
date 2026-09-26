# PyInstaller spec -- builds face_service.exe, face_unlock_tray.exe and face_unlock_watchdog.exe into
# one shared dist folder. installer/build.py runs it; by hand, from the repo root:
#     set FU_VARIANT=cpu   (or gpu)
#     pyinstaller installer/windows_face_unlock.spec --noconfirm --clean
#
# Stage 9 (act 9b R17): two variants. "cpu" (the main one) ships no NVIDIA file and no CUDA
# provider. "gpu" adds ONLY the NVIDIA DLLs the ORT CUDA provider needs (NVIDIA_ALLOWLIST below);
# build.py checks their Authenticode signatures and never modifies them.

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)
from PyInstaller.building.api import PYZ, EXE, COLLECT, MERGE
from PyInstaller.building.build_main import Analysis

import os
import sys

REPO_ROOT = Path(SPECPATH).resolve().parent
VARIANT = os.environ.get("FU_VARIANT", "cpu").strip().lower()
if VARIANT not in ("cpu", "gpu"):
    raise SystemExit(f"FU_VARIANT must be cpu or gpu, got {VARIANT!r}")
print(f"[spec] building the {VARIANT.upper()} variant")
# (Stage 9, D-109: no cipher -- PyInstaller 6 removed bytecode encryption; the argument was dead.)

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
# the detector is healthy. KNOWN_ISSUES #5 (docs/internal), 7j.
HIDDEN += ["scipy._cyutility"]

# Mine 2. scipy vendors array_api_compat under scipy._external and imports its submodules by
# COMPUTED name at first use -- numpy/__init__.py runs __import__(__package__ + ".fft")
# -- modulegraph never sees them, so the frozen build died on the first FaceAnalysis.get()
# with: No module named 'scipy._external.array_api_compat.numpy.fft'. Collect the whole tree;
# it is pure .py, no binaries. KNOWN_ISSUES #5 mine 2 (docs/internal), 7j.
HIDDEN += collect_submodules("scipy._external")

HIDDEN += [
    "onnxruntime.capi._pybind_state",
    "nvidia",                    # see the CUDA block below
    "requests", "tqdm",          # imported by insightface at import time (its download helper)
    "PIL._tkinter_finder",
    "win32api", "win32security", "win32file", "win32pipe",
    "win32event", "win32process", "winerror",
    "pywintypes",
    "presence_monitor.enroll_gui",
    "presence_monitor.gui",
    "presence_monitor.updater",
    "presence_monitor.widgets",
    # Stage 9 (R12 / R14): the tray's UI thread, single-instance guards and toasts -- named for
    # the same reason as the modules above.
    "presence_monitor.ui",
    "presence_monitor.instance",
    "presence_monitor.toast",
    # Stage 9 (R10): imported lazily inside functions -- named so a build never ships without them.
    "face_service.camera_devices",
    "face_service.session_state",
    "psutil",
    # Stage 9 (R17): Setup / the uninstaller call the tray exe with --register / --unregister /
    # --stop / --verify-acl: the Task Scheduler over COM (pywin32) and the ACL check.
    "face_service.taskreg",
    "face_service.ort_privacy",
    "win32com", "win32com.client", "pythoncom", "ntsecuritycon",
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
    # Stage 8b-2. Defect: pywin32 imports win32timezone LAZILY from its native side (pywintypes
    # turns a FILETIME into a datetime through it), so modulegraph never sees the import.
    # Consequence: the 8b bundle lacked it and the frozen service failed the data-directory heal on
    # every start. Fix: named here for all three EXEs (insurance for the whole class;
    # tools/verify_frozen_entrypoints.py pass 7 checks it is in every PYZ). The service no longer
    # calls a time-returning pywin32 function (face_service/datadir.py uses ctypes).
    "win32timezone",
    # New 8b modules, named so the graph does not depend on bytecode scanning (datadir and pipe_io
    # are also imported directly; selfcheck only from face_service/__main__.py).
    "face_service.datadir",
    "face_service.pipe_io",
    "face_service.selfcheck",
]

DATAS = []
# Stage 9 (D-152, F-51): no sample media. insightface's data/images (a celebrity photo, a group
# photo) and gui assets, and skimage's data/ (LFW face crops and more) are never read by the product.
DATAS += collect_data_files("insightface", excludes=["data/images/**", "gui/**"])
DATAS += collect_data_files("onnxruntime")
DATAS += collect_data_files("skimage", excludes=["data/**"])
DATAS += collect_data_files("cv2")

# Mine 3. insightface.data.get_object carries an EXPLICIT frozen branch
# (data/pickle_object.py:8-13): under sys.frozen it resolves objects/ against
# sys._MEIPASS -- the BUNDLE ROOT -- while collect_data_files above reproduces the package
# layout, insightface/data/objects/. The two never meet. get_object then returns None
# (it print()s the miss, and a windowed build has no stdout to print it to), so
# Landmark.mean_lmk is None for 1k3d68, which is the one model with require_pose set, and
# utils/transform.py estimate_affine_matrix_3d23d does X.shape[0] on it. Result: EVERY
# app.get() with a face in frame dies on "'NoneType' object has no attribute 'shape'"
# inside FaceAnalysis.get's per-face loop -- and 1k3d68 sorts FIRST, so recognition never
# runs at all and every frame reports d=1.000 on both service paths.
# landmark_3d_68 is load-bearing (recognizer.ALLOWED_MODULES feeds it the gesture pose), so
# the root copy is mandatory, not defensive. KNOWN_ISSUES #5 mine 3, 7k.
import insightface as _insightface

_IF_OBJECTS = Path(_insightface.__file__).resolve().parent / "data" / "objects"
DATAS += [(str(p), "objects") for p in sorted(_IF_OBJECTS.iterdir()) if p.is_file()]

def _keep_binary(entry) -> bool:
    name = Path(entry[0]).name.lower()
    # F-51: the TensorRT provider has no TensorRT runtime to load -- never shipped.
    if name.startswith("onnxruntime_providers_tensorrt"):
        return False
    # The CUDA provider only in the GPU variant.
    if name.startswith("onnxruntime_providers_cuda"):
        return VARIANT == "gpu"
    # D-151: the FFmpeg plugin serves files and URLs; the product opens cameras by index/name only.
    if name.startswith("opencv_videoio_ffmpeg"):
        return False
    return True


BINARIES = []
BINARIES += [b for b in collect_dynamic_libs("onnxruntime") if _keep_binary(b)]
BINARIES += [b for b in collect_dynamic_libs("cv2") if _keep_binary(b)]

# Bundle the YuNet detector. It is the only model committed to the repo.
DATAS += [
    (str(REPO_ROOT / "models" / "face_detection_yunet_2023mar.onnx"), "models"),
]

# ---------------------------------------------------------------------------
# The buffalo_l recognition pack is NOT in the bundle (Stage 9, decision 9-02 / act 9b R7, F-259).
#
# The InsightFace models are licensed for non-commercial research use and nothing grants
# redistribution. The installer downloads the official buffalo_l.zip (the URL insightface itself
# uses), shows the InsightFace terms and asks for consent, checks every file against the pins in
# face_service/model_pins.py and unpacks exactly the five files into {app}\models\buffalo_l --
# where face_service.recognizer.model_root() looks for them when frozen. installer/build.py fails
# the build if any model of the pack ends up in dist.

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
# Stage 9 (act 9b R17, F-262, F-263): the GPU variant ships ONLY the allowlist below -- what the
# ORT CUDA provider imports (cudart, cublas, cublasLt, cufft, cudnn64_9), the cuDNN 9 sublibraries
# it loads by name, and NVRTC, which cuDNN's runtime-compiled engines load. nvblas, cufftw, curand
# (imported by nothing), the nvrtc ".alt" copy and nvJitLink (not in the CUDA EULA's redistributable
# list) are left out. Files are copied byte-for-byte; build.py verifies each one's Authenticode
# signature and NEVER re-signs or modifies them. The CPU variant ships none of them, and a GPU build
# without the nvidia packages is an error, not a silent CPU build (F-219).
# ---------------------------------------------------------------------------
NVIDIA_ALLOWLIST = {
    "cuda_runtime": ["cudart64_12.dll"],
    "cublas": ["cublas64_12.dll", "cublasLt64_12.dll"],
    "cufft": ["cufft64_11.dll"],
    "cuda_nvrtc": ["nvrtc64_120_0.dll", "nvrtc-builtins64_129.dll"],
    "cudnn": ["cudnn64_9.dll", "cudnn_adv64_9.dll", "cudnn_cnn64_9.dll",
              "cudnn_engines_precompiled64_9.dll", "cudnn_engines_runtime_compiled64_9.dll",
              "cudnn_engines_tensor_ir64_9.dll", "cudnn_ext64_9.dll", "cudnn_graph64_9.dll",
              "cudnn_heuristic64_9.dll", "cudnn_ops64_9.dll"],
}
if VARIANT == "gpu":
    try:
        import nvidia as _nvidia
    except ImportError:
        raise SystemExit("GPU variant requested but the nvidia CUDA/cuDNN packages are not installed "
                         "(install the GPU lock) -- refusing to build a CPU bundle under a GPU name.")
    _missing = []
    for _pkg, _names in NVIDIA_ALLOWLIST.items():
        for _n in _names:
            _src = next((Path(r) / _pkg / "bin" / _n for r in _nvidia.__path__
                         if (Path(r) / _pkg / "bin" / _n).is_file()), None)
            if _src is None:
                _missing.append(f"{_pkg}/{_n}")
            else:
                DATAS.append((str(_src), f"nvidia/{_pkg}/bin"))
    if _missing:
        raise SystemExit("GPU variant: NVIDIA files missing: " + ", ".join(_missing))

EXCLUDES = [
    # Only insightface.thirdparty.face3d wants matplotlib, and only
    # insightface.gui wants Qt. Both are filtered out of HIDDEN above; excluding
    # them here keeps a stray transitive import from dragging the stacks in.
    "matplotlib",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "jupyter", "ipykernel", "notebook",
    # Stage 9 (F-51, D-153): packages no product code imports.
    "pandas", "sympy", "mpmath", "pip", "setuptools", "pkg_resources", "fsspec", "imageio",
    "tifffile", "jinja2", "bs4", "soupsieve", "filelock", "pytest", "IPython",
]

# Stage 9 (act 9b §2.9, F-261): pystray (LGPL-3.0) is collected as replaceable .py SOURCE files
# under _internal\pystray, outside every PYZ -- the user can swap in a modified pystray, as LGPL-3
# section 4(d) asks. Its license texts ship beside it (build.py stages them); only the tray uses it.
COLLECTION_MODE = {"pystray": "py"}

# Stage 9 (D-108, D-156): version resources for the three executables, from face_service/_version.py.
sys.path.insert(0, str(REPO_ROOT))
from face_service._version import __version__ as _APP_VERSION  # noqa: E402
from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo)


def _version_info(description: str, internal: str) -> VSVersionInfo:
    nums = tuple(int(x) for x in _APP_VERSION.split(".")[:3]) + (0,)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004,
                          fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "xbaox"),
            StringStruct("FileDescription", description),
            StringStruct("FileVersion", _APP_VERSION),
            StringStruct("InternalName", internal),
            StringStruct("LegalCopyright",
                         "Copyright (c) 2026 Cao Chí Tâm; modifications (c) 2026 xbaox. MIT License."),
            StringStruct("OriginalFilename", internal + ".exe"),
            StringStruct("ProductName", "Windows Face Unlock"),
            StringStruct("ProductVersion", _APP_VERSION)])]),
              VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])

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
    module_collection_mode=COLLECTION_MODE,
    noarchive=False,
)

tray_analysis = Analysis(
    [str(REPO_ROOT / "presence_monitor" / "__main__.py")],
    pathex=[str(REPO_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN + ["tkinter", "tkinter.ttk", "tkinter.messagebox", "pystray._win32"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    module_collection_mode=COLLECTION_MODE,
    noarchive=False,
)

watchdog_analysis = Analysis(
    [str(REPO_ROOT / "tools" / "watchdog.py")],
    pathex=[str(REPO_ROOT)],
    binaries=BINARIES,
    datas=DATAS,
    # tools/watchdog.py imports face_service.config, face_service.watchdog,
    # face_service.logging_setup and face_service.pipe_io INSIDE functions, so
    # they are named explicitly rather than trusted to bytecode scanning. It
    # needs neither cv2 nor the models -- it pings a named pipe and finds the
    # service process with psutil (Stage 9, F-251: no PowerShell child) -- but
    # MERGE puts the shared payload in the first analysis anyway.
    hiddenimports=HIDDEN + [
        "face_service.config",
        "face_service.watchdog",
        "face_service.logging_setup",
        "face_service.pipe_io",
        "psutil",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    module_collection_mode=COLLECTION_MODE,
    noarchive=False,
)

# Share Python DLLs + site-packages between the EXEs to avoid multiplying
# the bundle size. The first member owns the shared payload.
MERGE(
    (service_analysis, "face_service", "face_service"),
    (tray_analysis, "face_unlock_tray", "face_unlock_tray"),
    (watchdog_analysis, "watchdog", "face_unlock_watchdog"),
)

service_pyz = PYZ(service_analysis.pure, service_analysis.zipped_data)
tray_pyz = PYZ(tray_analysis.pure, tray_analysis.zipped_data)
watchdog_pyz = PYZ(watchdog_analysis.pure, watchdog_analysis.zipped_data)

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
    disable_windowed_traceback=False,
    icon=None,
    version=_version_info("Windows Face Unlock service", "face_service"),
)

# Stage 9 (act 9b R12, F-170): the tray exe hosts every window (tray, wizard, password dialog), so it
# declares per-monitor-v2 DPI awareness in its manifest -- sharp text at 125-200 % instead of a
# bitmap-stretched blur. The rest is PyInstaller's default manifest (asInvoker, supported OS,
# long paths); PyInstaller adds the Common-Controls dependency itself. The process also sets the
# same mode at run time (presence_monitor.ui.enable_dpi_awareness) for the dev layout.
TRAY_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
      <supportedOS Id="{1f676c76-80e1-4239-95bb-83d0f6d0da78}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
      <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true/pm</dpiAware>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2, PerMonitor</dpiAwareness>
    </windowsSettings>
  </application>
</assembly>
"""

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
    disable_windowed_traceback=False,
    icon=None,
    manifest=TRAY_MANIFEST,
    version=_version_info("Windows Face Unlock tray, setup wizard and task registrar",
                          "face_unlock_tray"),
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
    # Windowed for the same reason as the other two: it runs from a Scheduled Task with no
    # interactive console; its schtasks child runs with CREATE_NO_WINDOW.
    console=False,
    disable_windowed_traceback=False,
    icon=None,
    version=_version_info("Windows Face Unlock watchdog", "face_unlock_watchdog"),
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
