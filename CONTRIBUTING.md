# Contributing and the developer layout

Thanks for looking at the code. This page covers building and running Face Unlock from a source
checkout, the tests, and the rules a change has to follow. End-user documentation is in
[README.md](README.md), [INSTALL.md](INSTALL.md) and [SECURITY.md](SECURITY.md). Security problems:
see [SECURITY.md](SECURITY.md#reporting-a-vulnerability), not the issue tracker.

## Repository map

| Path | What |
|---|---|
| `face_service/` | the service: camera, recognition, liveness, the pipe server, data-folder custody, task registration (`taskreg.py`) |
| `presence_monitor/` | the tray: Settings, Status, setup wizard, password dialog, notifications, updater check, walk-away lock |
| `credential_provider/` | the lock-screen tile (C++ DLL) and its unit tests (`tests/`) |
| `installer/` | Inno Setup script, PyInstaller spec, `build.py`, `notices.py` -- see [installer/README.md](installer/README.md) |
| `tools/` | selftests (`*_selftest.py`), the dev task registrar, the dev uninstaller, the frozen-bundle gate, `pipe_client.py`, `set_password.py` (console password tool), `lowlight_probe.py`, `measure_threshold.py` |
| `tools/diag/` | diagnostics that need a camera, a live service or a person: `bench`, `camera_busy_integration`, `camera_cold_probe`, `challenge_probe`, `enroll_qc_probe`, `screen_probe`, `session_lock_probe`, `shutdown_integration`. Run from the repo root as `python -m tools.diag.<name>`. Not tests, not shipped. |
| `models/` | the YuNet detector and its license. The InsightFace models are never committed. |
| `docs/SMOKE.md` | how a live smoke test is run and judged |
| `docs/internal/` | the project's internal working documents (Russian): audit notes, task briefs, the known-issues ledger, the Stage-9 decisions. Code comments that say `KNOWN_ISSUES #N` refer to `docs/internal/KNOWN_ISSUES.md`. Not user documentation. |

## Requirements

- Windows 11 x64 (Windows 10 22H2 works for development too).
- **Python 3.12** (the lock files pin packages that need it).
- Visual Studio 2022 Build Tools with the C++ workload (for the lock-screen DLL); its `cmake` is at
  `<BuildTools>\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin`.
- Inno Setup 6.5+ to build the installer.
- Optional: an NVIDIA GPU and the GPU lock (`requirements-gpu.lock`).

## Setting up a checkout

```powershell
.\setup.ps1 -SkipAutostart          # .venv + requirements.lock, hash-checked; -Gpu for requirements-gpu.lock
```

**Models.** A source checkout reads the InsightFace `buffalo_l` models from
`%USERPROFILE%\.insightface\models\buffalo_l\` and never downloads them. Put the five files there
yourself (from `buffalo_l.zip`, see [INSTALL.md §5](INSTALL.md#5-installing-without-internet-or-silently)
for the URL and SHA-256), after reading the InsightFace terms. The service checks them against
`face_service/model_pins.py` and refuses to run face functions with anything else.

**Running by hand** (two terminals):

```powershell
.\.venv\Scripts\python -m face_service
.\.venv\Scripts\python -m presence_monitor
```

Talk to a running service with `.\.venv\Scripts\python -m tools.pipe_client status` (or `ping`,
`verify`). `unlock` and `unlock_gesture` answer only the sign-in screen (SYSTEM), by design.

## The developer logon tasks, and why they may refuse (R19)

`setup.ps1` without `-SkipAutostart` (or `tools\register_tasks.ps1 -Mode Dev -Action Register`)
registers the three logon tasks to run **this checkout's** Python code at every sign-in. Because
that code then handles your face data and your Windows password, the registrar **refuses** when the
checkout -- the repo root, `.venv`, `.venv\Scripts`, `face_service`, `presence_monitor` or `tools`
-- can be changed by anyone other than Administrators, SYSTEM or TrustedInstaller. A checkout under
your user profile, or under a folder that grants *Authenticated Users* or *Users* write access
(the default for new folders on `C:\`), is refused.

To use the dev tasks, put the checkout under a folder only administrators can write (for example a
folder whose permissions you restricted to Administrators and SYSTEM, with read/execute for Users)
and edit it from an elevated editor -- or simply run the parts by hand as above, or use the
installer. `tools\uninstall.ps1 -Force` applies the same check.

The dev layout and an installed copy must not run at the same time (they share the pipe name and
the data folder). `tools\register_tasks.ps1` only touches this checkout's tasks;
`tools\uninstall.ps1` (inventory first, `-Force` to act; `tools\uninstall_dryrun_proof.ps1` proves
the inventory part cannot change anything) removes an installed copy through its own executable.

## Tests

**Selftests.** Every `tools/*_selftest.py` runs without a camera, a live service or network:

```powershell
foreach ($t in Get-ChildItem tools -Filter '*_selftest.py') { .\.venv\Scripts\python -m ("tools." + $t.BaseName) }
```

Isolation is enforced, not optional: each test calls `tools.testhome.isolate()` (or `own_root()`)
**before** importing anything of the product. It uses a `FACE_UNLOCK_HOME` you pass only if that
folder is inside the temporary directory and is not your real `%USERPROFILE%\.face-unlock`;
otherwise it creates its own temporary home and removes it at exit. A test pointed at the real data
folder exits with code 2 and touches nothing. `tools/isolation_selftest.py` checks all of this, for
every test. New tests must follow the same pattern (the isolation selftest fails otherwise), must
restore anything they monkeypatch (`tools.testkit.patch` / `run_restoring`), and must not sleep to
synchronise threads -- use events.

A section that cannot run (a missing optional import) is a **failure** unless you pass
`--allow-skip`.

**Credential Provider unit tests:**

```powershell
cmake -S credential_provider/tests -B build-tests -A x64
cmake --build build-tests --config Release
.\build-tests\Release\test_parser.exe
```

**The frozen-bundle gate** (`tools/verify_frozen_entrypoints.py`) runs inside `installer\build.py`;
`tools/verifier_selftest.py` and `tools/frozen_custody_selftest.py` test it on fixtures.

CI (`.github/workflows/release.yml`) runs all selftests (each with its own TEMP and home) and the
C++ tests before any build.

## Rules for changes

- **Security-relevant code** -- the pipe, custody of the data folder, the password path, liveness,
  the lock-screen DLL -- needs a test that fails without the change.
- **Two functions are frozen** (`Recognizer.verify_frame` and `_prep_cuda_dlls` in
  `face_service/recognizer.py`); changing them needs a recorded decision and a re-measurement.
- **Languages:** English and Russian are complete and must stay key-for-key equal
  (`face_service/i18n.py`, the lock-screen texts in `credential_provider/PipeClient.cpp`, and the
  installer's `installer/lang/*.isl`). `tools/presence_guards_selftest.py` checks the parity.
- **PowerShell files are ASCII-only** and must work in Windows PowerShell 5.1.
- **Dependencies** are pinned with hashes. To change one, regenerate the lock files from a tested
  environment and install with `--require-hashes`.
- **Third-party licenses:** a new runtime dependency must come with its license text; `installer/notices.py`
  copies it from the package's dist-info, and the build gate fails if a package's folder is empty.
  Add a row to [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **Lock-screen DLL identity:** if you register your own build of the DLL for experiments, use your
  own CLSID in `credential_provider/guid.h` so it cannot collide with an installed copy.
- Commit messages say what changed and why; reference finding IDs where there are any.

## Building the installer

See [installer/README.md](installer/README.md): `installer\build.py --variant cpu|gpu`, the gate,
signing (Azure Trusted Signing, skipped loudly without credentials) and CI.
