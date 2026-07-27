# Face Unlock — Fresh Install Guide

This captures every lesson learned during the original setup so the next
install on a fresh machine is painless. Read top-to-bottom.

## 0. Prerequisites

| Requirement | Tested version | Install command |
|---|---|---|
| Windows 10/11 x64 | 11 Pro 26200 | — |
| Python 3.11 or 3.12 | 3.12.10 | `winget install Python.Python.3.12` |
| Git | any | `winget install Git.Git` |
| CMake | 4.3 | `winget install Kitware.CMake` |
| VS Build Tools 2022 (C++ workload + Win11 SDK) | 17.14 | see §3 |
| GitHub CLI (optional, for push) | 2.89 | `winget install GitHub.cli` |

An NVIDIA GPU is optional. Recognition runs on the CUDA execution provider when
one is present and falls back to CPU with a warning in `service.log` when it is
not.

## 1. Clone + Python setup

```powershell
git clone <this-repo> C:\Users\<you>\Documents\Projects\face-unlock
cd C:\Users\<you>\Documents\Projects\face-unlock
.\setup.ps1               # creates .venv, installs deps, registers the scheduled tasks
```

`requirements.txt` pulls the recognition engine — `onnxruntime-gpu` (pinned),
`insightface`, `onnx`, `opencv-python`, `numpy` — plus `pywin32`, `psutil`,
`pystray`, `Pillow`, `tomli-w`, and `tomli` on Python 3.10 only.

Active liveness (blink, head-pose gesture, anti-screen) is built on the
InsightFace landmark models, so it needs no extra ML dependency.

### Recognition models

The engine is InsightFace `buffalo_l`. It is **not** in this repo and not
downloaded by `setup.ps1`: insightface fetches it into
`%USERPROFILE%\.insightface\models\buffalo_l\` (~290 MB) the first time the
service warms up. Budget for that on a fresh machine, and make sure the first
warmup happens while you are online.

The YuNet detector used by `presence_mode = "detection"` **is** bundled, at
`models/face_detection_yunet_2023mar.onnx`.

## 2. Enrollment + password

```powershell
.\.venv\Scripts\python -m tools.enroll capture --count 15    # look at camera, move head slightly
.\.venv\Scripts\python -m tools.set_password                 # enter Windows password (DPAPI encrypt)
```

Enrollment only keeps frames that pass the quality gates (detector confidence,
sharpness, exposure); with 15 captures expect roughly 9 to survive. Re-run
`enroll capture --count 20` if fewer than 6 make it.

`set_password` stores `{user, password, domain}` in
`%USERPROFILE%\.face-unlock\credentials.bin` encrypted with DPAPI (user scope).
The password never leaves your user profile.

## 3. Install Visual Studio Build Tools (only for the Credential Provider)

Skip this section if you only want presence auto-lock without real lock-screen
unlock.

```powershell
winget install Microsoft.VisualStudio.2022.BuildTools --silent --override `
  "--wait --quiet --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.Windows11SDK.22621 --includeRecommended"
```

About 6–8 GB. Takes 10–20 minutes depending on bandwidth.

## 4. Build + register the Credential Provider DLL

Build from the repo root. The output tree `build-cp\` is the one the registered
CLSID points at, so this is where LogonUI loads the DLL from — build here and
nowhere else, and never delete the directory.

```powershell
cmake -S credential_provider -B build-cp -A x64 -G "Visual Studio 17 2022"
cmake --build build-cp --config Release

