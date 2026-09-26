#!/usr/bin/env python3
"""Self-test: the packaging declarations must not drift apart.

Stage 7d-G. Four facts live in more than one file, and nothing pinned them together. Each of them
fails SILENTLY and only on a machine nobody is looking at:

  [1] the version. face_service/_version.py and installer/installer.iss carry independent
      literals. CI rewrites both from the git tag, so a release cannot drift -- but a locally
      built installer can advertise a version its own bundled __version__ disagrees with, and
      __version__ is exactly what the updater compares to decide whether a release is newer.

  [2] the executable names. installer/windows_face_unlock.spec names each EXE; tools/tasks.psd1
      names the InstalledExe the scheduled task launches; tools/watchdog.py matches the service by
      image name in the Installed layout. A rename in one place leaves a task pointing at a file
      that does not exist, or a watchdog that matches nothing -- and the watchdog failure mode is
      an inert restart that still reports success on the kill.

  [3] tools.pipe_client must be in the bundle. register_tasks.ps1 asks the frozen tray for
      --pipe-shutdown, which routes into it; without the hidden import the graceful shutdown
      silently degrades to a hard kill on every installed stop.

  [4] three entry points. The watchdog EXE is what lets FaceUnlock-Watchdog exist in the Installed
      layout at all.

Parses the declarations as TEXT -- no PyInstaller import, no scheduled-task query, no build. Reads
only files inside the repo.

Run from repo root:
    python tools\\packaging_selftest.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = REPO_ROOT / "installer" / "windows_face_unlock.spec"
ISS = REPO_ROOT / "installer" / "installer.iss"
VERSION_PY = REPO_ROOT / "face_service" / "_version.py"
TASKS_PSD1 = REPO_ROOT / "tools" / "tasks.psd1"
WATCHDOG_PY = REPO_ROOT / "tools" / "watchdog.py"

# Entry points the spec is expected to produce, and the task each one backs.
EXPECTED_EXES = {"face_service", "face_unlock_tray", "face_unlock_watchdog"}


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def spec_exe_names(text: str) -> list[str]:
    """Every ``name="..."`` that belongs to an EXE(...) block, in file order."""
    names = []
    for m in re.finditer(r"=\s*EXE\(", text):
        tail = text[m.end():]
        stop = tail.find("\n)")
        block = tail if stop < 0 else tail[:stop]
        nm = re.search(r'name\s*=\s*"([^"]+)"', block)
        if nm:
            names.append(nm.group(1))
    return names


def psd1_installed_exes(text: str) -> dict[str, str]:
    """Task name -> InstalledExe, for every declared task (empty values included)."""
    out: dict[str, str] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        # Comments in this file quote the watchdog's matcher, which contains Name='pythonw.exe'.
        # Anchoring on the key and skipping comments keeps that prose out of the parse.
        if not line or line.startswith("#"):
            continue
        m = re.match(r"Name\s*=\s*'([^']*)'", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"InstalledExe\s*=\s*'([^']*)'", line)
        if m and current:
            out[current] = m.group(1)
            current = None
    return out


def main(argv=None) -> int:
    t = T()
    spec = _read(SPEC)
    iss = _read(ISS)

    print("[1] version literals agree")
    v_py = re.search(r'__version__\s*=\s*"([^"]+)"', _read(VERSION_PY))
    v_iss = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss)
    t.ok(bool(v_py), "face_service/_version.py declares __version__")
    t.ok(bool(v_iss), "installer.iss defines MyAppVersion")
    if v_py and v_iss:
        t.ok(v_py.group(1) == v_iss.group(1),
             f"_version.py {v_py.group(1)!r} == installer.iss {v_iss.group(1)!r}")

    print("\n[2] spec EXE names == tasks.psd1 InstalledExe")
    exes = spec_exe_names(spec)
    t.ok(len(exes) == len(set(exes)), f"spec EXE names are unique ({exes})")
    t.ok(set(exes) == EXPECTED_EXES,
         f"spec builds exactly {sorted(EXPECTED_EXES)} (got {sorted(exes)})")
    declared = psd1_installed_exes(_read(TASKS_PSD1))
    t.ok(bool(declared), f"tasks.psd1 parsed ({len(declared)} tasks)")
    spec_files = {f"{n}.exe" for n in exes}
    for task, exe in sorted(declared.items()):
        if not exe:
            t.ok(False, f"{task}: InstalledExe is empty -- the task is skipped when installed")
            continue
        t.ok(exe in spec_files,
             f"{task}: InstalledExe {exe!r} is built by the spec")

    print("\n[3] the watchdog matches the service exe the spec actually builds")
    wd = _read(WATCHDOG_PY)
    m = re.search(r'_INSTALLED_SERVICE_EXE\s*=\s*"([^"]+)"', wd)
    t.ok(bool(m), "tools/watchdog.py declares _INSTALLED_SERVICE_EXE")
    if m:
        svc = declared.get("FaceUnlock-Service", "")
        t.ok(m.group(1) == svc,
             f"watchdog matches {m.group(1)!r} == FaceUnlock-Service InstalledExe {svc!r}")

    print("\n[4] hidden imports the frozen layout depends on")
    hidden_block = spec[spec.find("HIDDEN += ["):spec.find("DATAS = []")]
    for mod in ("tools.pipe_client", "presence_monitor.password_gui",
                "presence_monitor.enroll_gui", "face_service.logging_setup"):
        t.ok(f'"{mod}"' in hidden_block, f"hiddenimports contains {mod}")

    print("\n[5] three Analysis entry points")
    analyses = re.findall(r"(\w+)\s*=\s*Analysis\(", spec)
    t.ok(len(analyses) == 3, f"spec declares 3 Analysis blocks (got {len(analyses)}: {analyses})")
    scripts = re.findall(r'REPO_ROOT\s*/\s*"([^"]+)"\s*/\s*"__main__\.py"', spec)
    t.ok(set(scripts) == {"face_service", "presence_monitor"} or len(scripts) >= 2,
         f"entry scripts resolve from the repo ({scripts})")
    t.ok("tools" in spec and "watchdog" in spec,
         "the spec references the watchdog entry point")

    print("\n[6] the recognition models are NOT bundled (decision 9-02, act 9b R7)")
    from face_service.recognizer import MODEL_FILES
    shipped = [n for n in MODEL_FILES if f'"{n}"' in spec]
    t.ok(not shipped and "insightface_home" not in spec.split("# The buffalo_l recognition pack is NOT", 1)[-1],
         f"the spec ships no buffalo_l file (found: {shipped or 'none'})")
    build = _read(REPO_ROOT / "installer" / "build.py")
    t.ok("def gate_no_models" in build and "gate_no_models(dist_root)" in build,
         "build.py fails the build when a model reaches dist")

    print("\n[7] the updater points at this repository")
    up = _read(REPO_ROOT / "presence_monitor" / "updater.py")
    owner = re.search(r'^REPO\s*=\s*"([^"]+)"', up, re.M)       # Stage 9 (R15): one constant
    t.ok(bool(owner) and owner.group(1) == "xbaox/windows-face-unlock",
         f"REPO is this fork (got {owner.group(1)!r} )" if owner else "REPO found")
    t.ok("WindowsFaceUnlock-Setup-" in up,
         "the installer asset is selected by its exact published name pattern")

    # --- Stage 8b: installer invariants (F-41, F-01, F-09, F-34, F-35, F-07, F-36) -------------
    print("\n[8] installer invariants (Stage 8b)")
    pub = re.search(r'#define\s+MyAppPublisher\s+"([^"]+)"', iss)
    url = re.search(r'#define\s+MyAppURL\s+"([^"]+)"', iss)
    t.ok(bool(pub) and pub.group(1) == "xbaox", f"Publisher is this fork (F-41): {pub and pub.group(1)}")
    t.ok(bool(url) and url.group(1) == "https://github.com/xbaox/windows-face-unlock",
         f"AppURL is this fork (F-41): {url and url.group(1)}")
    readme = _read(REPO_ROOT / "README.md")
    t.ok("caochitam" not in iss and "caochitam/windows-face-unlock/releases" not in readme,
         "no upstream URL left in installer.iss or the README install link (F-41)")
    code_lines = [ln for ln in iss.splitlines() if not ln.lstrip().startswith(";")]
    t.ok(not any(ln.strip().lower().startswith("[dirs]") for ln in code_lines)
         and "users-modify" not in "\n".join(code_lines),
         "no [Dirs] section and no users-modify permission (F-01)")
    t.ok(re.search(r"^DisableDirPage=yes\s*$", iss, re.M) is not None, "DisableDirPage=yes (F-09)")
    uninst = re.split(r"^\[UninstallRun\]\s*$", iss, maxsplit=1, flags=re.M)[1].split("[UninstallDelete]", 1)[0]
    entries = [ln for ln in uninst.splitlines() if ln.startswith("Filename:")]
    t.ok(len(entries) == 2 and uninst.count("RunOnceId:") == 2,
         f"both [UninstallRun] entries carry RunOnceId (F-34): {uninst.count('RunOnceId:')}")
    prep = iss.split("function PrepareToInstall", 1)[1].split("function InstallModels", 1)[0]
    # Stage 9 (R17): the stop runs in [Code] over COM / WMI (no registrar to extract); the
    # registrations stay, the Register after the copy overwrites them (F-35 semantics kept).
    t.ok("StopStack(Dir)" in prep and "Unregister" not in prep and "DeleteTask" not in prep,
         "PrepareToInstall STOPS the stack (registrations kept), no unregister (F-35)")
    t.ok("WTSGetActiveConsoleSessionId" in iss and "'--register --user-sid ' + GetOwnerSid()" in iss
         and "'OriginalUserSid', GetOwnerSid()" in iss and "/FORCEOWNER" in iss and "/OWNER" in iss,
         "the registrar and the registry get the OWNER's SID -- the console user or /OWNER= "
         "(Stage 9 R1; was the original user, F-07)")
    psd1 = _read(REPO_ROOT / "tools" / "tasks.psd1")
    t.ok(re.search(r"Priority\s*=\s*5", psd1) is not None and psd1.count("RestartOnFailure = $true") == 2,
         "service priority 5; restart policy on tray and watchdog (F-36)")

    print()
    if t.fail:
        print(f"PACKAGING SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("PACKAGING SELFTEST OK: version literals agree, every declared InstalledExe is an EXE "
          "the spec builds, the watchdog matches the service exe by the same name, the frozen-only "
          "hidden imports are present, all three entry points are declared, no recognition model "
          "is bundled, and the installer invariants hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
