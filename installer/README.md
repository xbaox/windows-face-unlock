# Installer build

This folder builds the end-user installer: one `.exe` that puts Windows Face Unlock onto a machine
with nothing installed -- no Python, no ONNX runtime. Two variants (Stage 9, act 9b R17):

| Variant | File | For |
|---|---|---|
| **CPU** (main, recommended) | `WindowsFaceUnlock-Setup-<ver>-cpu.exe` | every x64 PC |
| **GPU** | `WindowsFaceUnlock-Setup-<ver>-gpu.exe` | PCs with an NVIDIA GPU; adds only the NVIDIA DLLs the ONNX Runtime CUDA provider needs |

Each comes with `<file>.sha256` and `<file>.buildinfo.json` (bundle manifest hash, git head,
variant, signing state). Neither contains a face recognition model.

## What the installer does

- Installs into `C:\Program Files\WindowsFaceUnlock` -- always; a `/DIR=` elsewhere is refused.
- **Models (decision 9-02):** shows the InsightFace terms (non-commercial research use) and asks for
  consent, then downloads the official `buffalo_l.zip` (the source insightface uses) or takes a
  local copy (`/MODELZIP=<path>` or the file chooser), checks size + SHA-256 against
  `face_service/model_pins.py`, and unpacks the five pinned files into `{app}\models\buffalo_l`.
  An upgrade that already has valid models skips all of this. A silent install that needs the
  models fails closed without `/ACCEPTMODELLICENSE`.
- Checks that the program folder is writable by administrators only, then registers the sign-in
  tile (`regsvr32` of `credential_provider\FaceCredentialProvider.dll`, the `cp` task, checked by
  default; unticking it on an upgrade unregisters it).
- Registers and starts the three scheduled tasks through the product itself:
  `face_unlock_tray.exe --register --user-sid <owner>` (Task Scheduler over COM,
  `face_service/taskreg.py`). **No PowerShell runs during install or uninstall.**
- Offers the first-run steps on the Finish page: save the Windows password, set up the face.
- Upgrade: stops the running copy first (graceful pipe shutdown as the owner, the scheduler, then a
  bounded kill -- all over COM/WMI); if Setup is cancelled after that, the old tasks are started
  again.
- Uninstall: `face_unlock_tray.exe --unregister` (stop, remove the tasks, count survivors),
  `regsvr32 /u` plus a fallback removal of the provider's registry keys, busy files removed at the
  next restart, only its own folders deleted; the owner's data is kept unless they say otherwise.

Exit codes of a silent install that failed after the files were copied: **21** tasks, **22**
sign-in tile, **23** models (the Inno Setup codes 1-8 keep their usual meaning).

## Local build

Prerequisites: Windows 10/11 x64; Python **3.12**; Inno Setup **6.5+** (`ISCC.exe`); Visual Studio
2022 Build Tools with the C++ workload and its `cmake` on `PATH`
(`<BuildTools>\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin`). `SKIP_CP=1` builds without
the sign-in tile on purpose.

```powershell
.\setup.ps1 -SkipAutostart                         # .venv + requirements.lock (hash-checked)
.\.venv\Scripts\python -m pip install --require-hashes -r installer\requirements-build.txt
.\.venv\Scripts\python installer\build.py --variant cpu
```

For the GPU variant install `requirements-gpu.lock` instead (`setup.ps1 -Gpu`) and pass
`--variant gpu`; without the NVIDIA wheels the GPU build refuses rather than producing a CPU
bundle under a GPU name.

