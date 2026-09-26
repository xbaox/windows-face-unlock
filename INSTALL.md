# Installing and using Windows Face Unlock

This guide is for people who install the program. Building from source is in
[CONTRIBUTING.md](CONTRIBUTING.md). Security and privacy: [SECURITY.md](SECURITY.md).

## 1. Before you start

| You need | Notes |
|---|---|
| Windows 11 24H2 or 25H2, 64-bit (x64) | Supported. Windows 10 22H2 and Windows 11 23H2 install and should work, but are untested ("best effort"). Older Windows and ARM64 PCs are refused by the installer. |
| Administrator rights | To install and to uninstall. |
| An ordinary webcam | Built-in or USB. Put it at eye level, facing you. No infrared camera is needed or used. |
| A Windows **password** you know | Face sign-in submits your saved password to Windows. Accounts that sign in without any password cannot use it. |
| Internet during installation, once | To download the face-recognition models (about 275 MiB) -- or a copy of `buffalo_l.zip` on disk (see [5](#5-installing-without-internet-or-silently)). |
| Disk space | About 1 GB for the CPU variant; several GB for the GPU variant. |
| Smart App Control **off** | Until a signed release exists. With Smart App Control on, the unsigned programs are blocked and the lock-screen tile does not load. |

**Which installer?** `WindowsFaceUnlock-Setup-0.2.0-cpu.exe` works on every x64 PC and is the one
to pick if unsure. `...-gpu.exe` adds NVIDIA's CUDA libraries and uses an NVIDIA graphics card for
recognition; it is much larger. Each installer has a `.sha256` file next to it -- you can compare it
with `Get-FileHash <file> -Algorithm SHA256` in PowerShell.

**One Windows user per PC.** Face Unlock belongs to one account: the user signed in at the
computer's screen when you run Setup (not the administrator account you may type in the UAC
prompt). Setup shows who that is before it installs. Other accounts on the PC sign in exactly as
before and never see the face tile.

## 2. Installing

1. Sign in to Windows as the person who will use face sign-in.
2. Run the installer and approve the administrator prompt. Setup installs into
   `C:\Program Files\WindowsFaceUnlock` (the location is fixed).
3. **Face-recognition models.** The recognition models are made by InsightFace and are licensed
   *for non-commercial research purposes only*. They are not part of this program. Setup shows
   those terms and asks you to accept them; then it downloads the official `buffalo_l.zip` from
   InsightFace's own release page, checks its size and SHA-256 fingerprint, and unpacks the five
   model files into the program folder. You can instead choose a `buffalo_l.zip` you already have
   (the same check applies). If you do not accept, Setup cannot continue. When you upgrade and valid
   models are already installed, this page is skipped.
4. **"Sign in to Windows with your face"** is ticked by default. It registers the
   lock-screen tile. Your PIN and password tiles stay; untick it if you only want walk-away lock.
5. Setup registers three background tasks for you (service, tray, watchdog) and starts them.
6. On the last page, keep both boxes ticked:
   - **Save your Windows password** -- type it once; Windows checks it before it is saved (see
     [3.1](#31-your-windows-password)).
   - **Set up your face** -- the wizard (see [3.2](#32-setting-up-your-face)).

If you are upgrading, Setup stops the running copy first, keeps your face profile, password and
settings, and starts the new version. If you cancel an upgrade half-way, the old version is started
again.

## 3. First-time setup

A tray icon (a face) appears in the notification area. Everything below is also in its menu.

### 3.1 Your Windows password

Tray → **Windows password…**. Enter your normal Windows sign-in password -- for a Microsoft account,
the password of that account, not your PIN.

- Before saving, the dialog tries a real Windows sign-in with it. A wrong password is **not
  saved** (a wrong saved password would cost you a failed sign-in every time you unlock).
- If Windows cannot give a clear answer (this happens with some Microsoft accounts and when a domain
  controller is not reachable), the password is saved with a yellow warning. If it turns out to be
  wrong, the lock-screen tile will say "Windows rejected the saved password" -- save it again.
- If Windows is set to "passwordless" (Settings → Accounts → Sign-in options → *For improved
  security, only allow Windows Hello sign-in*), face sign-in cannot work; the dialog explains how to
  turn that off.
- **After you change your Windows password, save the new one here too.**

The password is encrypted with Windows DPAPI for your account only and kept in your profile folder.

### 3.2 Setting up your face

Tray → **Set up face…**.

1. Pick your camera from the list (it is remembered by name).
2. Sit about an arm's length from the camera, face it, in normal light.
3. Press **Start** when the preview shows you. The wizard takes a series of photos by itself; move
   your head slightly between them.
4. Only photos that are sharp, well lit and clearly of the same person are kept. If too few pass, the
   wizard tells you why -- fix the light and try again.
5. **Calibration**: turn your head to the left when asked. This teaches Face Unlock which way "left"
   is for your camera (some cameras show a mirrored picture).
6. At the end the wizard checks that everything is ready: password saved and accepted, face profile
   loaded by the service, data folder secured. Each problem has a button to fix it.

To start over later, open the wizard again in **Replace** mode (the old profile is kept until the new
one is complete), **Add** more photos to the current profile, or **Delete face profile**. The
calibration can be repeated with **Calibrate head turn**.

## 4. Everyday use

### 4.1 Unlocking

Lock the PC (Windows+L or walk away). On the lock screen:

1. Select the **Face Unlock** tile and press the arrow.
2. Look at the camera. The tile says what to do, for example **"Turn your head left, then nod."**
   Keep your head still until the instruction appears, then do the two movements in that order.
3. Windows signs you in.

If it does not work, the tile says why, and the PIN and password tiles are always there:

| The tile says | What to do |
|---|---|
| Face not recognised. Try again or use PIN or password. | Try again facing the camera; if it keeps happening, set up your face again. |
| Face sign-in is locked for N s. | 5 failed attempts in a row lock face sign-in for 5 minutes. Use your PIN. |
| No Windows password is saved in Face Unlock. | Sign in with your PIN and save it (3.1). |
| Windows rejected the saved password. | Your password changed or was saved wrong. Sign in with your PIN and save it again. |
| No face is set up yet. | Run the setup wizard (3.2). |
| The camera is busy or not responding. | Close apps using the camera (Teams, Zoom, Camera) or reconnect it. |
| Too dark to recognise your face. | Add light. |
| Face Unlock service is not running. | See 6.1. |
| Face Unlock components do not match. Update Face Unlock. | Run the latest installer again. |
| Face Unlock needs attention. | Sign in with your PIN and open the tray icon: Status shows the reason. |

**After a restart or sign-out, the first sign-in is always PIN or password.** Face Unlock runs in
your Windows session, so it starts only after you sign in. From the next lock on, the face tile is
there. (A restart from the Start menu with "Use my sign-in info to automatically finish setting up
after an update or restart" enabled signs you in automatically and locks; face sign-in then works
right away.)

Face sign-in is available only on the lock screen and the sign-in screen of the owner's session:
not in UAC or other credential prompts, and not inside a Remote Desktop session.

### 4.2 Walk-away lock (optional)

Off by default. Turn it on in **Settings → Basic → Walk-away lock**. The tray then checks the camera
every 60 seconds and locks the PC after your face has been missing on two checks in a row (both
numbers are in Settings → Basic).

- Settings → Advanced → presence: **Your face** (default) locks when *your* face is not there;
  **Any face** only asks whether *a* face is there.
- If the camera cannot give an answer -- busy in another app, unplugged, covered, too dark to see
  anything -- the result is **unknown**: it neither locks nor counts as present. Status shows why.
- A very dim room can be taken as "nobody there". Keep Windows' own screen-lock timeout as a
  backstop.
- It never locks while the session is controlled remotely (Remote Desktop, TeamViewer, Chrome Remote
  Desktop, Quick Assist, Windows Remote Assistance).
- **Pause presence checks** in the tray stops it until you resume (kept across restarts).

### 4.3 The tray menu

- **Status…** -- whether the service is running and serving, face profile, password, camera,
  presence, watchdog, recent events and messages.
- **Settings…** -- *Basic*: language, walk-away lock and its interval and strike count, the face
  sign-in check (4.4), camera, notifications, update check. *Advanced* (collapsed): the match
  threshold, the screen check, attempts before the face lockout and its length, presence details,
  low-light brightening. Changes apply when you press Save.
- **Check presence now**, **Pause / Resume presence checks**.
- **Set up face…**, **Windows password…**.
- **Open log folder** -- opens only the `logs` folder (not your face photos or password).
- **Language** -- English or Русский.
- **Check for updates…** -- asks GitHub for the newest release and tells you. Nothing is downloaded
  or installed automatically; the automatic check runs at most once a day and can be turned off in
  Settings.
- **Help…**, **Quit Face Unlock** (asks first).

**Quit** stops the tray and the service. The walk-away lock stays off until you sign in again; the
watchdog brings the service back after 5 minutes so the lock-screen tile keeps working.

Notifications (update found, face sign-in locked, service stopped) appear as Windows notifications
from "Windows Face Unlock", and also in Status → recent events.

### 4.4 Liveness modes

Settings → Basic → **Face sign-in check**:

- **Head movements every time (safer)** -- the default for new installations: every unlock needs
  the face match *and* two random head movements.
- **Head movements only when in doubt (faster)** -- the movements are asked for only when the match
  is not clearly strong or a screen is suspected. Quicker, but a good photo or video of you is
  easier to use against it. See [SECURITY.md](SECURITY.md).

## 5. Installing without internet, or silently

- **Offline:** get `buffalo_l.zip` from InsightFace's release
  (`https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip`, 288,621,354
  bytes, SHA-256 `80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f`) and choose it on
  the models page.
- **Silent / scripted** (administrators):

  ```
  WindowsFaceUnlock-Setup-0.2.0-cpu.exe /VERYSILENT /ACCEPTMODELLICENSE [/MODELZIP=C:\path\buffalo_l.zip] [/OWNER=DOMAIN\user] [/FORCEOWNER] [/MERGETASKS="cp"]
  ```

  `/ACCEPTMODELLICENSE` means you accept the InsightFace model terms on behalf of the user; without
  it a silent installation that needs the models stops. `/OWNER=` names the face-sign-in user
  (otherwise: the user at the console); replacing a different recorded owner needs `/FORCEOWNER`.
  Exit codes besides Inno Setup's usual ones: **21** the background tasks could not be registered,
  **22** the lock-screen tile could not be registered, **23** the models could not be installed.
  Password and face still have to be set up by the user.

## 6. Troubleshooting

### 6.1 "Face Unlock service is not running"

- Right after you sign in, the service needs a few seconds to start. Try again.
- Open the tray icon → Status. If the tray is gone, sign out and in again.
- The watchdog restarts a service that stopped answering, with growing pauses if it keeps failing.

### 6.2 Status says "Face sign-in is off: …"

| Reason | Meaning / fix |
|---|---|
| the data folder could not be secured | Your `%USERPROFILE%\.face-unlock` folder has permissions or an owner Face Unlock cannot repair (another account or program changed them). Restart the PC. If it persists, delete the folder (then set up face and password again), or restore its permissions to only you and SYSTEM. The service log names the file. |
| Face Unlock belongs to another Windows account on this PC | You are not the owner recorded at installation. Reinstall while signed in as the right person. |
| the face recognition models are missing or damaged | Run the installer again. |
| the sign-in attempt counter cannot be saved | The disk or folder refuses writes; free space or fix permissions, then restart. |

### 6.3 Other problems

- **The movement is not recognised** -- face the camera straight, do the movements clearly (about a
  quarter turn, a clear nod), and stay still until the instruction appears. If "left" and "right"
  seem swapped, run the setup wizard again: its calibration step fixes it for your camera.
- **The wrong camera is used** -- Settings → Basic → Camera, or the wizard's camera list.
- **Settings window too tall** -- it scrolls; Save and Cancel stay at the bottom.
- **Logs** -- Tray → Open log folder. When you ask for help, send only the `*.log` files from that
  folder. Never send the rest of `%USERPROFILE%\.face-unlock`: it holds your face photos and your
  encrypted password.

## 7. Uninstalling

Settings → Apps → Installed apps → **Windows Face Unlock** → Uninstall (administrator).

The uninstaller stops Face Unlock, removes its background tasks, unregisters the lock-screen tile
and deletes the program folder (files still in use are removed at the next restart). It then asks
whether to also delete the owner's data folder `%USERPROFILE%\.face-unlock` (face photos, face
templates, the encrypted password, settings, logs). A silent uninstall keeps that folder unless it
is run with `/REMOVEDATA`.

What can remain after an uninstall: the data folder if you kept it (and a
`%TEMP%\windows-face-unlock-update` folder if an older version downloaded an update there); Windows'
own records (Task Scheduler history, event logs). The downloaded model archive lives only in
Setup's temporary folder and is deleted when Setup ends. Nothing is stored in Windows Credential Manager, and no firewall rules are
created.
