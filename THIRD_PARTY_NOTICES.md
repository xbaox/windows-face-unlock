# Third-party notices

Windows Face Unlock is MIT-licensed ([LICENSE](LICENSE)). It is built from, and ships, the
components below, each under its **own** license. The full license texts ship with the program in
the `licenses\` folder of the installation (`C:\Program Files\WindowsFaceUnlock\licenses\`), one
subfolder per component, with an `INDEX.txt` listing the exact versions of the build. The build
fails if any of them is missing.

The MIT license of this project does **not** apply to any component listed here.

## Not included: the face-recognition models

The InsightFace **buffalo_l** models (`det_10g`, `w600k_r50`, `2d106det`, `1k3d68`,
`genderage`) are **not part of this program and are not redistributed by it**. InsightFace states:
*"The pretrained models provided with this library are for non-commercial research only, whether
downloaded automatically or manually."* Setup shows these terms, and only after you accept them
downloads the official archive from InsightFace's own release
(`https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip`), verified by its
SHA-256. Whether your use is permitted by those terms is between you and InsightFace.

## Components shipped in the installation

| Component | License | Notes |
|---|---|---|
| CPython 3.12 runtime and standard library | PSF-2.0 | `licenses\python\LICENSE.txt` also covers the bundled OpenSSL (Apache-2.0), bzip2, libffi, xz/liblzma, zlib, expat, libmpdec, SQLite (public domain), Tcl/Tk, and states Microsoft's conditions for the Visual C++ runtime DLLs |
| Microsoft Visual C++ runtime (`vcruntime140*.dll`, `msvcp140*.dll`; static CRT in the lock-screen DLL) | Microsoft Visual Studio Distributable Code | Redistributed unmodified, only as part of this application |
| PyInstaller bootloader and run-time hooks | GPL-2.0-or-later **with the Bootloader Exception**; hooks Apache-2.0 | The exception permits distributing the executables under our own terms; `licenses\pyinstaller\`, `licenses\pyinstaller-hooks-contrib\` |
| Inno Setup (Setup and the uninstaller `unins000.exe`) | Inno Setup License | Copyright (C) 1997-2026 Jordan Russell, Martijn Laan; `licenses\inno-setup\` |
| ONNX Runtime (`onnxruntime-gpu` package; in the CPU variant without the CUDA provider) | MIT | Its `ThirdPartyNotices.txt` (Eigen MPL-2.0, protobuf, and others) and `Privacy.md` ship in `licenses\onnxruntime-gpu\`. Face Unlock turns ORT's telemetry off. Eigen source: https://gitlab.com/libeigen/eigen |
| ONNX | Apache-2.0 | with its NOTICE |
| OpenCV (`opencv-python`) | Apache-2.0 (OpenCV), MIT (packaging) | `LICENSE-3RD-PARTY.txt` covers the libraries built into `cv2`. The FFmpeg video plugin is **not** shipped. |
| YuNet face detector (`face_detection_yunet_2023mar.onnx`, OpenCV Zoo) | MIT | Copyright (c) 2020 Shiqi Yu; `licenses\yunet\` |
| insightface (Python library code) | MIT | The wheel has no license file; its README states MIT. `licenses\insightface\` records this. The models are covered above, not by MIT. |
| NumPy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Includes OpenBLAS (BSD-3-Clause), LAPACK (BSD-3-Clause-Open-MPI) and the GCC runtime libraries (GPL-3.0-or-later **with the GCC Runtime Library Exception**) |
| SciPy | BSD-3-Clause | Same OpenBLAS / LAPACK / GCC runtime notices as NumPy |
| scikit-image | BSD-3-Clause (some files BSD-2-Clause) | sample data is not shipped |
| Pillow | MIT-CMU | Portions of this software are copyright © The FreeType Project (www.freetype.org). All rights reserved. This software is based in part on the work of the Independent JPEG Group. Also includes libjpeg-turbo, libpng, libwebp, libtiff, OpenJPEG, lcms2, HarfBuzz, zlib, xz, brotli, libavif (see its LICENSE) |
| **pystray** | **LGPL-3.0** | See [pystray and the LGPL](#pystray-and-the-lgpl) |
| pywin32 | PSF-2.0 | |
| psutil | BSD-3-Clause | |
| protobuf | BSD-3-Clause | |
| ml_dtypes | Apache-2.0; includes Eigen headers under **MPL-2.0** | Eigen source: https://gitlab.com/libeigen/eigen |
| flatbuffers | Apache-2.0 | Copyright 2014 Google Inc. |
| tqdm | MPL-2.0 AND MIT | Source: https://github.com/tqdm/tqdm (PyPI sdist `tqdm`) |
| certifi | **MPL-2.0** | `cacert.pem` ships in source form; source: https://github.com/certifi/python-certifi |
| requests | Apache-2.0 | with its NOTICE |
| urllib3, charset-normalizer, six | MIT | |
| idna, lazy-loader, networkx, tifffile | BSD-3-Clause | |
| imageio | BSD-2-Clause | |
| packaging | Apache-2.0 OR BSD-2-Clause | |
| typing_extensions | PSF-2.0 | |
| tomli_w | MIT | |
| colorama | BSD-3-Clause | |

The table follows `requirements.lock`; the build copies the license files of exactly the versions
it was built with, so `licenses\INDEX.txt` is authoritative for a given installer.

### GPU variant only: NVIDIA components

`WindowsFaceUnlock-Setup-<version>-gpu.exe` additionally contains NVIDIA CUDA runtime, cuBLAS /
cuBLASLt, cuFFT, NVRTC and cuDNN 9 DLLs, taken **unmodified** from NVIDIA's official
redistributable packages. **They are not licensed under MIT.** They are licensed under the NVIDIA
Software License Agreement / CUDA Toolkit supplement and the cuDNN supplement, whose texts
(including their third-party notices) ship in `licenses\nvidia\`. They are redistributed only as
part of this application and may be used only by it. Only the libraries ONNX Runtime's CUDA
provider needs are included. NVIDIA files are never re-signed or modified by this project.

### pystray and the LGPL

The tray icon uses [pystray](https://github.com/moses-palmer/pystray) (LGPL-3.0). To keep your
LGPL rights intact, pystray is shipped as **plain, replaceable Python source files** in
`_internal\pystray\` of the installation (not compiled into the executables): you may replace them
with a modified version of pystray and the tray will use it. The GNU LGPL-3.0 and GPL-3.0 texts
are in `licenses\pystray\`. The source of the shipped version is the files themselves and the
`pystray` 0.19.5 release on PyPI / GitHub.

## Upstream project

This project started as a fork of https://github.com/caochitam/windows-face-unlock (MIT,
Copyright (c) 2026 Cao Chí Tâm). That copyright notice is kept in [LICENSE](LICENSE).

## Build-only tools (not shipped)

CMake, the Microsoft C++ build tools and Windows SDK, PyInstaller's build machinery, and Inno Setup's
compiler are used to build the installer and are not redistributed beyond the parts listed above.
