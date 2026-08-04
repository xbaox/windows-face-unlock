# Windows Face Unlock — face login + presence auto-lock for Windows 10/11

**An open-source Windows face recognition login and walk-away auto-lock —
the kind of "Howdy for Windows" / Windows Hello alternative that stays on
your machine, runs on any off-the-shelf webcam, and you can actually read
the source of.**

Keywords: *windows face unlock, windows face login, face recognition
windows, webcam login windows, howdy windows, windows hello alternative,
credential provider face, insightface windows, arcface windows, auto lock
when away, presence monitor, walk-away lock, face id for pc.*

| | |
|---|---|
| Platform | Windows 10 / 11 x64 |
| Python   | 3.11 or 3.12 |
| License  | MIT |
| Models   | InsightFace `buffalo_l` (ArcFace ONNX) + YuNet (OpenCV Zoo) |
| Runtime  | ONNX Runtime — CUDA when available, CPU otherwise |

## What this is, and what it is not

Read this before you decide to rely on it.

This is a **convenience-grade** face login with **real** anti-spoofing. It runs
on an ordinary RGB webcam, which means it has **no infrared sensor and no TPM
binding** — so it is *not* Windows Hello and cannot be. Hello's security
guarantee comes from IR depth sensing plus hardware-backed key storage;
neither exists here.

What it does have:

- an **active** liveness challenge (blink or head gesture), not just a passive
  photo check, and the gesture must be performed **by the face that matched**
- a passive anti-screen check that flags a display being held up to the camera
- a hardened local IPC channel, and credential release gated to `SYSTEM`

What that buys you: a flat photo, a phone screen and a recorded video all fail.
What it does not buy you: resistance to a 3-D mask, to a determined attacker
with your unlocked machine, or to malware already running as you.

**Your PIN and password always keep working.** The face tile is an addition to
the lock screen, never a replacement, and every failure path falls back to
them. Treat face sign-in as convenience; treat the PIN as the real credential.

> **First sign-in after any reboot is your PIN — by design.** All three
> background tasks are *logon* tasks, so the service does not exist until you
> have signed in once. Face sign-in is available from the next lock onward.

## Features at a glance

- **Log in with your face** from the Windows lock screen via a proper
  Credential Provider tile (C++ DLL), not a user-mode hack.
- **Walk-away auto-lock**: the tray process probes the webcam on an interval;
  if your enrolled face isn't there for N consecutive probes,
  `LockWorkStation()` fires. Can be set to observe-only.
- **Two presence modes**: strict (must match the enrolled face, blocks
  strangers) or lightweight (any face is enough, replaces old AutoFaceLock
  scripts).
- **Active liveness**: blink detection and randomised head-pose gestures
  (turn left / turn right / nod) on InsightFace landmarks, plus a passive
  anti-screen (moiré / glare) check.
- **Two liveness modes**: `fast` asks for a gesture only when the match is in
  doubt; `paranoid` demands one on every unlock.
- **Remote-session aware**: skips auto-lock when the session is RDP, or
  when TeamViewer / AnyDesk / RustDesk / Parsec / Chrome Remote Desktop /
  Quick Assist / UltraViewer hold an active remote connection.
- **Managed from the tray**: live status dashboard, settings editor,
  guided enrollment wizard with live camera preview + auto-capture, one
  Quit button that actually stops everything.
- **12-language UI** — English, Tiếng Việt, 中文, Español, Français,
  Deutsch, 日本語, 한국어, Русский, Português, العربية, हिन्दी. Switch
  from the tray, applies live.
- **DPAPI-encrypted** Windows password storage (user scope).
- **Rate limiting**: consecutive face failures trigger a temporary face
  lockout. PIN and password are never locked out.

## Install (end user)

