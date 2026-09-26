"""End-to-end installer build (Stage 9, act 9b R17 / R18).

    python installer\\build.py --variant cpu          (the main variant)
    python installer\\build.py --variant gpu          (NVIDIA; needs requirements-gpu.lock)

Steps, in this order (the signing order is R18's):
  0. preflight -- cmake, ISCC, PyInstaller, the variant's packages; the output directory for this
     variant is cleaned (F-220, F-221: fail in seconds, not after the whole build)
  1. build the Credential Provider DLL (CMake, Release x64)
  2. sign the CP DLL                                   -- Azure Trusted Signing, or LOUDLY SKIPPED
  3. PyInstaller (FU_VARIANT=<variant>), then stage the CP DLL, the docs and the LGPL texts
  4. GPU only: every shipped NVIDIA DLL is checked with Authenticode (signed by NVIDIA Corporation)
     and never modified; unsigned ones need a catalog signature (.cat) under our identity --
     LOUDLY SKIPPED without signing credentials, recorded in the stamp
  5. sign every unsigned PE of ours in the bundle -- never NVIDIA's, never an already-signed file
                                                       -- Azure Trusted Signing, or LOUDLY SKIPPED
  6. THE GATE on the (signed) bundle: verify_frozen_entrypoints (PE content compared WITHOUT the
     certificate table and checksum), packaging_selftest, the frozen custody self-check, no model
     in the bundle, the variant's shape (CPU: no CUDA provider and no NVIDIA file; GPU: exactly the
     allowlist), the CP DLL; then the stamp
  7. ISCC (version, variant and the model pins as /D defines; SignTool + SignedUninstaller only
     with credentials); the EXACT file ISCC was told to write is the artefact (F-220); the bundle
     is re-hashed after ISCC and must still match the stamp (F-225)
  8. SHA-256 sidecar and a build-info sidecar (manifest, git head, variant, signing state)

Signing (R18, P-24): Azure Trusted Signing through signtool + the Azure.CodeSigning dlib, when all
of AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CODESIGN_ENDPOINT, AZURE_CODESIGN_ACCOUNT,
AZURE_CODESIGN_PROFILE, SIGNTOOL and AZURE_CODESIGN_DLIB are set (in CI: OIDC). The expected
signer is pinned by subject (FU_SIGN_SUBJECT) and every signed file must verify 'Valid'. Without
the credentials every signing step says SKIPPED and why; the build still completes (until 9f).

    --half 1 / --half 2     steps 0-6 / steps 7-8 (the operator dist-smoke sits between)
    --gate-only             re-run step 6 on the existing dist
    --resign                on an existing dist: steps 2, 4, 5, 6 (the CI sign job)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER_DIR = REPO_ROOT / "installer"
CP_DIR = REPO_ROOT / "credential_provider"
CP_BUILD_DIR = REPO_ROOT / "build-cp"
TOOLS_DIR = REPO_ROOT / "tools"
DIST_DIR = REPO_ROOT / "dist"
DIST_ROOT = DIST_DIR / "WindowsFaceUnlock"
BUILD_DIR = REPO_ROOT / "build"
OUTPUT_DIR = REPO_ROOT / "installer_output"
ISS = INSTALLER_DIR / "installer.iss"
CP_DLL_NAME = "FaceCredentialProvider.dll"
GATE_STAMP = DIST_DIR / "WindowsFaceUnlock.gate.json"
GATE_STAMP_SCHEMA = 2
TOTAL_STEPS = 8

sys.path.insert(0, str(REPO_ROOT))
from face_service._version import __version__ as VERSION  # noqa: E402
from face_service.model_pins import (BUFFALO_FILES, BUFFALO_ZIP_BYTES,  # noqa: E402
                                     BUFFALO_ZIP_SHA256, BUFFALO_ZIP_URL)

MODEL_SHA256 = {name: digest for name, (_size, digest) in BUFFALO_FILES.items()}
AZURE_VARS = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CODESIGN_ENDPOINT",
              "AZURE_CODESIGN_ACCOUNT", "AZURE_CODESIGN_PROFILE", "SIGNTOOL", "AZURE_CODESIGN_DLIB")
NVIDIA_SUBJECT = "CN=NVIDIA Corporation"


class BuildAbort(RuntimeError):
    """A refusal with a message meant for the operator, not a stack trace."""


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def loud(msg: str) -> None:
    bar = "!" * 78
    print(f"[build] {bar}\n[build] {msg}\n[build] {bar}", flush=True)


def step(n: int, msg: str) -> None:
    log(f"step {n}/{TOTAL_STEPS} -- {msg}")


def run(cmd: list, cwd: "Path | None" = None, env: "dict | None" = None) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        subprocess.check_call([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env)
    except FileNotFoundError as e:
        raise BuildAbort(f"{cmd[0]} was not found ({e}). Install it or put it on PATH.") from e


def child_env(**extra) -> dict:
    """F-246: UTF-8 stdio for every Python child, whatever the console code page."""
    return dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", **extra)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def artefact_name(variant: str) -> str:
    return f"WindowsFaceUnlock-Setup-{VERSION}-{variant}.exe"


def iscc_defines(variant: str) -> "list[str]":
    """Everything installer.iss takes from outside: version, variant, the model pins."""
    d = [f"/DMyAppVersion={VERSION}", f"/DVariant={variant}", f"/DBuffaloURL={BUFFALO_ZIP_URL}",
         f"/DBuffaloSHA256={BUFFALO_ZIP_SHA256}", f"/DBuffaloBytes={BUFFALO_ZIP_BYTES}"]
    for i, (name, (_size, sha)) in enumerate(BUFFALO_FILES.items(), 1):
        d += [f"/DModel{i}={name}", f"/DModelSHA{i}={sha}"]
    return d


# ---------------------------------------------------------------------------------- signing
def azure_ready() -> bool:
    return all(os.environ.get(v) for v in AZURE_VARS)


def azure_missing() -> "list[str]":
    return [v for v in AZURE_VARS if not os.environ.get(v)]


_SIG_PS = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = Get-AuthenticodeSignature -LiteralPath $env:FU_SIG_PATH; "
    "$c = $s.SignerCertificate; "
    "[pscustomobject]@{ Status = [string]$s.Status; Type = [string]$s.SignatureType; "
    "Subject = $(if ($c) { $c.Subject } else { '' }); Message = [string]$s.StatusMessage "
    "} | ConvertTo-Json -Compress"
)


def authenticode(path: Path) -> dict:
    """Get-AuthenticodeSignature as data (a BUILD-time check; the installer uses no PowerShell).
    The path travels in the environment, never in the command line."""
    out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                          "Bypass", "-Command", _SIG_PS], capture_output=True, text=True,
                         env=dict(os.environ, FU_SIG_PATH=str(path)))
    if out.returncode != 0:
        raise BuildAbort(f"Get-AuthenticodeSignature failed on {path}: {out.stderr.strip()}")
    try:
        return json.loads(out.stdout)
    except ValueError as e:
        raise BuildAbort(f"unreadable signature report for {path}: {out.stdout[:200]!r}") from e


def azure_sign(files: "list[Path]", what: str) -> "dict":
    """Sign ``files`` with Azure Trusted Signing and verify each ('Valid', pinned subject).
    Without credentials: a loud SKIP, returned as data for the stamp."""
    if not files:
        return {"state": "nothing-to-sign", "files": 0}
    if not azure_ready():
        loud(f"SIGNING SKIPPED ({what}): no Azure Trusted Signing credentials "
             f"(missing: {', '.join(azure_missing())}). {len(files)} file(s) stay UNSIGNED. "
             "This build is not releasable until 9f.")
        return {"state": "skipped-no-credentials", "files": len(files)}
    meta = BUILD_DIR / "azure-codesign.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"Endpoint": os.environ["AZURE_CODESIGN_ENDPOINT"],
                                "CodeSigningAccountName": os.environ["AZURE_CODESIGN_ACCOUNT"],
                                "CertificateProfileName": os.environ["AZURE_CODESIGN_PROFILE"]}),
                    encoding="utf-8")
    for chunk_start in range(0, len(files), 50):
        chunk = files[chunk_start:chunk_start + 50]
        run([os.environ["SIGNTOOL"], "sign", "/v", "/fd", "SHA256", "/tr",
             "http://timestamp.acs.microsoft.com", "/td", "SHA256", "/dlib",
             os.environ["AZURE_CODESIGN_DLIB"], "/dmdf", str(meta), *chunk])
    subject = os.environ.get("FU_SIGN_SUBJECT", "")
    for f in files:
        sig = authenticode(f)
        if sig.get("Status") != "Valid" or (subject and subject not in (sig.get("Subject") or "")):
            raise BuildAbort(f"{f}: signature {sig.get('Status')} by {sig.get('Subject')!r} -- "
                             f"expected Valid by {subject or '(FU_SIGN_SUBJECT not set)'}")
    log(f"signed and verified {len(files)} file(s) ({what})")
    return {"state": "signed", "files": len(files), "subject": subject}


# ---------------------------------------------------------------------------------------- 0
def find_iscc() -> str:
    env = os.environ.get("INNO_SETUP_ISCC")
    if env and Path(env).exists():
        return env
    cands = [r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe"]
    if os.environ.get("LOCALAPPDATA"):
        cands.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for c in cands:
        if Path(c).exists():
            return c
    exe = shutil.which("iscc") or shutil.which("ISCC")
    if exe:
        return exe
    raise BuildAbort("ISCC.exe (Inno Setup 6.5+) not found: install it or set INNO_SETUP_ISCC.")


def step_preflight(variant: str, need_cp: bool) -> dict:
    step(0, f"preflight ({variant.upper()} variant)")
    tools = {"iscc": find_iscc()}
    if need_cp:
        cm = shutil.which("cmake")
        if not cm:
            raise BuildAbort("cmake not found (it ships inside Visual Studio 2022 Build Tools) -- "
                             "put it on PATH, or set SKIP_CP=1 for a build without the sign-in tile.")
        tools["cmake"] = cm
    import importlib.util
    if importlib.util.find_spec("PyInstaller") is None:
        raise BuildAbort("PyInstaller missing: pip install --require-hashes -r "
                         "installer/requirements-build.txt")
    has_nvidia = importlib.util.find_spec("nvidia") is not None
    if variant == "gpu" and not has_nvidia:
        raise BuildAbort("the GPU variant needs the NVIDIA wheels: pip install --require-hashes -r "
                         "requirements-gpu.lock (F-219: never a CPU bundle under a GPU name)")
    if not (REPO_ROOT / "LICENSE").is_file():
        raise BuildAbort("LICENSE is missing -- the MIT notice must ship (D-114)")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob(f"WindowsFaceUnlock-Setup-*-{variant}.exe*"):
        old.unlink()
        log(f"removed stale output {old.name}")
    log(f"version {VERSION}, variant {variant}, artefact {artefact_name(variant)}")
    return tools


def cp_skipped() -> bool:
    return os.environ.get("SKIP_CP") == "1"


# ---------------------------------------------------------------------------------------- 1
def step_build_cp() -> "Path | None":
    if cp_skipped():
        step(1, "Credential Provider DLL -- skipped (SKIP_CP=1)")
        return None
    step(1, "build the Credential Provider DLL")
    run(["cmake", "-S", CP_DIR.name, "-B", CP_BUILD_DIR.name, "-A", "x64", "-G", "Visual Studio 17 2022"],
        cwd=REPO_ROOT)
    run(["cmake", "--build", CP_BUILD_DIR.name, "--config", "Release"], cwd=REPO_ROOT)
    dll = CP_BUILD_DIR / "Release" / CP_DLL_NAME
    if not dll.exists():
        raise BuildAbort(f"cmake reported success but {dll} is missing")
    return dll


# ---------------------------------------------------------------------------------------- 2
def step_sign_cp(dll: "Path | None") -> dict:
    step(2, "sign the CP DLL")
    if dll is None:
        return {"state": "no-cp"}
    return azure_sign([dll], "Credential Provider DLL")


# ---------------------------------------------------------------------------------------- 3
def step_pyinstaller(variant: str, cp_dll: "Path | None") -> Path:
    step(3, f"PyInstaller ({variant}) + staging")
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         str(INSTALLER_DIR / "windows_face_unlock.spec")], cwd=REPO_ROOT,
        env=child_env(FU_VARIANT=variant))
    if not DIST_ROOT.exists():
        raise BuildAbort(f"expected PyInstaller output at {DIST_ROOT}")
    # D-122: the package versions the bundle was built from, so the gate can tell when it scans a
    # different interpreter (verify_frozen_entrypoints 6b).
    from importlib import metadata
    (DIST_DIR / (DIST_ROOT.name + ".build-env.json")).write_text(json.dumps(
        {d.metadata["Name"]: d.version for d in metadata.distributions() if d.metadata["Name"]},
        indent=0, sort_keys=True), encoding="utf-8")
    if cp_dll is not None:
        dest = DIST_ROOT / "credential_provider"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cp_dll, dest / CP_DLL_NAME)
        # (D-114: register.ps1 is a developer tool; it no longer ships.)
    for doc in ("LICENSE", "README.md", "INSTALL.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md"):
        p = REPO_ROOT / doc
        if p.exists():
            shutil.copy2(p, DIST_ROOT / doc)
    # §2.9 (F-261): pystray's license texts beside its replaceable sources.
    try:
        from importlib import metadata
        dist = metadata.distribution("pystray")
        lic_dir = DIST_ROOT / "licenses" / "pystray"
        lic_dir.mkdir(parents=True, exist_ok=True)
        for f in dist.files or []:
            if Path(str(f)).name.upper().startswith(("COPYING", "LICENSE")):
                shutil.copy2(Path(dist.locate_file(f)), lic_dir / Path(str(f)).name)
    except Exception as e:
        raise BuildAbort(f"cannot stage the pystray license texts: {e!r}") from e
    return DIST_ROOT


# ---------------------------------------------------------------------------------------- 4
def nvidia_files(dist_root: Path) -> "list[Path]":
    base = dist_root / "_internal" / "nvidia"
    return sorted(base.rglob("*.dll")) if base.is_dir() else []


def step_nvidia(variant: str, dist_root: Path) -> dict:
    step(4, "NVIDIA files: Authenticode, never modified")
    files = nvidia_files(dist_root)
    if variant == "cpu":
        if files:
            raise BuildAbort(f"the CPU bundle contains NVIDIA files: {[f.name for f in files][:5]}")
        return {"state": "cpu-none"}
    signed, unsigned = [], []
    for f in files:
        sig = authenticode(f)
        ok = sig.get("Status") == "Valid" and NVIDIA_SUBJECT in (sig.get("Subject") or "")
        (signed if ok else unsigned).append(f.name)
        log(f"  {f.name:44s} {sig.get('Status'):12s} {sig.get('Subject', '')[:40]}")
    if unsigned:
        if azure_ready():
            raise BuildAbort("unsigned NVIDIA files need the catalog (.cat) path of act 9b R17, "
                             "which is designed and verified in a VM in 9f: " + ", ".join(unsigned))
        loud(f"GPU VARIANT: {len(unsigned)} NVIDIA DLL(s) are NOT signed by NVIDIA in the source "
             f"packages ({', '.join(unsigned)}). They are shipped byte-for-byte (never re-signed); "
             "the catalog signature that would cover them needs signing credentials -- SKIPPED. "
             "The GPU installer is not releasable until 9f.")
    return {"state": "checked", "signed_by_nvidia": signed, "unsigned": unsigned}


# ---------------------------------------------------------------------------------------- 5
def step_sign_bundle(dist_root: Path) -> dict:
    step(5, "sign our unsigned PE files (never NVIDIA's, never an already-signed file)")
    nvidia = {f.resolve() for f in nvidia_files(dist_root)}
    todo = []
    for f in sorted(dist_root.rglob("*")):
        if f.suffix.lower() not in (".exe", ".dll", ".pyd") or f.resolve() in nvidia:
            continue
        if f.name == CP_DLL_NAME:
            continue                              # signed on its own step
        todo.append(f)
    if not azure_ready():
        return azure_sign(todo, "bundle PE files")   # loud skip, no per-file PowerShell run
    unsigned = [f for f in todo if authenticode(f).get("Status") == "NotSigned"]
    return azure_sign(unsigned, "bundle PE files")


# ---------------------------------------------------------------------------------------- 6
def bundle_manifest(dist_root: Path) -> dict:
    h = hashlib.sha256()
    files = total = 0
    for p in sorted(dist_root.rglob("*"), key=lambda q: q.relative_to(dist_root).as_posix().lower()):
        if not p.is_file():
            continue
        rel = p.relative_to(dist_root).as_posix()
        size = p.stat().st_size
        h.update(f"{rel}\t{size}\t{sha256_file(p)}\n".encode("utf-8"))
        files += 1
        total += size
    return {"sha256": h.hexdigest(), "files": files, "bytes": total}


def _git(*args: str) -> "str | None":
    try:
        return subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _gate_run(label: str, cmd: list) -> None:
    log(f"gate: {label}")
    rc = subprocess.call([str(c) for c in cmd], cwd=str(REPO_ROOT), env=child_env())
    if rc != 0:
        raise BuildAbort(f"GATE FAILED: {label} exited {rc} -- read the FAIL lines above.")


def gate_no_models(dist_root: Path) -> None:
    """R7 / F-259: no file of the buffalo_l pack (by name, or by size + SHA-256) in the bundle."""
    names = set(MODEL_SHA256)
    sizes = {size for size, _d in BUFFALO_FILES.values()}
    found = []
    for p in dist_root.rglob("*"):
        if p.is_dir() and p.name.lower() in ("buffalo_l", "insightface_home"):
            found.append(str(p.relative_to(dist_root)))
        elif p.is_file() and (p.name in names or (p.stat().st_size in sizes
                                                   and sha256_file(p) in MODEL_SHA256.values())):
            found.append(str(p.relative_to(dist_root)))
    if found:
        raise BuildAbort("GATE FAILED: recognition models are inside the bundle ("
                         + ", ".join(found[:5]) + ") -- decision 9-02: the installer downloads them.")


def gate_variant(variant: str, dist_root: Path) -> None:
    """F-219: the bundle IS the variant it is named after."""
    internal = dist_root / "_internal"
    cuda = list(internal.rglob("onnxruntime_providers_cuda*.dll"))
    trt = list(internal.rglob("onnxruntime_providers_tensorrt*.dll"))
    nv = nvidia_files(dist_root)
    if trt:
        raise BuildAbort("GATE FAILED: the TensorRT provider ships without a TensorRT runtime (F-51)")
    if variant == "cpu" and (cuda or nv):
        raise BuildAbort("GATE FAILED: the CPU variant contains the CUDA provider or NVIDIA files")
    if variant == "gpu":
        if not cuda or not nv:
            raise BuildAbort("GATE FAILED: the GPU variant lacks the CUDA provider or NVIDIA files")
        spec = (INSTALLER_DIR / "windows_face_unlock.spec").read_text(encoding="utf-8")
        allowed = {n for n in __import__("re").findall(r'"([\w.-]+\.dll)"', spec.split("NVIDIA_ALLOWLIST", 1)[1].split("}", 1)[0])}
        extra = sorted(f.name for f in nv if f.name not in allowed)
        if extra:
            raise BuildAbort(f"GATE FAILED: NVIDIA files outside the allowlist: {extra}")
    if list(internal.rglob("opencv_videoio_ffmpeg*.dll")):
        raise BuildAbort("GATE FAILED: the FFmpeg plugin ships (D-151)")
    pyst = internal / "pystray"
    if not pyst.is_dir() or not list(pyst.glob("*.py")):
        raise BuildAbort("GATE FAILED: pystray is not shipped as replaceable .py sources (F-261)")
    log(f"gate: bundle shape matches the {variant.upper()} variant")


def gate_frozen_custody(dist_root: Path) -> dict:
    """The frozen service heals a seeded directory (clean -> 0) and refuses a junction (-> 1).
    Runs only because FU_BUILD_GATE=1 is set (D-73)."""
    import tempfile
    exe = dist_root / "face_service.exe"
    if not exe.is_file():
        raise BuildAbort(f"GATE FAILED: {exe} missing")
    summary: dict = {}
    with tempfile.TemporaryDirectory(prefix="fu_gate_custody_") as tmp:
        td = Path(tmp)
        victim = td / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_bytes(b"k")
        for case in ("clean", "junction"):
            home = td / case
            (home / "enroll").mkdir(parents=True)
            (home / "credentials.bin").write_bytes(os.urandom(96))
            (home / "pipe_entropy.bin").write_bytes(os.urandom(32))
            subprocess.run(["icacls", str(home), "/grant", "*S-1-5-32-545:(OI)(CI)(M)"],
                           capture_output=True, check=True)
            if case == "junction":
                subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "enroll" / "link"), str(victim)],
                               capture_output=True, check=True)
            out = td / f"{case}.json"
            try:
                proc = subprocess.run([str(exe), "--selfcheck-custody", str(home), "--out", str(out)],
                                      env=child_env(FU_BUILD_GATE="1"), capture_output=True, timeout=180)
            except subprocess.TimeoutExpired as e:
                raise BuildAbort(f"GATE FAILED: the custody self-check hung ({case})") from e
            try:
                data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
            except ValueError:
                data = {}
            summary[case] = {"rc": proc.returncode, "ok": data.get("ok"),
                             "foreign": data.get("foreign_ace_problems"),
                             "secrets": {k: v.get("self_system_only")
                                         for k, v in (data.get("secrets") or {}).items()},
                             "frozen": data.get("frozen"), "exception": data.get("exception")}
            log(f"gate: frozen custody [{case}] rc={proc.returncode} ok={data.get('ok')}")
        clean, junc = summary["clean"], summary["junction"]
        bad = []
        if clean["rc"] != 0 or clean["ok"] is not True or clean["foreign"] != 0:
            bad.append(f"clean case {clean}")
        if sorted(clean["secrets"]) != ["credentials.bin", "pipe_entropy.bin"] or not all(clean["secrets"].values()):
            bad.append(f"clean case secrets {clean['secrets']}")
        if clean["frozen"] is not True:
            bad.append("the self-check did not run frozen")
        if junc["rc"] != 1 or junc["ok"] is not False:
            bad.append(f"junction case {junc}")
        if not (victim / "keep.txt").exists():
            bad.append("the junction target was modified")
        if bad:
            raise BuildAbort("GATE FAILED: frozen custody self-check -> " + "; ".join(bad))
    return summary


def step_gate(variant: str, dist_root: Path, signing: dict) -> dict:
    step(6, "GATE (on the signed bundle) + stamp")
    if not dist_root.is_dir():
        raise BuildAbort(f"no bundle at {dist_root}; run --half 1 first")
    if GATE_STAMP.exists():
        GATE_STAMP.unlink()
    _gate_run("tools/verify_frozen_entrypoints.py",
              [sys.executable, TOOLS_DIR / "verify_frozen_entrypoints.py", "--dist", dist_root])
    _gate_run("tools/packaging_selftest.py", [sys.executable, "-m", "tools.packaging_selftest"])
    custody = gate_frozen_custody(dist_root)
    gate_no_models(dist_root)
    gate_variant(variant, dist_root)
    staged = dist_root / "credential_provider" / CP_DLL_NAME
    cp = {"sha256": sha256_file(staged) if staged.is_file() else None}
    if not cp_skipped() and not staged.is_file():
        raise BuildAbort(f"GATE FAILED: {staged} missing")
    manifest = bundle_manifest(dist_root)
    stamp = {
        "schema": GATE_STAMP_SCHEMA, "variant": variant, "version": VERSION,
        "gated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dist": str(dist_root.resolve()), "manifest": manifest,
        "installer_iss_sha256": sha256_file(ISS), "cp": cp, "signing": signing,
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty_paths": len((_git("status", "--porcelain") or "").splitlines()),
        "python": sys.version.split()[0],
        "frozen_custody": custody,
    }
    GATE_STAMP.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    log(f"GATE PASSED: {manifest['files']} files, {manifest['bytes'] / 2**30:.2f} GiB, "
        f"manifest {manifest['sha256'][:16]}...")
    return stamp


def check_gate_stamp(variant: str, dist_root: Path) -> dict:
    if not GATE_STAMP.is_file():
        raise BuildAbort("no gate stamp -- run --half 1 (or --gate-only) first")
    try:
        stamp = json.loads(GATE_STAMP.read_text(encoding="utf-8"))
    except ValueError as e:
        raise BuildAbort(f"gate stamp unreadable ({e})") from e
    if stamp.get("schema") != GATE_STAMP_SCHEMA or stamp.get("variant") != variant:
        raise BuildAbort(f"the gate stamp is for {stamp.get('variant')} / schema {stamp.get('schema')}")
    if stamp.get("installer_iss_sha256") != sha256_file(ISS):
        raise BuildAbort("installer.iss changed after the gate -- re-run --gate-only")
    now = bundle_manifest(dist_root)
    if now != stamp.get("manifest"):
        raise BuildAbort("dist changed after the gate -- re-run --gate-only")
    return stamp


# ---------------------------------------------------------------------------------------- 7
def step_inno(variant: str, dist_root: Path, iscc: str) -> Path:
    step(7, "Inno Setup")
    stamp = check_gate_stamp(variant, dist_root)
    defines = iscc_defines(variant)
    if azure_ready():
        signtool = (f'"{os.environ["SIGNTOOL"]}" sign /fd SHA256 /tr http://timestamp.acs.microsoft.com '
                    f'/td SHA256 /dlib "{os.environ["AZURE_CODESIGN_DLIB"]}" '
                    f'/dmdf "{BUILD_DIR / "azure-codesign.json"}" $f')
        defines += ["/DSignToolName=azure", f"/Sazure={signtool}"]
    else:
        loud("SETUP AND UNINSTALLER SIGNING SKIPPED: no Azure Trusted Signing credentials "
             f"(missing: {', '.join(azure_missing())}).")
    run([iscc, *defines, ISS], cwd=REPO_ROOT)
    out = OUTPUT_DIR / artefact_name(variant)
    if not out.is_file():
        raise BuildAbort(f"ISCC finished but {out} is not there")
    if bundle_manifest(dist_root) != stamp["manifest"]:              # F-225
        raise BuildAbort("dist changed while ISCC ran -- the installer packs an ungated bundle")
    return out


# ---------------------------------------------------------------------------------------- 8
def step_checksums(installer_path: Path, variant: str) -> None:
    step(8, "checksums + build info")
    digest = sha256_file(installer_path)
    (installer_path.parent / f"{installer_path.name}.sha256").write_text(
        f"{digest}  {installer_path.name}\n", encoding="ascii")
    stamp = json.loads(GATE_STAMP.read_text(encoding="utf-8"))
    info = {"installer": installer_path.name, "sha256": digest, "bytes": installer_path.stat().st_size,
            "variant": variant, "version": VERSION, "manifest": stamp["manifest"],
            "git_head": stamp.get("git_head"), "signing": stamp.get("signing"),
            "cp": stamp.get("cp")}
    (installer_path.parent / f"{installer_path.name}.buildinfo.json").write_text(
        json.dumps(info, indent=2) + "\n", encoding="utf-8")
    log(f"sha256 = {digest}")


# ---------------------------------------------------------------------------------------- main
def parse_args(argv):
    ap = argparse.ArgumentParser(description="Build the Windows Face Unlock installer.")
    ap.add_argument("--variant", choices=("cpu", "gpu"), default="cpu")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--half", type=int, choices=(1, 2))
    mode.add_argument("--gate-only", action="store_true")
    mode.add_argument("--resign", action="store_true",
                      help="on an existing dist (CI sign job): sign the staged CP DLL and our PE "
                           "files, check NVIDIA's, re-run the gate")
    return ap.parse_args(argv)


def _run(args) -> int:
    variant = args.variant
    tools = step_preflight(variant, need_cp=not cp_skipped() and args.half != 2 and not args.gate_only
                           and not args.resign)
    if args.half == 2:
        out = step_inno(variant, DIST_ROOT, tools["iscc"])
        step_checksums(out, variant)
        log(f"Installer ready: {out}")
        return 0
    if args.resign:
        if not DIST_ROOT.is_dir():
            raise BuildAbort(f"no bundle at {DIST_ROOT} to sign")
        staged = DIST_ROOT / "credential_provider" / CP_DLL_NAME
        signing = {"cp": step_sign_cp(staged if staged.is_file() else None)}
        signing["nvidia"] = step_nvidia(variant, DIST_ROOT)
        signing["bundle"] = step_sign_bundle(DIST_ROOT)
        step_gate(variant, DIST_ROOT, signing)
        return 0
    if args.gate_only:
        prev = {}
        if GATE_STAMP.is_file():
            try:
                prev = json.loads(GATE_STAMP.read_text(encoding="utf-8")).get("signing") or {}
            except ValueError:
                prev = {}
        step_gate(variant, DIST_ROOT, prev)
        return 0
    cp_dll = step_build_cp()
    signing = {"cp": step_sign_cp(cp_dll)}
    dist_root = step_pyinstaller(variant, cp_dll)
    signing["nvidia"] = step_nvidia(variant, dist_root)
    signing["bundle"] = step_sign_bundle(dist_root)
    step_gate(variant, dist_root, signing)
    if args.half == 1:
        log(f"half 1 done: {dist_root} staged, signed as far as possible, gated. Next: the operator "
            f"dist-smoke, then python installer\\build.py --variant {variant} --half 2")
        return 0
    out = step_inno(variant, dist_root, tools["iscc"])
    step_checksums(out, variant)
    log(f"Installer ready: {out}")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return _run(args)
    except BuildAbort as exc:
        log(f"BUILD ABORTED: {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        log(f"BUILD ABORTED: command failed with exit code {exc.returncode}: "
            f"{' '.join(str(c) for c in exc.cmd)}")
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:     # F-221: no raw traceback
        log(f"BUILD ABORTED: {exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
