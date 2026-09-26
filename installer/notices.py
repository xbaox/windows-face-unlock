"""Third-party license texts that ship with the bundle (Stage 9, F-260 / act 9b R17, §2.9).

``stage(dist_root, variant, iscc)`` fills ``<bundle>\\licenses\\`` from the BUILD interpreter:

  * every runtime package pinned in requirements.lock (+ PyInstaller, whose bootloader and runtime
    hooks are inside each exe): the license / copying / notice files its dist-info declares;
  * packages whose wheel carries no such file get the text from where it really is
    (onnxruntime: its package folder) or a written statement (insightface: MIT per its README;
    flatbuffers: Apache-2.0, text taken from another Apache-2.0 package);
  * the CPython LICENSE.txt (it also covers OpenSSL, bzip2, libffi, xz, zlib, Tcl/Tk, expat and the
    Microsoft Distributable Code conditions of the MSVC runtime);
  * the YuNet model's MIT text (models/LICENSE-yunet) and Inno Setup's license (the uninstaller);
  * GPU variant: the NVIDIA EULA texts of every NVIDIA wheel the bundle takes DLLs from;
  * ``licenses\\INDEX.txt``: folder -> package, version, declared license.

``check(dist_root, variant)`` is the gate's half: every expected folder is present and non-empty,
THIRD_PARTY_NOTICES.md and LICENSE are at the bundle root. Build-time only, never shipped.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "requirements.lock"
LICENSE_NAME_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|THIRDPARTYNOTICES|PRIVACY)", re.I)
BUILD_DISTS = ("pyinstaller", "pyinstaller-hooks-contrib")
NVIDIA_PREFIX = "nvidia-"

INSIGHTFACE_TEXT = """insightface (Python package) -- MIT License

The insightface wheel carries no license file. Its README (github.com/deepinsight/insightface,
python-package/README.md) states: "The library code is released under the MIT License, for
academic and commercial use." Copyright (c) the InsightFace authors (deepinsight/insightface).

The PRETRAINED MODELS are NOT covered by this: "The pretrained models provided with this library are
for non-commercial research only, whether downloaded automatically or manually." Windows Face Unlock
does not redistribute them; Setup downloads the official buffalo_l.zip only after you accept those
terms (see THIRD_PARTY_NOTICES.md).

MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT
OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

FLATBUFFERS_HEAD = """flatbuffers -- Apache License 2.0
Copyright 2014 Google Inc. (github.com/google/flatbuffers). The wheel carries no license file; the
Apache License 2.0 text follows (taken verbatim from another Apache-2.0 package of this bundle).

"""


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_dists(lock: Path = LOCK) -> "list[str]":
    """Package names pinned in requirements.lock (``name==version`` lines)."""
    names = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", line.strip())
        if m:
            names.append(m.group(1))
    return names


def expected_folders(variant: str, lock: Path = LOCK) -> "list[str]":
    """Folder names under licenses\\ the gate insists on (NVIDIA ones are checked as a group)."""
    names = [_norm(n) for n in runtime_dists(lock)] + list(BUILD_DISTS)
    names += ["python", "yunet", "inno-setup"]
    if variant == "gpu":
        names.append("nvidia")
    return sorted(set(names))


def _dist_license_files(dist) -> "list[Path]":
    out = []
    for f in dist.files or []:
        p = Path(str(f))
        if ".dist-info" in str(f) and LICENSE_NAME_RE.match(p.name):
            out.append(Path(dist.locate_file(f)))
    return out


def _copy(src: Path, dest_dir: Path, name: "str | None" = None) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest_dir / (name or src.name))


