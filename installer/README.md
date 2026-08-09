# Installer build

This folder builds the end-user installer that drops Windows Face Unlock onto a
machine with nothing installed — no Python, no ONNX runtime, no Visual C++
redist beyond what Windows ships. One `.exe`, users double-click, done.

Two ways to build: locally (for testing) and in CI (for releases).

## What you get

`installer_output\WindowsFaceUnlock-Setup-<version>.exe`, plus a `.sha256`
next to it.

The installer:
- Copies everything into `C:\Program Files\WindowsFaceUnlock\`.
- Registers the scheduled tasks declared in `tools\tasks.psd1` (staged into
  `{app}\postinstall`) and starts them.
- Optionally registers `FaceCredentialProvider.dll` with `regsvr32` so the face
  tile appears on the Windows lock screen.
- On uninstall: walks the same task declaration to stop and remove the tasks,
  unregisters the DLL, removes all files, and asks whether to also wipe
  `%USERPROFILE%\.face-unlock`.

## Local build

Prerequisites:
- Windows 10 / 11 x64
- Python 3.11 or 3.12 in PATH
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (installs `ISCC.exe`)
- Visual Studio 2022 Build Tools with the C++ workload, to build the Credential
  Provider DLL. Set `SKIP_CP=1` to deliberately build a
  presence-auto-lock-only installer without it.
- `cmake` on `PATH`. Build Tools ships one but does not put it there, so a plain
  shell fails at step 1 with `FileNotFoundError: [WinError 2]` — which reads like
  a broken CP build rather than a missing tool. It lives under
  `<BuildTools>\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin`.

Steps from the repo root:

```powershell
.\setup.ps1
.\.venv\Scripts\pip install -r installer\requirements-build.txt

