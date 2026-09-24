# Face Unlock Credential Provider (C++)

A minimal Windows Credential Provider that — when the user selects its tile on
the logon / lock screen — connects to the Python `FaceService` over a named
pipe (`\\.\pipe\FaceUnlock`), asks it to perform face verification, receives
the stored Windows credentials (decrypted via DPAPI in the user context), and
hands them to LogonUI through a standard
`KERB_INTERACTIVE_UNLOCK_LOGON` serialization.

## Build (Visual Studio 2022)

Prerequisites:
- Visual Studio 2022 with **Desktop development with C++** workload
- CMake 3.20+
- Windows SDK 10 (any recent version)

```powershell
# from the repo root
cmake -S credential_provider -B build-cp -A x64
cmake --build build-cp --config Release
```

Output: `build-cp\Release\FaceCredentialProvider.dll`.

Since Stage 7 the installed layout loads the DLL from
`C:\Program Files\WindowsFaceUnlock\credential_provider\`, and since Stage 8b
`register.ps1` **refuses** (exit 4) to register a DLL whose file or any parent
folder ordinary users can write to — `build-cp\Release` under a repo checkout is
such a folder. LogonUI loads this DLL as SYSTEM, so for a dev registration copy the
build into an admin-only folder and pass `-DllPath`.

## Register (Administrator PowerShell)

```powershell
.\register.ps1 -Action register
```

To remove: `.\register.ps1 -Action unregister`.

Registration writes:
- `HKCR\CLSID\{8414D7B6-D536-461B-B31B-ADF77B3A8974}` — COM class
- `HKCR\CLSID\{8414D7B6-D536-461B-B31B-ADF77B3A8974}\InprocServer32` — DLL path, Apartment threading
- `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{8414D7B6-D536-461B-B31B-ADF77B3A8974}`
  — enables the CP in LogonUI

After (un)registering, lock the workstation (Win+L) and the new "Face Unlock"
tile should appear.

## How it talks to the Python service

1. User selects the Face Unlock tile on lock screen.
2. `SetSelected` returns `*pbAutoLogon = FALSE`, so the scan does NOT start
   automatically; the user presses the submit arrow, which triggers
   `GetSerialization`.
3. `GetSerialization` starts a worker thread and returns at once; the worker opens
   `\\.\pipe\FaceUnlock` (checking that the server runs as the user or SYSTEM,
   identification-level only), sends `{"cmd":"unlock"}`, and is bounded by 12 s
   (15 s for the gesture round), the connect wait included.
4. `FaceService` performs the camera capture, the InsightFace recognition
   check and liveness, decrypts the DPAPI password blob, and returns
   `{"ok":true,"username":"...","password":"...","domain":"..."}`. When
   liveness wants an active gesture it answers `needs-gesture` with a
   single-use token instead, and the tile completes the round with
   `unlock_gesture` before any credential is released.
5. The CP packs those into a `KERB_INTERACTIVE_UNLOCK_LOGON` and returns
   `CPGSR_RETURN_CREDENTIAL_FINISHED`. LogonUI performs the actual logon.

## Important caveats

- **The DLL runs inside `LogonUI.exe` under the `SYSTEM` account.** Because
  the DPAPI blob is encrypted with the *user* key, the Python service (which
  runs in the user session) is the one that decrypts it and passes plaintext
  over the pipe — the CP itself never touches DPAPI. The pipe is local-only
  and carries an explicit security descriptor: `SELF` and `SYSTEM` only, no
  `Everyone` ACE, plus a medium integrity label. On top of that the `unlock`
  command is refused unless the caller's token SID is `SYSTEM` (`S-1-5-18`),
  which is what LogonUI loads this DLL as. Understand that the plaintext
  password still crosses the pipe before deploying widely.
- This is a **skeleton**: no custom tile bitmap, no localisation, no progress
  UI while the service captures frames, and only the single "unlock /
  interactive logon" scenario is implemented. The Microsoft
  [SampleCredentialProvider](https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/CredentialProvider)
  is a good reference for polishing.
- The `CLSID_FaceCredentialProvider` GUID in `guid.h` is this fork's own
  (`{8414D7B6-…}`, generated in Stage 0). A fork of this repository must generate
  its own (`uuidgen.exe`) before publishing builds.
- A failed scan never blocks LogonUI: the tile shows one of four fixed messages
  (not recognised / temporarily locked / service unavailable / stored password
  rejected) and the PIN and password tiles stay available. After Windows rejects
  the stored password, the tile stops scanning for the rest of that lock-screen
  session (Stage 8b, F-04) — re-save the password in Face Unlock.