# Then, from an Administrator PowerShell:
.\credential_provider\register.ps1 -Action register
```

`register.ps1` requires elevation, finds the DLL itself, and verifies the result
against the registry before reporting success. Remove it any time with
`.\credential_provider\register.ps1 -Action unregister`.

**Test the fallback before you rely on face sign-in:** stop the service task,
lock the workstation, and confirm PIN or password still gets you in.

### Gotchas encountered while building

These are already fixed in the source; listed only for troubleshooting.

1. **`FIELD_STATE_PAIR` undefined** — it's in the Microsoft sample set, not the
   public SDK. `helpers.h` defines it locally.
2. **`CPFG_CREDENTIAL_PROVIDER_LOGO` undefined** — same reason. We use
   `GUID_NULL` instead; the tile uses the default logo.
3. **`__ImageBase` undefined in `GetModuleFileNameW`** — because we call it
   directly from `DllRegisterServer`, not via a helper. Leave the
   `EXTERN_C IMAGE_DOS_HEADER __ImageBase;` at end of `dll.cpp`.
4. **`__try/__except` + C++ objects** — MSVC rejects it; use `try/catch`
   instead (already done).
5. **"Parameter is incorrect" from LogonUI** — two separate bugs:
   - `UNICODE_STRING.Buffer` inside the serialization must be an **offset** (in
     bytes from the start of the buffer), NOT an absolute pointer. LSA does the
     fixup across process boundaries.
   - Authentication package: use `"Negotiate"` (NEGOSSP_NAME_A), not
     `"Kerberos"`. Negotiate auto-picks Kerberos vs NTLM and works for local
     accounts. Do NOT fall through to `pkgId = 0` on lookup failure — return
     `HRESULT_FROM_NT(status)`.
6. **GUID must be unique** — replace `CLSID_FaceCredentialProvider` in `guid.h`
   with a freshly generated one (`uuidgen`) before distributing. It is also
   hardcoded in `register.ps1`; keep the two in sync.

## 5. Runtime behaviour

### Configuration

There is no config file after install, and none is needed — the service runs on
the defaults compiled into `face_service/config.py`.

**[`config.example.toml`](config.example.toml) is the single reference for every
setting**: it lists all 45 keys with their real defaults and a comment on each.
It is not a template you must copy and it is not allowed to drift — a self-test
(`tools/config_example_selftest.py`) fails the moment a key or a value there
stops matching the code.

To customise, copy it to `%USERPROFILE%\.face-unlock\config.toml` and delete
everything you do not want to change. Missing keys keep their defaults; unknown
keys are ignored.

Three settings are worth understanding before you touch anything else:

- **`liveness_mode`** — `fast` asks for a live challenge (a blink or a head
  gesture) only when the match is in doubt, so a confident unlock is
  sub-second. `paranoid` demands the gesture every single time: slower, and the
  strongest defence against a replayed video.
- **`auto_lock`** — turn it off to run presence detection in observe-only mode.
  Absences are still counted and visible in Status, but the workstation is never
  locked. Face sign-in works either way. Use this while tuning the presence
  settings so a false absence cannot lock you out mid-sentence.
- **`persistent_camera`** — on, the camera handle stays open between requests:
  verification is roughly three times faster, but the webcam LED stays lit. Off,
  the device is opened per request: the LED only lights while verifying, at the
  cost of a slower unlock.

After editing, restart both processes:

```powershell
powershell -ExecutionPolicy Bypass -File tools\clean_restart.ps1
```

### Scheduled tasks

`setup.ps1` registers **three** logon tasks. They are declared in
`tools\tasks.psd1` and created by `tools\register_tasks.ps1`, which is the only
script that knows about them:

- **FaceUnlock-Service** — the Python pipe server (`\\.\pipe\FaceUnlock`)
- **FaceUnlock-Presence** — the tray app and the presence probe
- **FaceUnlock-Watchdog** — pings the service and restarts it if it hangs

To re-register them all after changing the venv or moving the repo:

```powershell
.\tools\register_tasks.ps1 -Action Register
```

### First sign-in after a reboot uses your PIN

This is by design, not a fault. All three tasks are **logon** tasks, so the
service does not exist until you have already signed in once. The very first
sign-in after any reboot — and after any sign-out — is therefore PIN or
password. Face sign-in is available from the next lock onward.

### Lock-screen behaviour

The face tile is the default tile, but it does **not** start scanning by itself.
`FaceCredential::SetSelected` returns `pbAutoLogon = FALSE`, so selecting the
tile does nothing until you press the submit arrow; and
`FaceCredentialProvider::GetCredentialCount` only sets
`pbAutoLogonWithDefault = TRUE` when a verification result is already waiting.
An idle lock screen never wakes the camera on its own.

Safety: `GetSerialization` has a hard 12-second timeout and returns `S_FALSE` on
failure, so a bad verify cannot lock you out of the password tile.

### Environment variables

`face_service/__main__.py` sets thread caps so the runtime does not spawn worker
processes that compete for the pipe:

```
OMP_NUM_THREADS=1
TF_NUM_INTRAOP_THREADS=1
TF_NUM_INTEROP_THREADS=1
```

**`CUDA_VISIBLE_DEVICES` is deliberately NOT set.** Setting it to `-1` hides
every GPU and the engine silently falls back to CPU. An earlier version of this
project set it; removing it was the fix that made GPU inference work. If you
have it in your environment, unset it.

### Remote-session exclusion

`presence_monitor/remote_session.py` skips auto-lock when:

- an RDP session is active (`GetSystemMetrics(SM_REMOTESESSION)`)
- a known remote-control process holds an ESTABLISHED external TCP connection
  (UltraViewer, AnyDesk, RustDesk, Parsec, Chrome Remote Desktop, Splashtop)
- a process name matches one of the tools that only run during active sessions
  (TeamViewer_Desktop.exe, Quick Assist, MSRA)

Tune the list in that file if your remote tool is missing.

## 6. If you also have `facewinunlock-tauri` installed

Run the disable script once (as Administrator) to turn off its autostart, kill
its processes, and remove its Credential Provider registrations (backed up
first):

```powershell
tools\disable_tauri.ps1
```

Backup lives at `%USERPROFILE%\face-unlock-backup\` — `reg import` those files
to restore if needed.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot open camera index 0` | Close other camera apps; Teams/Zoom/UltraViewer can hold the device. Try a different `camera_index`. |
| Enrollment says "No face found" on all images | Lighting too dim, or you weren't centred. Re-run `enroll capture --count 20`. |
| Unlock refused with "too dark" | The low-light gate fired. Add light; the exposure boost only rescues borderline scenes. |
| Verify is slow on the first call | Models didn't pre-warm. Check `service.log` for the warmup line, and confirm `warmup_on_start` is on. |
| Verify is slow on *every* call | The CUDA provider fell back to CPU. `service.log` logs the effective providers at startup. Check `CUDA_VISIBLE_DEVICES` is unset. |
| Two `python.exe` processes for one service | Normal — the venv launcher spawns the real interpreter. Only the inner one runs our code. |
| `Parameter is incorrect` on lock screen | You're running an old DLL. Rebuild into `build-cp` (§4) and lock/unlock once to reload. |
| Lock screen hangs for ~12 s | FaceService is down. `Start-ScheduledTask FaceUnlock-Service`. |
| User stuck, can't reach password | Click "Sign-in options" on the lock screen → pick the Password tile. Or boot into Safe Mode; third-party CPs are disabled there. |

## 8. Logs

- `%USERPROFILE%\.face-unlock\service.log` — FaceService
- `%USERPROFILE%\.face-unlock\presence.log` — PresenceMonitor
- `%USERPROFILE%\.face-unlock\audit.jsonl` — one record per verify / unlock /
  challenge, when `audit_log` is on
- Event Viewer → Applications and Services Logs → Microsoft → Windows → User
  Profile Service / Authentication — for LogonUI and LSA errors when debugging
  Credential Provider issues

## 9. Uninstall completely

```powershell
# Admin PowerShell, from the repo root
.\credential_provider\register.ps1 -Action unregister
.\tools\register_tasks.ps1 -Action Unregister
Remove-Item -Recurse -Force "$env:USERPROFILE\.face-unlock"
Remove-Item -Recurse -Force .\.venv
```

`register_tasks.ps1 -Action Unregister` walks the same `tasks.psd1` used to
create the tasks, so all three go, including the watchdog.

Optionally also remove the downloaded recognition models:

```powershell
Remove-Item -Recurse -Force "$env:USERPROFILE\.insightface"
```

The repo can then be deleted.