Grab the latest installer from the
[**Releases page**](https://github.com/caochitam/windows-face-unlock/releases)
and double-click it. Requires Windows 10/11 x64, admin rights, any webcam.

- The installer bundles Python, the ONNX runtime, the YuNet detector **and** the
  `buffalo_l` recognition models (~325 MiB unpacked), so a freshly installed
  machine signs in offline. Only a dev/source checkout still fetches the models
  on first warmup.
- Not yet code-signed, so SmartScreen will say **"Unknown publisher"**.
  Click **More info → Run anyway**. Signing application to SignPath is in
  progress — when approved the signed installer will replace unsigned.
- On first launch, open the tray icon → **Enroll face** and capture ~15
  photos, then **Set Windows password** to enable lock-screen unlock.
- Updates are checked automatically against GitHub Releases; a prompt
  appears when a new version is available. You can also trigger a check
  from the tray menu (*Check for updates…*).

Building the installer from source: see
[`installer/README.md`](installer/README.md).

## Architecture

An open-source, auditable replacement for closed-source webcam-login utilities
(like `facewinunlock-tauri`). Three cooperating components:

| Component             | Language | Runs as                 | Role                                                                 |
|-----------------------|----------|-------------------------|----------------------------------------------------------------------|
| `face_service`        | Python   | User session (always)   | Camera + recognition + liveness + DPAPI; exposes a named pipe.       |
| `presence_monitor`    | Python   | User session (tray)     | Probes presence on an interval; if absent → `LockWorkStation()`.     |
| `credential_provider` | C++      | LogonUI (SYSTEM)        | Windows Credential Provider tile that calls the service on unlock.   |

Plus two CLI tools: `tools.enroll` (capture reference photos) and
`tools.set_password` (store your Windows password encrypted with DPAPI).

### Why three pieces?

Windows lock-screen authentication runs in an isolated session as `SYSTEM`,
which cannot comfortably load a full inference stack or open the webcam. The
C++ Credential Provider is therefore a thin shim that talks to the Python
service over a local named pipe. This is the same pattern Howdy uses on Linux
with PAM.

### The unlock handshake

The lock-screen tile does not simply ask "is this you?" and take the answer:

1. The Credential Provider sends `unlock`.
2. If recognition matches and liveness is satisfied, credentials come back.
3. If liveness wants proof of life, the service instead answers
   `needs-gesture` with a randomly chosen gesture, a short-lived single-use
   token, and a prompt to display.
4. The tile performs the gesture round and replies `unlock_gesture` with that
   token. Frames that do **not** match the enrolled face are dropped rather
   than fed to the gesture detector, so the gesture cannot be performed by
   somebody else standing next to you.
5. Only then are credentials released.

A token is single-use and expires in seconds, so one abandoned at the lock
screen is worthless.

### The channel

The named pipe is not open to the world:

- an explicit security descriptor grants **`SELF` and `SYSTEM` only**, plus a
  medium integrity label — there is no `Everyone` ACE
- `FILE_FLAG_FIRST_PIPE_INSTANCE` makes the service refuse to start if the pipe
  name is already taken, so a squatter cannot impersonate it, and the client
  verifies the server's SID before sending
- the `unlock` command is **gated to `SYSTEM`** (`S-1-5-18`), the account
  LogonUI loads the Credential Provider as. Any other caller is refused with
  `not-authorized` before the password blob is ever touched

## Requirements

- Windows 10 / 11 x64
- Python 3.11 or 3.12
- Webcam
- (For Credential Provider) Visual Studio 2022 + CMake
- (Optional) An NVIDIA GPU. Recognition uses the CUDA execution provider when
  one is present and falls back to CPU with a warning otherwise.

## Install (Python parts)

```powershell
# From this folder, in PowerShell
.\setup.ps1
```

This creates `.\.venv`, installs dependencies, and registers the three Task
Scheduler jobs below. It deliberately does **not** write a config file: with no
`config.toml` the service runs on the defaults compiled into the code, which
cannot go stale. To customise, copy
[`config.example.toml`](config.example.toml) to
`%USERPROFILE%\.face-unlock\config.toml` and keep only the keys you change.

### Scheduled tasks

Three logon tasks, declared in `tools\tasks.psd1` and created by
`tools\register_tasks.ps1` — the only script that knows their names:

| Task | Role |
|---|---|
| `FaceUnlock-Service`  | the pipe server |
| `FaceUnlock-Presence` | the tray icon and the presence probe |
| `FaceUnlock-Watchdog` | pings the service and restarts it if it hangs |

## Enroll your face + store password

```powershell
.\.venv\Scripts\python -m tools.enroll capture --count 15
.\.venv\Scripts\python -m tools.set_password
```

Captured frames are quality-gated on detector confidence, sharpness and
exposure, so blurry or badly lit ones are dropped rather than stored.

Rebuild embeddings any time with:

```powershell
.\.venv\Scripts\python -m tools.enroll build
```

## (Optional) Enable the lock-screen tile

See [credential_provider/README.md](credential_provider/README.md). You will
need Visual Studio 2022. Without this, the presence-lock still works — you
just unlock with your PIN or password like usual.

## Quick test (no Credential Provider needed)

```powershell
# Terminal 1
.\.venv\Scripts\python -m face_service

# Terminal 2
.\.venv\Scripts\python -m presence_monitor
```

Trigger a manual verification probe:

```powershell
# Named-pipe one-shot client from PowerShell
$p = New-Object IO.Pipes.NamedPipeClientStream('.', 'FaceUnlock', 'InOut')
$p.Connect(5000)
$w = New-Object IO.StreamWriter($p); $w.AutoFlush = $true
$r = New-Object IO.StreamReader($p)
$w.Write('{"cmd":"verify"}'); $p.WaitForPipeDrain()
$r.ReadToEnd()
```

`unlock` will refuse this client with `not-authorized` — that is the SYSTEM
gate doing its job. Use `verify` to check recognition health.

## Configuration reference

[`config.example.toml`](config.example.toml) is the single reference: it lists
every setting with its real default and a comment on each, and a self-test
fails the build if it ever disagrees with the code. Start there. The knobs
worth understanding first are `liveness_mode`, `auto_lock` and
`persistent_camera`.

## Security notes

Read these before trusting the CP for daily unlock:

1. The stored Windows password is encrypted with **DPAPI user-scope**. That
   protects it from other users and from offline disk inspection, but **not**
   from malware running as you. If your attacker model includes that, use a
   smart card or Windows Hello proper.
2. Liveness blocks flat photos, phone screens and replayed video, but **not
   3-D masks**. See "What this is, and what it is not" above.
3. The credential provider skeleton uses a hard-coded GUID from this repo —
   **generate your own** before sharing builds.
4. The lock-screen call has a hard 12-second timeout and fails closed to the
   password tile, so a hung or missing service can never lock you out.

## Credits / prior art

This project draws on ideas from:

- [boltgolt/howdy](https://github.com/boltgolt/howdy) — Linux/PAM face login
- [deepinsight/insightface](https://github.com/deepinsight/insightface) — the
  recognition and landmark models
- [ageitgey/face_recognition](https://github.com/ageitgey/face_recognition) — dlib wrapper
- Microsoft's [SampleCredentialProvider](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider)
  — reference implementation for `ICredentialProvider`

## Contributing

Issues and PRs are welcome. Before sending a PR please:

- Run `python -m tools.bench` if you touched the recognizer / camera paths.
- Keep new UI strings translatable — add keys to
  [`face_service/i18n.py`](face_service/i18n.py) under all 12 languages
  (English fallback is automatic if a key is missing).
- Generate your own GUID in `credential_provider/guid.h` if you're going
  to register the CP DLL on your machine.

## License

MIT — see [LICENSE](LICENSE).
