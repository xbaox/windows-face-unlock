# Windows Face Unlock

Sign in to the Windows lock screen with your face, using an ordinary webcam. Optionally, lock the PC
when you walk away. Everything runs on your own computer; nothing is sent to a cloud service.

| | |
|---|---|
| Version | 0.2.0 (not released yet -- see [Status](#status)) |
| Windows | Windows 11 24H2 / 25H2 x64 (supported). Windows 10 22H2 and Windows 11 23H2 install, best effort, untested. Not ARM64. |
| Camera | Any ordinary RGB webcam (no infrared or depth camera needed or used) |
| Languages | English, Russian |
| License | MIT for this project; third-party components under their own licenses ([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)) |

## What it is -- and what it is not

**Face Unlock is a convenience feature, not a security boundary.** Your PIN and password stay the
real credentials, and they always keep working: the face tile is an extra tile on the lock screen,
never a replacement, and every failure falls back to them.

An ordinary webcam sees a flat colour picture. It cannot measure depth, and there is no infrared
sensor and no hardware-backed key. So this is **not Windows Hello** and cannot give Hello's
guarantees. What Face Unlock adds on top of a plain face match:

- **Two random head movements on every unlock** (for example "turn your head left, then nod"),
  performed by the face that matched; frames of any other face are ignored. Your head has to be
  still when the prompt appears.
- A passive check that flags a phone or monitor held up to the camera.
- Face sign-in is locked for 5 minutes after 5 failed attempts. The PIN and password are never
  affected.

It does **not** stop a 3-D mask, a determined attacker who already has your unlocked session, or
malware running under your account. A printed photo moved by hand is a known limitation of any
RGB-only system. The details, with measured numbers, are in [SECURITY.md](SECURITY.md) -- read it
before you rely on this.

## Features

- **Lock-screen tile** (a Windows Credential Provider). Press the arrow, look at the camera, do the
  two movements shown on the tile. Clear messages when something is wrong: no password saved, no
  face set up, camera busy, too dark, locked for N seconds, password rejected by Windows, service
  not running.
- **Setup wizard** with a live camera preview, camera choice by name, automatic capture, a quality
  check, a one-person check, and a short calibration of the turn direction for your camera.
- **Password dialog** that checks the password with Windows before saving it (the same kind of
  sign-in the lock screen does), stored encrypted for your Windows account only (DPAPI).
- **Walk-away lock (optional, off by default)**: the tray checks the camera on an interval and locks
  the PC after your face has been missing for a few checks. It never locks while you are being
  helped over Remote Desktop, TeamViewer, Chrome Remote Desktop, Quick Assist or Windows Remote
  Assistance, and it treats "camera busy / covered / unplugged" as *unknown*, never as "present".
- **Tray icon** with Status, Settings (Basic and Advanced), pause/resume, update check and help.
- **The camera is used only when needed**: during a sign-in attempt, the setup wizard, or a
  presence check. When you lock the PC the camera is warmed up for up to 60 seconds so the unlock
  is quick, then released.
- **Update check** once a day against the GitHub releases page (can be turned off). It only tells
  you; it never downloads or installs anything by itself.

## Status

0.2.0 is the Stage-9 rework of this project. It has **not been released**: there is no published
installer yet, and the build is not code-signed until the signing step of the release process is in
place. Unsigned builds are test builds; on a PC with **Smart App Control** turned on they will not
run (and the lock-screen tile will not load) until a signed release exists.

## Install

See **[INSTALL.md](INSTALL.md)** -- requirements, the installer (including the one-time download of
the face-recognition models, which needs your consent), first-time setup, everyday use,
troubleshooting and uninstall.

In short: run `WindowsFaceUnlock-Setup-0.2.0-cpu.exe` as an administrator (or the `-gpu` variant if
you have an NVIDIA graphics card), accept the model terms, then on the last page save your Windows
password and set up your face.

## Limits you should know about

- **One Windows user per PC.** The person signed in at the console during installation is the
  owner; only the owner gets the face tile. Other accounts sign in as usual.
- **Lock screen only.** No face sign-in in UAC/credential prompts or inside Remote Desktop.
- **After a restart or sign-out, sign in once with your PIN or password.** Face Unlock runs inside
  your session, so it starts after you sign in; from the next lock on, the face tile is there.
- **You need a Windows password** that you know. Accounts that sign in without a password
  (passwordless Microsoft accounts, "Windows Hello only") cannot use face sign-in.
- **Microsoft Entra ID (work/school) accounts**: implemented, not yet verified.
- **Screen readers**: the windows are built with Tk, which has limited screen-reader support.
  Keyboard use works (Tab, Enter, Escape).

## Privacy

Your face photos, face templates (numbers derived from your face), the encrypted Windows password,
settings and logs stay in `%USERPROFILE%\.face-unlock` on your PC. The only network traffic is the
one-time model download during installation and the daily update check (turn it off in Settings).
ONNX Runtime's own telemetry is switched off. Full details: [SECURITY.md -- Privacy](SECURITY.md#privacy).

## How it works

| Component | Runs as | Role |
|---|---|---|
| `face_service.exe` (`face_service`) | you, in your session | camera, face recognition and the movement check; answers the lock-screen tile over a local pipe |
| `face_unlock_tray.exe` (`presence_monitor`) | you, in your session | tray icon, Settings, setup wizard, password dialog, optional walk-away lock |
| `face_unlock_watchdog.exe` | you, in your session | restarts the service if it stops answering |
| `FaceCredentialProvider.dll` | Windows' sign-in screen (SYSTEM) | the lock-screen tile; asks the service and submits your saved password to Windows |

The lock screen runs as SYSTEM in a separate session and cannot open your camera, so the tile is a
small C++ component that talks to the service over a named pipe. The pipe accepts local callers
only (network access is denied), the service answers only the owner and the sign-in screen, and it
releases the password only to the sign-in screen, only after the face and both movements passed.
Recognition uses InsightFace models (ArcFace) through ONNX Runtime; face detection for the presence
check uses OpenCV's YuNet.

## Developers

Building from source, the development layout, tests and the release pipeline:
[CONTRIBUTING.md](CONTRIBUTING.md), [installer/README.md](installer/README.md),
[credential_provider/README.md](credential_provider/README.md) and
[credential_provider/SIGNING.md](credential_provider/SIGNING.md).

## Credits

- This project started as a fork of **[caochitam/windows-face-unlock](https://github.com/caochitam/windows-face-unlock)**
  by Cao Chí Tâm, whose MIT copyright notice is kept in [LICENSE](LICENSE). Thank you.
- [deepinsight/insightface](https://github.com/deepinsight/insightface) -- the recognition and
  landmark models (non-commercial research license, downloaded with your consent) and library (MIT).
- [OpenCV](https://opencv.org/) and the OpenCV Zoo YuNet face detector (MIT).
- [ONNX Runtime](https://onnxruntime.ai/) (MIT).
- [boltgolt/howdy](https://github.com/boltgolt/howdy) -- the idea of face sign-in with a thin
  sign-in module talking to a user-space service.
- Microsoft's [Credential Provider samples](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider)
  -- the reference for the `ICredentialProvider` interfaces.
- Portions of this software are copyright © The FreeType Project (www.freetype.org). This software
  is based in part on the work of the Independent JPEG Group. (Both via Pillow.)
- Every other component and its license: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT -- see [LICENSE](LICENSE). The face-recognition models are **not** covered by it: they are
InsightFace's, for non-commercial research use, and are downloaded from InsightFace's own release
only after you accept those terms.