`build.py` steps (the signing order is act 9b R18's):

0. **Preflight** -- tools, the variant's packages, LICENSE; the old outputs of this variant are
   removed.
1. CMake -> `build-cp\Release\FaceCredentialProvider.dll`.
2. Sign the CP DLL (Azure Trusted Signing) -- **loudly skipped** without credentials.
3. PyInstaller (`FU_VARIANT=<variant>`) -> `dist\WindowsFaceUnlock\` (three exes, one runtime
   folder); the CP DLL, the docs (README, INSTALL, SECURITY, THIRD_PARTY_NOTICES, LICENSE) and
   every third-party license text are staged -- `licenses\<package>\` + `licenses\INDEX.txt`,
   copied by `installer/notices.py` from the build interpreter (F-260).
4. GPU only: every NVIDIA DLL is checked with Authenticode. The cuDNN DLLs are signed by NVIDIA;
   the CUDA runtime / cuBLAS / cuFFT / NVRTC wheels ship unsigned DLLs. They are never modified or
   re-signed (their EULA); the catalog (`.cat`) signature that would cover them needs our signing
   identity and is **skipped** until 9f -- a GPU build is not releasable before that.
5. Sign every unsigned PE of ours (never NVIDIA's, never an already-signed file) -- **skipped**
   without credentials.
6. **The gate** on the (signed) bundle: `tools\verify_frozen_entrypoints.py` (PE content compared
   without the certificate table), `tools\packaging_selftest.py`, the frozen custody self-check,
   no model in the bundle, the variant's shape (CPU: no CUDA provider, no NVIDIA file; GPU: exactly
   the allowlist; no FFmpeg, no TensorRT provider; pystray as replaceable `.py` files), every
   expected license folder present and non-empty (F-260), then `dist\WindowsFaceUnlock.gate.json`.
7. ISCC with the version, the variant and the model pins as `/D` defines (plus SignTool /
   SignedUninstaller when signing is configured). The artefact is exactly
   `WindowsFaceUnlock-Setup-<ver>-<variant>.exe`; the bundle is re-hashed after ISCC and must still
   match the stamp.
8. `.sha256` and `.buildinfo.json`.

`--half 1` = steps 0-6 (the operator dist-smoke can follow), `--half 2` = steps 7-8,
`--gate-only` re-runs step 6, `--resign` runs steps 2, 4, 5, 6 on an existing `dist\` (the CI sign
job).

### Signing (act 9b R18, until 9f)

Signing uses Azure Trusted Signing when `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
`AZURE_CODESIGN_ENDPOINT`, `AZURE_CODESIGN_ACCOUNT`, `AZURE_CODESIGN_PROFILE`, `SIGNTOOL` and
`AZURE_CODESIGN_DLIB` are set; `FU_SIGN_SUBJECT` pins the expected signer, and every signed file
must verify `Valid`. Without them every signing step prints `SIGNING SKIPPED` with the missing
names, and the result is a test build. See `credential_provider\SIGNING.md`.

## CI (`.github/workflows/release.yml`)

`test` (selftests + CP unit tests) -> `build` (cpu and gpu, read-only token, no secrets,
hash-checked installs, unsigned) -> `sign` (only when the Azure variables exist; OIDC, no
repository rights; `--resign` + `--half 2`) -> `publish` (only for a `vX.Y.Z` tag equal to
`face_service/_version.py`; the only job with `contents: write`; a **draft** release that the
operator smokes and publishes by hand).

## Scripted install

```
WindowsFaceUnlock-Setup-0.2.0-cpu.exe /VERYSILENT /ACCEPTMODELLICENSE [/MODELZIP=C:\path\buffalo_l.zip]
    [/OWNER=DOMAIN\user] [/FORCEOWNER] [/MERGETASKS="cp"]
unins000.exe /VERYSILENT [/REMOVEDATA]
```

The owner is the user signed in at the console (R1); `/OWNER=` names one explicitly, and replacing
a different recorded owner in a silent install needs `/FORCEOWNER`.

## Anatomy

- `installer.iss` -- the Inno Setup script (owner, models, stop / register through the product,
  uninstall).
- `lang\en.isl`, `lang\ru.isl` -- the installer's own texts, English and Russian.
- `windows_face_unlock.spec` -- PyInstaller (variants, NVIDIA allowlist, exclusions, version
  resources, the tray's PerMonitorV2 manifest).
- `build.py` -- the pipeline above. `notices.py` -- stages and checks the third-party license
  texts. `requirements-build.txt` -- PyInstaller and its dependencies, hashed.
