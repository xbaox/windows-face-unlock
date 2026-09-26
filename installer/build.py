"""End-to-end installer build.

Runs these steps in order (numbered 0-7):
  0. check the buffalo_l recognition pack: all five files present, SHA-256 pinned
  1. build the Credential Provider DLL via CMake (Release x64) into build-cp/
  2. Authenticode-sign the CP DLL with the pinned certificate (SIGN_CP) and verify
     the signer thumbprint
  3. run PyInstaller on installer/windows_face_unlock.spec
  4. stage the CP DLL, the task registrar and the docs into the dist folder
  5. THE GATE: tools/verify_frozen_entrypoints.py against the staged bundle,
     tools/packaging_selftest.py, model hashes and the CP signature of the staged
     copy; on success writes a gate stamp next to the bundle
  6. compile installer/installer.iss with Inno Setup -- refused unless the gate
     stamp matches the bundle byte for byte
  7. emit SHA-256 checksums next to the installer

Two official halves, so the operator dist-smoke (installer/README.md, "The gate")
can sit between the automated gate and ISCC:

    python installer\\build.py --half 1    steps 0-5: stage + gate, writes the stamp
    (operator dist-smoke out of dist\\WindowsFaceUnlock)
    python installer\\build.py --half 2    steps 6-7: Inno Setup + checksums

With no --half the whole 0-7 chain runs, gate included. Also:

    --gate-only       re-run step 5 on the existing dist (e.g. after a smoke) and
                      re-stamp it; nothing is rebuilt
    --check-models    run step 0 only (CI calls this right after fetching the pack)
    --allow-unsigned-cp
                      build without SIGN_CP. See step_sign_cp for why this is an
                      explicit opt-out rather than the default.

Intended to run both locally and in CI. Environment:
    SIGN_CP         -- 40-hex thumbprint of the code-signing certificate for the
                       CP DLL (CurrentUser\\My or LocalMachine\\My). Required unless
                       --allow-unsigned-cp or SKIP_CP=1.
    INNO_SETUP_ISCC -- full path to ISCC.exe (default: search known locations/PATH)
    SKIP_CP         -- set to 1 to build a presence-auto-lock-only installer
                       with no Credential Provider. This is the ONLY way to
                       skip the DLL; a failing CP build aborts the run.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
import re
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
DIST_ROOT = DIST_DIR / "WindowsFaceUnlock"          # installer.iss BuildRoot
BUILD_DIR = REPO_ROOT / "build"                     # PyInstaller work dir, unrelated to CP
OUTPUT_DIR = REPO_ROOT / "installer_output"
ISS = INSTALLER_DIR / "installer.iss"
CP_DLL_NAME = "FaceCredentialProvider.dll"

# Written by the gate, read by Inno Setup's step. Lives NEXT TO the bundle, not in it:
# installer.iss packs dist\WindowsFaceUnlock\*, and PyInstaller's step wipes dist\,
# so a fresh build can never inherit a stale stamp.
GATE_STAMP = DIST_DIR / "WindowsFaceUnlock.gate.json"
GATE_STAMP_SCHEMA = 1

TOTAL_STEPS = 7

# Stage 9 (act 9b R7): the pins live in face_service/model_pins.py -- the ONE source that build.py,
# the installer generator and the service all read (F-38: a substituted model is a wrong-party
# unlock). This file keeps no SHA literal of its own.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from face_service.model_pins import BUFFALO_FILES, BUFFALO_ZIP_SHA256  # noqa: E402

MODEL_SHA256 = {name: digest for name, (_size, digest) in BUFFALO_FILES.items()}
MODELS_PACK = Path.home() / ".insightface" / "models" / "buffalo_l"


class BuildAbort(RuntimeError):
    """A refusal with a message meant for the operator, not a stack trace."""


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def step(n: int, msg: str) -> None:
    log(f"step {n}/{TOTAL_STEPS} -- {msg}")


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    log("$ " + " ".join(str(c) for c in cmd))
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None, env=env)


def python_exe() -> str:
    return sys.executable


def gate_no_models(dist_root: Path) -> None:
    """Stage 9 (act 9b R7, F-259): buffalo_l is not redistributed -- the installer downloads it.
    Fail the build if ANY file of the pack (by name, or by size + SHA-256 under any name) or any
    directory named buffalo_l / insightface_home is inside the bundle."""
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
                         + ", ".join(found[:5]) + ") -> decision 9-02: the installer downloads "
                         "them; nothing may redistribute them -> refusing.")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- 0
def verify_model_pack(pack: Path, where: str) -> None:
    missing = [n for n in MODEL_SHA256 if not (pack / n).is_file()]
    if missing:
        raise BuildAbort(
            f"buffalo_l is incomplete in {pack} ({where}): missing {', '.join(missing)}.\n"
            "The installer ships the recognition models, so this build machine must have a "
            "complete pack. Populate it (run the service once with an internet connection, or "
            "copy the five .onnx files there) and re-run."
        )
    bad = []
    for name, want in MODEL_SHA256.items():
        got = sha256_file(pack / name)
        if got != want:
            bad.append(f"{name}: sha256 {got} != pinned {want}")
    if bad:
        raise BuildAbort(
            f"buffalo_l in {pack} ({where}) does not match the pinned SHA-256 -> the installer "
            "would ship recognition models nobody reviewed (a substituted model can match the "
            "wrong face) -> refusing. Mismatches:\n  " + "\n  ".join(bad) + "\n"
            "Fix: restore the pack from the pinned source; the pins are in face_service/model_pins.py "
            "only as a deliberate, reviewed model change."
        )


def step_check_models() -> Path:
    """Fail before PyInstaller if the buffalo_l pack is incomplete or not the pinned one.

    The spec bundles the five recognition models, so the build machine's own
    %USERPROFILE%\\.insightface IS the source of what ships. Checking here rather
    than only inside the spec buys a readable failure at the top of the run
    instead of a stack trace 40 minutes in, and keeps the reason in the build log.

    An installer built without these produces exactly the gap Stage 7d exists to
    close: a machine that cannot sign in offline and silently retries a ~290 MB
    download instead of saying so. An installer built with DIFFERENT ones ships
    unreviewed recognition models (8a F-38), hence the SHA-256 pins.
    """
    step(0, "check the buffalo_l recognition pack (presence + SHA-256)")
    verify_model_pack(MODELS_PACK, "build machine")
    zip_path = MODELS_PACK.parent / "buffalo_l.zip"
    if zip_path.is_file():
        got = sha256_file(zip_path)
        if got != BUFFALO_ZIP_SHA256:
            raise BuildAbort(
                f"{zip_path} sha256 {got} != pinned {BUFFALO_ZIP_SHA256} -> the pack next to it "
                "came from an archive nobody reviewed -> refusing. Fix: delete both and re-fetch "
                "from the pinned source."
            )
        log("buffalo_l.zip matches the pinned SHA-256")
    total = sum((MODELS_PACK / n).stat().st_size for n in MODEL_SHA256)
    log(f"buffalo_l complete and pinned: {len(MODEL_SHA256)} files, {total / (1024 * 1024):.0f} MiB")
    return MODELS_PACK


# --------------------------------------------------------------------------- 1
def cp_skipped() -> bool:
    return os.environ.get("SKIP_CP") == "1"


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
    if cp_skipped():
        step(1, "skipping Credential Provider DLL (SKIP_CP=1)")
        return None
    step(1, "build Credential Provider DLL")
    run(["cmake", "-S", CP_DIR.name, "-B", CP_BUILD_DIR.name, "-A", "x64",
         "-G", "Visual Studio 17 2022"], cwd=REPO_ROOT)
    run(["cmake", "--build", CP_BUILD_DIR.name, "--config", "Release"], cwd=REPO_ROOT)
    dll = CP_BUILD_DIR / "Release" / CP_DLL_NAME
    if not dll.exists():
        raise BuildAbort(
            f"cmake reported success but {dll} is missing. Set SKIP_CP=1 to build "
            "a presence-auto-lock-only installer on purpose."
        )
    return dll


# --------------------------------------------------------------------------- 2
def resolve_sign_policy(allow_unsigned: bool) -> dict:
    """Decide, BEFORE anything is built, how the CP DLL will be signed.

    Defect (8a F-13): signing was a silent no-op without SIGN_CP -> the 7l rebuild
    shipped an unsigned DLL although 7d-L5 had signed one -> nobody noticed until the
    audit. Fix: SIGN_CP=<thumbprint> is REQUIRED for a build with a CP; building
    without it takes the explicit --allow-unsigned-cp and says so loudly in the log
    and in the gate stamp. A signature does not decide whether LogonUI loads the tile
    (credential_provider/SIGNING.md), which is why an opt-out exists at all -- CI has
    no certificate -- but an unsigned build must be a decision, never an accident.

    SIGN_CP=self is refused: a certificate minted during the build has no thumbprint
    anyone could have pinned, so "is this the signer we meant" is unanswerable. Create
    the development certificate once with tools\\sign_cp.ps1 -SelfSigned and pass its
    thumbprint.
    """
    if cp_skipped():
        return {"mode": "skip_cp", "thumbprint": None}
    raw = os.environ.get("SIGN_CP", "")
    tp = raw.replace(" ", "").strip()
    if not tp:
        if allow_unsigned:
            log("WARNING: SIGN_CP is not set and --allow-unsigned-cp was given -> the CP DLL "
                "ships UNSIGNED (recorded in the gate stamp).")
            return {"mode": "unsigned-allowed", "thumbprint": None}
        raise BuildAbort(
            "SIGN_CP is not set -> the CP DLL would ship unsigned (8a F-13: the 7l rebuild did "
            "exactly this, silently) -> refusing.\n"
            "Fix: set SIGN_CP to the 40-hex thumbprint of the code-signing certificate, e.g.\n"
            "    $env:SIGN_CP = '13F9BB6228DDD5B039628D1B0CEF1B598E46649C'\n"
            "or pass --allow-unsigned-cp to build unsigned on purpose (CI does; see "
            "credential_provider/SIGNING.md)."
        )
    if tp.lower() == "self":
        raise BuildAbort(
            "SIGN_CP=self is no longer accepted -> a certificate created during the build has "
            "no pinned thumbprint, so the signer cannot be verified -> refusing.\n"
            "Fix: create it once with tools\\sign_cp.ps1 -SelfSigned, then set SIGN_CP to the "
            "thumbprint it prints."
        )
    if allow_unsigned:
        raise BuildAbort("SIGN_CP is set AND --allow-unsigned-cp was given; pick one.")
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", tp):
        raise BuildAbort(f"SIGN_CP={raw!r} is not a 40-hex certificate thumbprint.")
    return {"mode": "pinned", "thumbprint": tp.upper()}


_SIG_PS = (
    "$ErrorActionPreference = 'Stop'; "
    "$s = Get-AuthenticodeSignature -LiteralPath $env:FU_SIG_PATH; "
    "$c = $s.SignerCertificate; "
    "[pscustomobject]@{ "
    "Status = [string]$s.Status; "
    "Type = [string]$s.SignatureType; "
    "Thumbprint = $(if ($c) { $c.Thumbprint } else { '' }); "
    "Subject = $(if ($c) { $c.Subject } else { '' }); "
    "Message = [string]$s.StatusMessage "
    "} | ConvertTo-Json -Compress"
)


def authenticode(path: Path) -> dict:
    """Get-AuthenticodeSignature, as data. The path travels in the environment, not
    in the command line, so no quoting of spaces or quotes is ever involved."""
    env = dict(os.environ, FU_SIG_PATH=str(path))
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", _SIG_PS],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        raise BuildAbort(f"Get-AuthenticodeSignature failed on {path}: {out.stderr.strip()}")
    return json.loads(out.stdout)


def verify_cp_signature(dll: Path, thumbprint: str) -> dict:
    """The DLL carries an embedded Authenticode signature by exactly `thumbprint`.

    Status 'Valid' and 'UnknownError' are both accepted: the latter is what a
    development certificate whose root is not in this machine's trust store reports
    (sign_cp.ps1 does not install trust unless asked). The PINNED THUMBPRINT is the
    identity check, not the chain. NotSigned, HashMismatch (file altered after
    signing), NotTrusted (explicitly distrusted) and anything else abort.
    """
    sig = authenticode(dll)
    got = (sig.get("Thumbprint") or "").upper()
    status = sig.get("Status")
    if status not in ("Valid", "UnknownError"):
        raise BuildAbort(
            f"{dll}: Authenticode status {status} ({sig.get('Message')}) -> the CP DLL is not "
            f"signed as required by SIGN_CP={thumbprint} -> refusing."
        )
    if sig.get("Type") and sig["Type"] != "Authenticode":
        raise BuildAbort(
            f"{dll}: signature type {sig['Type']} -> a catalog signature does not travel with "
            "the file -> refusing. Fix: embed the signature (tools\\sign_cp.ps1)."
        )
    if got != thumbprint.upper():
        raise BuildAbort(
            f"{dll}: signed by {got or '(nobody)'} ({sig.get('Subject')}), expected "
            f"SIGN_CP={thumbprint} -> the shipped DLL would carry a signer nobody pinned -> "
            "refusing."
        )
    if status == "UnknownError":
        log(f"note: signature chain is not trusted on this machine ({sig.get('Message')}); "
            "accepted because the signer thumbprint is pinned")
    log(f"CP DLL signer verified: {got} ({sig.get('Subject')}), status {status}")
    return sig


def step_sign_cp(dll: Path | None, policy: dict) -> None:
    """Authenticode-sign the CP DLL with the pinned certificate, then VERIFY it.

    Runs before step_stage so the SIGNED file is the one that reaches dist/, and the
    gate re-verifies the staged copy. See resolve_sign_policy for why SIGN_CP is
    required, and credential_provider/SIGNING.md -- including the part where a
    signature is NOT what makes the lock-screen tile appear.
    """
    if policy["mode"] == "skip_cp" or dll is None:
        step(2, "sign the CP DLL -- skipped (no CP in this build)")
        return
    if policy["mode"] == "unsigned-allowed":
        step(2, "sign the CP DLL -- SKIPPED by --allow-unsigned-cp; the DLL ships UNSIGNED")
        return
    tp = policy["thumbprint"]
    step(2, f"sign the CP DLL with {tp} and verify the signer")
    script = TOOLS_DIR / "sign_cp.ps1"
    run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-DllPath", str(dll), "-Thumbprint", tp])
    verify_cp_signature(dll, tp)


# --------------------------------------------------------------------------- 3
def step_pyinstaller() -> Path:
    step(3, "PyInstaller")
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    run([
        python_exe(), "-m", "PyInstaller",
        "--noconfirm", "--clean",
        str(INSTALLER_DIR / "windows_face_unlock.spec"),
    ], cwd=REPO_ROOT)
    out = DIST_ROOT
    if not out.exists():
        raise BuildAbort(f"expected PyInstaller output at {out}")
    return out


# --------------------------------------------------------------------------- 4
def step_stage(dist_root: Path, cp_dll: Path | None) -> None:
    step(4, "stage CP DLL + task registrar + docs into dist")
    if cp_dll and cp_dll.exists():
        dest = dist_root / "credential_provider"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cp_dll, dest / CP_DLL_NAME)
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
            raise BuildAbort(f"cannot stage the task registrar: {src} is missing")
        shutil.copy2(src, post / name)

    for doc in ("README.md", "LICENSE", "INSTALL.md"):
        p = REPO_ROOT / doc
        if p.exists():
            shutil.copy2(p, dist_root / doc)


# --------------------------------------------------------------------------- 5
def bundle_manifest(dist_root: Path) -> dict:
    """Content digest of everything ISCC will pack: sorted (path, size, sha256) lines."""
    h = hashlib.sha256()
    files = 0
    total = 0
    for p in sorted(dist_root.rglob("*"), key=lambda q: q.relative_to(dist_root).as_posix().lower()):
        if not p.is_file():
            continue
        rel = p.relative_to(dist_root).as_posix()
        size = p.stat().st_size
        h.update(f"{rel}\t{size}\t{sha256_file(p)}\n".encode("utf-8"))
        files += 1
        total += size
    return {"sha256": h.hexdigest(), "files": files, "bytes": total}


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=str(REPO_ROOT), capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _gate_run(label: str, cmd: list[str]) -> None:
    log(f"gate: {label}")
    log("$ " + " ".join(str(c) for c in cmd))
    rc = subprocess.call(cmd, cwd=str(REPO_ROOT))
    if rc != 0:
        raise BuildAbort(
            f"GATE FAILED: {label} exited {rc} -> the bundle in dist\\ is not proven to run "
            "(KNOWN_ISSUES #5 class) -> refusing to compile an installer around it. Read the "
            "FAIL lines above, fix the spec/build, rebuild (--half 1)."
        )


def gate_frozen_custody(dist_root: Path) -> dict:
    """Live half of the gate for the data-directory custody (Stage 8b-2).

    Defect: the 8b bundle passed every static check, yet the FROZEN service could not heal the
    data directory at all (pywin32 lazily imported win32timezone, which the bundle lacked).
    Consequence: it surfaced only in a manual dist smoke, one step before the installer. Fix: run
    the real code path out of the BUILT exe -- face_service.exe --selfcheck-custody -- on two seeded
    scratch directories, before the stamp:
      clean    -- BUILTIN\\Users:(OI)(CI)(M) + fake credentials.bin / pipe_entropy.bin (random
                  bytes): exit 0, ok, 0 foreign ACEs, both secrets protected SELF + SYSTEM;
      junction -- the same plus a junction inside the tree: exit 1, ok false.
    FACE_UNLOCK_HOME points at a directory that must still not exist afterwards: the mode may act
    on its argument only. Any deviation aborts the build.
    """
    import tempfile

    exe = dist_root / "face_service.exe"
    if not exe.is_file():
        raise BuildAbort(f"GATE FAILED: {exe} missing -> cannot run the custody self-check.")
    summary: dict = {}
    with tempfile.TemporaryDirectory(prefix="fu_gate_custody_") as tmp:
        td = Path(tmp)
        never = td / "app-dir-must-not-appear"
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
                subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "enroll" / "link"),
                                str(victim)], capture_output=True, check=True)
            out = td / f"{case}.json"
            env = dict(os.environ, FACE_UNLOCK_HOME=str(never))
            proc = subprocess.run([str(exe), "--selfcheck-custody", str(home), "--out", str(out)],
                                  env=env, capture_output=True, timeout=180)
            data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
            summary[case] = {"rc": proc.returncode, "ok": data.get("ok"),
                             "heal": data.get("heal"), "foreign": data.get("foreign_ace_problems"),
                             "secrets": {k: v.get("self_system_only")
                                         for k, v in (data.get("secrets") or {}).items()},
                             "frozen": data.get("frozen"), "exception": data.get("exception")}
            log(f"gate: frozen custody [{case}] rc={proc.returncode} ok={data.get('ok')} "
                f"heal={(data.get('heal') or {}).get('ok')} "
                f"aces_removed={(data.get('heal') or {}).get('aces_removed')} "
                f"relocked={(data.get('heal') or {}).get('relocked')} "
                f"foreign={data.get('foreign_ace_problems')} "
                f"exception={data.get('exception')}")
        clean, junc = summary["clean"], summary["junction"]
        bad = []
        if clean["rc"] != 0 or clean["ok"] is not True:
            bad.append(f"clean case rc={clean['rc']} ok={clean['ok']} ({clean['exception'] or clean['heal']})")
        if clean["foreign"] != 0:
            bad.append(f"clean case left {clean['foreign']} foreign-ACE problem(s)")
        if sorted(clean["secrets"]) != ["credentials.bin", "pipe_entropy.bin"] or \
                not all(clean["secrets"].values()):
            bad.append(f"clean case secrets not SELF+SYSTEM protected: {clean['secrets']}")
        if clean["frozen"] is not True:
            bad.append("the self-check did not run frozen")
        if junc["rc"] != 1 or junc["ok"] is not False:
            bad.append(f"junction case rc={junc['rc']} ok={junc['ok']} (expected 1 / false)")
        if never.exists():
            bad.append(f"the self-check created {never} -- it must act on its argument only")
        if not (victim / "keep.txt").exists():
            bad.append("the junction target was modified")
        if bad:
            raise BuildAbort("GATE FAILED: frozen custody self-check -> " + "; ".join(bad))
    log("gate: frozen custody self-check passed (clean -> healed and locked; junction -> refused)")
    return summary


def step_gate(dist_root: Path, cp_dll: Path | None, policy: dict) -> dict:
    """The automated half of the gate, then the stamp that step 6 insists on.

    Defect (8a F-08 / D-09): the build went PyInstaller -> stage -> ISCC and never ran
    the gate the README demanded -> a CI release could ship a blind bundle straight to
    the updater. Fix: this step, which aborts the build on the first failure.
    """
    step(5, "GATE -- verify_frozen_entrypoints + packaging_selftest + models + CP signature")
    if not dist_root.is_dir():
        raise BuildAbort(f"no bundle at {dist_root}; run --half 1 first")
    if GATE_STAMP.exists():
        GATE_STAMP.unlink()          # a failed gate must never leave an old stamp behind

    _gate_run("tools/verify_frozen_entrypoints.py",
              [python_exe(), str(TOOLS_DIR / "verify_frozen_entrypoints.py"), "--dist", str(dist_root)])
    _gate_run("tools/packaging_selftest.py", [python_exe(), "-m", "tools.packaging_selftest"])
    custody = gate_frozen_custody(dist_root)

    log("gate: no recognition model in the bundle (decision 9-02, act 9b R7)")
    gate_no_models(dist_root)
    log("no buffalo_l file anywhere in the bundle")

    staged = dist_root / "credential_provider" / CP_DLL_NAME
    cp = {"mode": policy["mode"], "thumbprint": policy["thumbprint"], "sha256": None}
    if policy["mode"] == "skip_cp":
        log("gate: no CP in this build (SKIP_CP=1)")
    else:
        if not staged.is_file():
            raise BuildAbort(f"GATE FAILED: {staged} missing -> the installer would have no "
                             "face tile -> refusing.")
        cp["sha256"] = sha256_file(staged)
        if cp_dll is not None and cp_dll.is_file() and sha256_file(cp_dll) != cp["sha256"]:
            raise BuildAbort(f"GATE FAILED: staged {staged} differs from {cp_dll} -> the "
                             "installer would ship a DLL other than the one built and signed "
                             "-> refusing. Re-run --half 1.")
        if policy["mode"] == "pinned":
            log("gate: signer of the STAGED CP DLL")
            verify_cp_signature(staged, policy["thumbprint"])
        else:
            sig = authenticode(staged)
            log(f"WARNING: staged CP DLL signature status {sig.get('Status')} "
                "(--allow-unsigned-cp)")

    log("gate: hashing the bundle for the stamp")
    manifest = bundle_manifest(dist_root)
    stamp = {
        "schema": GATE_STAMP_SCHEMA,
        "gated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "dist": str(dist_root.resolve()),
        "manifest": manifest,
        "installer_iss_sha256": sha256_file(ISS),
        "cp": cp,
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty_paths": len((_git("status", "--porcelain") or "").splitlines()),
        "python": sys.version.split()[0],
        "checks": ["verify_frozen_entrypoints rc=0", "packaging_selftest rc=0",
                   "frozen custody self-check clean=0 junction=1",
                   "buffalo_l sha256 pinned", f"cp {policy['mode']}"],
        "frozen_custody": custody,
    }
    GATE_STAMP.write_text(json.dumps(stamp, indent=2) + "\n", encoding="utf-8")
    log(f"GATE PASSED: {manifest['files']} files, {manifest['bytes'] / 2**30:.2f} GiB, "
        f"manifest {manifest['sha256'][:16]}... -> {GATE_STAMP}")
    return stamp


def check_gate_stamp(dist_root: Path) -> dict:
    """Step 6's precondition: the bundle ISCC is about to pack is the one the gate passed."""
    if not GATE_STAMP.is_file():
        raise BuildAbort(
            f"no gate stamp at {GATE_STAMP} -> the bundle in dist\\ was never gated -> refusing "
            "to run Inno Setup. Fix: python installer\\build.py --half 1 (or --gate-only on an "
            "existing dist)."
        )
    try:
        stamp = json.loads(GATE_STAMP.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BuildAbort(f"gate stamp {GATE_STAMP} is unreadable ({exc}); re-run the gate") from exc
    if stamp.get("schema") != GATE_STAMP_SCHEMA:
        raise BuildAbort(f"gate stamp schema {stamp.get('schema')} != {GATE_STAMP_SCHEMA}; re-run the gate")
    if stamp.get("dist") != str(dist_root.resolve()):
        raise BuildAbort(f"gate stamp is for {stamp.get('dist')}, not {dist_root}; re-run the gate")
    if stamp.get("installer_iss_sha256") != sha256_file(ISS):
        raise BuildAbort(
            "installer.iss changed after the gate -> what ISCC packs is no longer what was "
            "gated (packaging_selftest reads it) -> refusing. Re-run --gate-only.")
    env_tp = os.environ.get("SIGN_CP", "").replace(" ", "").strip().upper()
    stamp_tp = (stamp.get("cp") or {}).get("thumbprint") or ""
    if env_tp and env_tp != stamp_tp.upper():
        raise BuildAbort(
            f"SIGN_CP={env_tp} but the gated bundle's CP is {stamp.get('cp')} -> this half would "
            "package a DLL signed differently from what you asked for -> refusing. Re-run --half 1.")
    log("verifying the gate stamp against the bundle (re-hashing)")
    now = bundle_manifest(dist_root)
    if now != stamp.get("manifest"):
        raise BuildAbort(
            f"dist\\ changed after the gate: now {now['files']} files / {now['sha256'][:16]}..., "
            f"gated {stamp['manifest'].get('files')} files / {stamp['manifest'].get('sha256', '')[:16]}... "
            "-> ISCC would pack a bundle nobody gated -> refusing. Re-run --gate-only (or --half 1).")
    log(f"gate stamp OK: gated {stamp.get('gated_at')} at {stamp.get('git_head') or '?'}, "
        f"{now['files']} files, CP {stamp['cp']['mode']}")
    return stamp


# --------------------------------------------------------------------------- 6
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


def step_inno(dist_root: Path) -> Path:
    step(6, "Inno Setup (gate stamp checked first)")
    check_gate_stamp(dist_root)
    iscc = _find_iscc()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run([iscc, str(ISS)], cwd=REPO_ROOT)
    # Expected output: installer_output\WindowsFaceUnlock-Setup-<ver>.exe
    outputs = sorted(OUTPUT_DIR.glob("WindowsFaceUnlock-Setup-*.exe"))
    if not outputs:
        raise BuildAbort(f"Inno Setup produced no artefact in {OUTPUT_DIR}")
    return outputs[-1]


# --------------------------------------------------------------------------- 7
def step_checksums(installer_path: Path) -> None:
    step(7, "SHA-256 checksums")
    digest = sha256_file(installer_path)
    (installer_path.parent / f"{installer_path.name}.sha256").write_text(
        f"{digest}  {installer_path.name}\n", encoding="ascii"
    )
    log(f"sha256 = {digest}")


# --------------------------------------------------------------------------- main
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Build the Windows Face Unlock installer.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--half", type=int, choices=(1, 2),
                      help="1 = steps 0-5 (stage + gate, writes the stamp); "
                           "2 = steps 6-7 (Inno Setup + checksums, requires the stamp)")
    mode.add_argument("--gate-only", action="store_true",
                      help="re-run step 5 on the existing dist and re-stamp it")
    mode.add_argument("--check-models", action="store_true",
                      help="run step 0 only (pack presence + pinned SHA-256)")
    ap.add_argument("--allow-unsigned-cp", action="store_true",
                    help="build without SIGN_CP; the CP DLL ships unsigned (recorded in the stamp)")
    return ap.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    if args.check_models:
        step_check_models()
        return 0

    if args.half == 2:
        installer_path = step_inno(DIST_ROOT)
        step_checksums(installer_path)
        log(f"Installer ready: {installer_path}")
        return 0

    policy = resolve_sign_policy(args.allow_unsigned_cp)   # fail fast, before 40 minutes of build

    if args.gate_only:
        cp_dll = None if cp_skipped() else CP_BUILD_DIR / "Release" / CP_DLL_NAME
        step_gate(DIST_ROOT, cp_dll, policy)
        log("gate-only: stamp refreshed; next: python installer\\build.py --half 2")
        return 0

    step_check_models()   # cheapest check, and the one that invalidates the whole build
    cp_dll = step_build_cp()
    step_sign_cp(cp_dll, policy)
    dist_root = step_pyinstaller()
    step_stage(dist_root, cp_dll)
    step_gate(dist_root, cp_dll, policy)
    if args.half == 1:
        log("half 1 done: bundle staged and gated. Next: the operator dist-smoke out of "
            f"{dist_root} (installer/README.md), then python installer\\build.py --half 2")
        return 0
    installer_path = step_inno(dist_root)
    step_checksums(installer_path)
    log(f"Installer ready: {installer_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    sys.exit(main())
