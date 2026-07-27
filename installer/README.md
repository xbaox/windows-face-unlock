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
2. PyInstaller against `windows_face_unlock.spec` → two exes (`face_service.exe`,
   `face_unlock_tray.exe`) sharing one runtime folder in `dist\WindowsFaceUnlock\`.
3. Stage into that folder: the CP DLL + `register.ps1` under
   `credential_provider\`, the task registrar + `tasks.psd1` under `postinstall\`,
   and the top-level docs.
4. `ISCC.exe installer\installer.iss` → installer in `installer_output\`.
5. SHA-256 checksum next to the installer.

### Environment knobs

| Variable          | Effect |
|-------------------|--------|
| `SKIP_CP=1`       | Build without the Credential Provider DLL. The installer still offers the task box, but its `FileExists` check will be false so it stays unavailable. |
| `INNO_SETUP_ISCC` | Full path to `ISCC.exe` if it is not at the default `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`. |

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

## Known gap: the recognition models are not bundled

The engine is InsightFace `buffalo_l`, and nothing in this pipeline ships it.
`FaceAnalysis(name="buffalo_l")` is called without a `root=`, so insightface
looks in `%USERPROFILE%\.insightface\models\buffalo_l\` and, if that is empty,
downloads roughly 290 MB from the internet on first use — with no pinned version
and no checksum. On a fresh machine that download happens the first time the
service warms up.

Provisioning these models with the installer is open work for Stage 7d. Do not
assume a freshly installed machine can sign in offline.

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
