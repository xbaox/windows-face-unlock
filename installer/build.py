"""End-to-end installer build.

Runs these steps in order:
  1. build the Credential Provider DLL via CMake (Release x64) into build-cp/
  2. run PyInstaller on installer/windows_face_unlock.spec
  3. stage the CP DLL, the task registrar and the docs into the dist folder
  4. compile installer/installer.iss with Inno Setup
  5. emit SHA-256 checksums next to the installer

Intended to run both locally and in CI. Environment:
    INNO_SETUP_ISCC — full path to ISCC.exe (default: search PATH)
    SKIP_CP        — set to 1 to build a presence-auto-lock-only installer
                     with no Credential Provider. This is the ONLY way to
                     skip the DLL; a failing CP build aborts the run.
"""
from __future__ import annotations
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = REPO_ROOT / "installer"
CP_DIR = REPO_ROOT / "credential_provider"          # C++ sources
CP_BUILD_DIR = REPO_ROOT / "build-cp"               # CMake tree for the CP DLL
TOOLS_DIR = REPO_ROOT / "tools"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"                     # PyInstaller work dir, unrelated to CP
OUTPUT_DIR = REPO_ROOT / "installer_output"


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None, env=env)


def python_exe() -> str:
    return sys.executable


def step_build_cp() -> Path | None:
    """Build the Credential Provider DLL into build-cp/ (repo root).

    Two deliberate properties:

    * The tree is build-cp/ at the repo root, never a subdirectory of the
      sources. build-cp/ is what LogonUI's registered CLSID actually points at,
      and it is NOT wiped first: a build script has no business deleting the
      DLL the lock screen is currently loading. CMake rebuilds incrementally.
    * A CP build failure is fatal. It used to be swallowed, so a broken
      toolchain silently produced an installer whose face tile could never
      appear. Opting out is now explicit and only via SKIP_CP=1.
    """
    if os.environ.get("SKIP_CP") == "1":
        log("step 1/5 — skipping Credential Provider DLL (SKIP_CP=1)")
        return None
    log("step 1/5 — build Credential Provider DLL")
    run(["cmake", "-S", CP_DIR.name, "-B", CP_BUILD_DIR.name, "-A", "x64",
         "-G", "Visual Studio 17 2022"], cwd=REPO_ROOT)
    run(["cmake", "--build", CP_BUILD_DIR.name, "--config", "Release"], cwd=REPO_ROOT)
    dll = CP_BUILD_DIR / "Release" / "FaceCredentialProvider.dll"
    if not dll.exists():
        raise RuntimeError(
            f"cmake reported success but {dll} is missing. Set SKIP_CP=1 to build "
            "a presence-auto-lock-only installer on purpose."
        )
    return dll


def step_pyinstaller() -> Path:
    log("step 2/5 — PyInstaller")
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    run([
        python_exe(), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        str(INSTALLER_DIR / "windows_face_unlock.spec"),
    ], cwd=REPO_ROOT)
    out = DIST_DIR / "WindowsFaceUnlock"
    if not out.exists():
        raise RuntimeError(f"expected PyInstaller output at {out}")
    return out


def step_stage(dist_root: Path, cp_dll: Path | None) -> None:
    log("step 3/5 — stage CP DLL + task registrar + docs into dist")
    if cp_dll and cp_dll.exists():
        dest = dist_root / "credential_provider"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cp_dll, dest / "FaceCredentialProvider.dll")
        # keep the register script alongside the DLL for the installer
        reg = CP_DIR / "register.ps1"
        if reg.exists():
            shutil.copy2(reg, dest / "register.ps1")

    # Scheduled-task registrar + its declaration. installer.iss invokes this on
    # install AND on uninstall, and tasks.psd1 is the only place the task list
    # exists, so the two must travel together or neither is usable. Missing
    # files are fatal: an installer that cannot register its tasks is not one.
    post = dist_root / "postinstall"
    post.mkdir(parents=True, exist_ok=True)
    for name in ("register_tasks.ps1", "tasks.psd1"):
        src = TOOLS_DIR / name
        if not src.exists():
            raise RuntimeError(f"cannot stage the task registrar: {src} is missing")
        shutil.copy2(src, post / name)

    for doc in ("README.md", "LICENSE", "INSTALL.md"):
        p = REPO_ROOT / doc
        if p.exists():
            shutil.copy2(p, dist_root / doc)


def _find_iscc() -> str:
    env = os.environ.get("INNO_SETUP_ISCC")
    if env and Path(env).exists():
        return env
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    # winget installs Inno Setup to the user profile by default.
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(str(Path(local_appdata) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    exe = shutil.which("iscc") or shutil.which("ISCC")
    if exe:
        return exe
    raise FileNotFoundError(
        "ISCC.exe (Inno Setup) not found. Install from https://jrsoftware.org/isdl.php "
        "or set INNO_SETUP_ISCC to its path."
    )


def step_inno() -> Path:
    log("step 4/5 — Inno Setup")
    iscc = _find_iscc()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run([iscc, str(INSTALLER_DIR / "installer.iss")], cwd=REPO_ROOT)
    # Expected output: installer_output\WindowsFaceUnlock-Setup-<ver>.exe
    outputs = sorted(OUTPUT_DIR.glob("WindowsFaceUnlock-Setup-*.exe"))
    if not outputs:
        raise RuntimeError(f"Inno Setup produced no artefact in {OUTPUT_DIR}")
    return outputs[-1]


def step_checksums(installer_path: Path) -> None:
    log("step 5/5 — SHA-256 checksums")
    h = hashlib.sha256()
    with installer_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    (installer_path.parent / f"{installer_path.name}.sha256").write_text(
        f"{digest}  {installer_path.name}\n", encoding="ascii"
    )
    log(f"sha256 = {digest}")


def main() -> int:
    cp_dll = step_build_cp()
    dist_root = step_pyinstaller()
    step_stage(dist_root, cp_dll)
    installer_path = step_inno()
    step_checksums(installer_path)
    log(f"Installer ready: {installer_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