.\.venv\Scripts\python installer\build.py
```

`build.py` runs, in order:

1. CMake → `build-cp\Release\FaceCredentialProvider.dll`. Built at the repo root
   with `-S credential_provider -B build-cp`, because `build-cp\` is the tree the
   registered CLSID points at. The tree is **not** wiped first — it holds the DLL
   LogonUI may be loading right now. Skipped only with `SKIP_CP=1`; any other
   failure aborts the build.
2. PyInstaller against `windows_face_unlock.spec` → three exes
   (`face_service.exe`, `face_unlock_tray.exe`, `face_unlock_watchdog.exe`)
   sharing one runtime folder in `dist\WindowsFaceUnlock\`.
3. Stage into that folder: the CP DLL + `register.ps1` under
   `credential_provider\`, the task registrar + `tasks.psd1` under `postinstall\`,
   and the top-level docs.
4. `ISCC.exe installer\installer.iss` → installer in `installer_output\`.
5. SHA-256 checksum next to the installer.

### The gate between step 3 and step 4

Do not compile the installer around a bundle nobody has run. Two Stage-7 blocks
shipped a `dist\` that every check of the day called green and that could not
start: `face_unlock_tray.exe` died instantly on a relative import in its entry
script, and `face_service.exe` died ninety seconds in, inside numpy, on a Python
module that only numpy's C extension imports. Neither is visible in
`warn-*.txt`, and neither is visible by reading the PYZ — which is exactly how
both survived a validation pass that consisted of reading the PYZ.

So the gate has two halves, and step 4 waits for both:

```powershell
.\.venv\Scripts\python tools\verify_frozen_entrypoints.py
```

is the static half. It walks the entry-point sources for relative imports,
disassembles the entry bytecode **out of the built EXEs** to ask the artefact the
same question, checks the PYZ holds every absolute target, and verifies numpy's
extension has both its native dependencies and every Python module it imports by
name from C. Exit code 0 means clear to compile.

The other half is a human running the executables out of `dist\` — at minimum
`face_unlock_tray.exe --set-password` (a dialog must appear) and
`face_service.exe` (silence; it exits as a mutex-loser if a service is already
running). A static check proves what was collected. It never proves the result
runs.

### Environment knobs

| Variable          | Effect |
|-------------------|--------|
| `SKIP_CP=1`       | Build without the Credential Provider DLL. The installer still offers the task box, but its `FileExists` check will be false so it stays unavailable. |
| `INNO_SETUP_ISCC` | Full path to `ISCC.exe` if it is not at the default `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`. |

## Serialization rule (hard)

Setup and the uninstaller never run at the same time. This is not a preference:
whichever starts second breaks, and it takes the first one's work with it. On
2026-08-08 `unins000.exe /REMOVEDATA` was started while a silent install was
still running — a message box the silent run could not display was auto-answered
*Abort*, the install rolled back, and four completed acceptance steps went with
it.

The only signal that a run has finished is `Log closed` in its Inno log. Not the
window going away, not the process leaving the task list. The uninstaller needs a
second signal on top of that, because `unins000.exe` copies itself into `%TEMP%`
and runs from the copy: the process you can watch exit is the copy, and its exit
code is not observable from where you started it. For the uninstaller, the
install directory under `Program Files` disappearing is what says the removal
actually completed.

A silent run can print nothing for up to fifteen minutes — the bundled models
alone are ~325 MiB, compressed `lzma2/ultra64`. That is normal, and it is not
evidence of a hang. Do not interrupt it; `Ctrl+C` leaves a half-written install
directory and a registration state with no name. When a wait is unavoidable, wait
in a loop that echoes progress, so that "still working" and "wedged" stay
distinguishable from each other.

## CI build

`.github/workflows/release.yml` reproduces the local pipeline on `windows-2022`
runners and publishes a GitHub Release attached to the `v<version>` tag:

```bash
git tag v0.1.0
```

The workflow stamps `__version__` and Inno Setup's `MyAppVersion` from the tag
before building. It does **not** set `SKIP_CP`: a release that cannot build the
Credential Provider should fail, not ship a face-unlock installer that cannot
unlock.

## The recognition models are bundled (Stage 7d)

The engine is InsightFace `buffalo_l`. It used to be the pipeline's known gap:
`FaceAnalysis(name="buffalo_l")` was called without a `root=`, so insightface
looked in `%USERPROFILE%\.insightface\models\buffalo_l\` and, finding it empty on
a fresh machine, downloaded the pack over plain HTTP at the first unlock attempt
— unpinned, unchecksummed, with no timeout, and impossible offline. The spec even
shipped `requests` and `tqdm` to make that download work.

Now the five `.onnx` files are shipped as `datas`, and
`face_service.recognizer.model_root()` points a frozen build at
`insightface_home/models/buffalo_l` inside the bundle. An installed machine
therefore needs no network for recognition.

What that costs, and why the numbers differ from the old "~290 MB":

| what | size |
|---|---|
| `buffalo_l.zip`, the download | ~275 MiB |
| the five unpacked `.onnx` — what ships | ~325 MiB |
| both, which is what an auto-download leaves on disk | ~600 MiB |

Only the unpacked set goes into the installer. The archive is deliberately not
shipped: nothing reads it, and it would nearly double the download for no gain.
The release workflow removes it after fetching, because insightface extracts and
then keeps it (its `os.remove` is commented out).

Only four of the five models are used (`ALLOWED_MODULES` in `recognizer.py`), but
all five must be present: `FaceAnalysis` globs every `*.onnx` in the directory and
builds a session for each **before** the filter runs. `genderage.onnx` is 1.3 MiB,
so there is nothing to save by trimming it.

Both `installer/build.py` (step 0) and the spec itself refuse to build if the pack
on the build machine is incomplete — an installer without models is precisely the
defect this replaced.

## Code signing

The installer is published unsigned by default. First-time users will see
Windows SmartScreen flag it as *Unknown publisher* — they click **More info →
Run anyway**. Acceptable for tinkerers, not ideal for wider distribution.

The workflow integrates with [SignPath.io](https://signpath.io/open-source),
which offers free code signing to vetted open-source projects. Once approved,
add these repository secrets:

| Secret                  | From SignPath |
|-------------------------|---------------|
| `SIGNPATH_API_TOKEN`    | CI token |
| `SIGNPATH_ORG_ID`       | Organization ID |
| `SIGNPATH_PROJECT_SLUG` | Project slug |

With all three present, the workflow auto-detects them and submits the installer
for signing between PyInstaller and Release publish. Without them, the unsigned
installer goes out unchanged — no workflow edits needed.

Alternatives if SignPath isn't an option:
- **Certum Open Source Code Signing** — ~USD 25/year, requires ID verification.
- **Azure Trusted Signing** — ~USD 10/month, fastest if you already have Azure.
  Plug into the workflow via `azure/trusted-signing-action`.
- **Self-signed certs do NOT help SmartScreen** — reputation requires a CA
  Microsoft trusts. Don't bother.

## Anatomy

- `windows_face_unlock.spec` — PyInstaller: two Analyses merged via `MERGE()` so
  the shared runtime lands once. Collects `insightface` (minus its PySide GUI and
  its face3d thirdparty tree), `onnxruntime`, `skimage.transform` — which
  `insightface.utils.face_align` imports and scikit-image hides behind
  `lazy_loader` — and `cv2`. Also reproduces the `nvidia\<pkg>\bin` layout that
  `recognizer._prep_cuda_dlls()` walks, without which the CUDA provider silently
  falls back to CPU. Excludes matplotlib and Qt.
- `requirements-build.txt` — build-time-only pins (PyInstaller). Kept out of the
  runtime `requirements.txt` on purpose.
- `installer.iss` — Inno Setup script. Admin install, `lzma2/ultra64`,
  `CloseApplications=yes` so the updater can replace files in place, one optional
  task (register the CP), uninstall asks about user data. It contains no
  scheduled-task names: install and uninstall both call the registrar.
- `build.py` — glue script that runs all of the above.

## Updating

The tray process checks GitHub Releases 30 seconds after start and whenever the
user picks *"Check for updates…"* in the tray menu. If a newer `tag_name` is
found with an `.exe` asset, it offers to download and launch the installer
silently (`/SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`). The installer then
stops the running tray and service, replaces files, and restarts both.

See [`presence_monitor/updater.py`](../presence_monitor/updater.py) and the
`[Setup] CloseApplications=yes` line in `installer.iss`.