def stage(dist_root: Path, variant: str, iscc: "str | None", lock: Path = LOCK) -> "list[str]":
    """Fill <dist_root>\\licenses. Returns problems (empty = complete)."""
    from importlib import metadata
    lic = dist_root / "licenses"
    if lic.exists():
        shutil.rmtree(lic)
    lic.mkdir(parents=True)
    problems: list[str] = []
    index: list[str] = []
    apache_text: "Path | None" = None

    for name in runtime_dists(lock) + list(BUILD_DISTS):
        folder = lic / _norm(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: not installed in the build interpreter")
            continue
        declared = (dist.metadata.get("License-Expression") or dist.metadata.get("License") or "")
        declared = declared.strip().splitlines()[0][:60] if declared.strip() else "see files"
        files = _dist_license_files(dist)
        for i, f in enumerate(files):
            # nested license files (numpy's vendored libraries) keep their relative path as a name
            rel = str(f).split(".dist-info", 1)[-1].strip("\\/").replace("\\", "/")
            rel = rel[len("licenses/"):] if rel.startswith("licenses/") else rel
            _copy(f, folder, rel.replace("/", "__"))
            if apache_text is None and _norm(name) == "requests" and f.name == "LICENSE":
                apache_text = f
        if _norm(name) in ("onnxruntime-gpu", "onnxruntime"):
            import importlib.util
            spec = importlib.util.find_spec("onnxruntime")
            pkg = Path(spec.origin).parent if spec and spec.origin else None
            for n in ("LICENSE", "ThirdPartyNotices.txt", "Privacy.md"):
                if pkg is not None and (pkg / n).is_file():
                    _copy(pkg / n, folder)
        if _norm(name) == "insightface":
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "LICENSE.txt").write_text(INSIGHTFACE_TEXT, encoding="utf-8")
        index.append(f"{_norm(name):28} {dist.version:16} {declared}")

    fb = lic / "flatbuffers"
    if "flatbuffers" in {_norm(n) for n in runtime_dists(lock)} and \
            not (fb.is_dir() and any(fb.iterdir())):
        if apache_text is not None:
            fb.mkdir(parents=True, exist_ok=True)
            (fb / "LICENSE.txt").write_text(FLATBUFFERS_HEAD + apache_text.read_text(encoding="utf-8"),
                                            encoding="utf-8")

    py_lic = Path(sys.base_prefix) / "LICENSE.txt"
    if py_lic.is_file():
        _copy(py_lic, lic / "python")
        index.append(f"{'python':28} {sys.version.split()[0]:16} PSF-2.0 (+ OpenSSL, bzip2, libffi, "
                     "xz, zlib, Tcl/Tk, expat, MSVC runtime conditions)")
    else:
        problems.append(f"CPython license not found at {py_lic}")

    yunet = REPO_ROOT / "models" / "LICENSE-yunet"
    if yunet.is_file():
        _copy(yunet, lic / "yunet", "LICENSE.txt")
        index.append(f"{'yunet':28} {'2023mar':16} MIT (face_detection_yunet_2023mar.onnx)")
    else:
        problems.append("models/LICENSE-yunet missing")

    inno = Path(iscc).parent / "license.txt" if iscc else None
    if inno is not None and inno.is_file():
        _copy(inno, lic / "inno-setup")
        index.append(f"{'inno-setup':28} {'':16} Inno Setup License (the uninstaller, unins000.exe)")
    else:
        problems.append("Inno Setup license.txt not found next to ISCC.exe")

    if variant == "gpu":
        n = 0
        for dist in metadata.distributions():
            dn = _norm(dist.metadata["Name"] or "")
            if dn.startswith(NVIDIA_PREFIX):
                # only wheels the bundle actually takes DLLs from (the allowlist drops e.g. curand,
                # nvJitLink): nvidia-cuda-runtime-cu12 -> _internal\nvidia\cuda_runtime
                pkg = re.sub(r"-cu\d+$", "", dn[len(NVIDIA_PREFIX):]).replace("-", "_")
                shipped = dist_root / "_internal" / "nvidia" / pkg
                if not (shipped.is_dir() and any(shipped.rglob("*.dll"))):
                    continue
                files = [Path(dist.locate_file(f)) for f in dist.files or []
                         if Path(str(f)).name.lower() == "license.txt"]
                for f in files:
                    _copy(f, lic / "nvidia" / dn)
                    n += 1
                index.append(f"{'nvidia/' + dn:28} {dist.version:16} NVIDIA proprietary EULA "
                             "(not MIT; redistributed only as part of this application)")
        if not n:
            problems.append("GPU variant: no NVIDIA license text found")

    (lic / "INDEX.txt").write_text(
        "Third-party license texts shipped with Windows Face Unlock (see THIRD_PARTY_NOTICES.md).\n"
        "folder                       version          declared license\n"
        + "\n".join(sorted(index)) + "\n", encoding="utf-8")
    problems += check(dist_root, variant, lock, notices_doc=False)
    return problems


def check(dist_root: Path, variant: str, lock: Path = LOCK, notices_doc: bool = True) -> "list[str]":
    """The gate: every expected licenses\\ folder is present and non-empty."""
    lic = dist_root / "licenses"
    problems = []
    for folder in expected_folders(variant, lock):
        d = lic / folder
        if not d.is_dir() or not any(p.is_file() for p in d.rglob("*")):
            problems.append(f"licenses\\{folder} missing or empty")
    if variant == "cpu" and (lic / "nvidia").exists():
        problems.append("CPU variant ships NVIDIA license texts (no NVIDIA file may ship)")
    if not (lic / "INDEX.txt").is_file():
        problems.append("licenses\\INDEX.txt missing")
    if notices_doc:
        for doc in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            if not (dist_root / doc).is_file():
                problems.append(f"{doc} missing at the bundle root")
    return problems
