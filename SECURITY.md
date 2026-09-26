# Security and privacy

Windows Face Unlock puts a face sign-in tile on the Windows lock screen. This page says plainly what
that protects against, what it does not, what was measured, and what the program stores and sends.

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting on the
repository (**Security → Report a vulnerability**). Include the version, what you did, and what
happened. You will get an answer; fixes are released as a new installer.

## 1. The short version

- **Face Unlock is a convenience feature, not a security boundary.** Your PIN and password remain
  the real credentials. Face sign-in, when it succeeds, submits your *saved Windows password* to
  Windows -- so anyone who gets through the face check gets exactly what your password gives.
- It uses an **ordinary RGB webcam**: no depth, no infrared, no hardware-backed key. It is not
  Windows Hello and cannot offer Hello's guarantees.
- Protection against a photo or video of you comes from **random head movements** that the matched
  face has to perform, a check for a **still head** when the instruction appears, and a check for
  **screens** held up to the camera. This raises the bar; it is not proof against every replay.
- **Out of scope:** 3-D masks; anyone who already has your unlocked session; malware or any program
  running under your Windows account; administrators of the PC.

If your threat model includes a determined attacker with physical access and a good recording of
your face, use Windows Hello with an IR camera, a security key, or your PIN.

## 2. How an unlock is decided

1. **Phase 1 -- match.** The tile asks the service to unlock. The service opens the camera, takes a
   short burst, and compares the face with your enrolled face profile (ArcFace embeddings, cosine
   distance, threshold 0.32). A dark scene, a busy camera or an engine fault is reported as such and
   never counts as a failed attempt.
2. **Phase 2 -- two movements.** The service answers with a one-time token and a prompt such as
   *"Turn your head left, then nod"*: two different movements drawn at random from turn left, turn
   right and nod, in a random order. Then:
   - only frames of the **matched face** are used -- another person's movements are ignored;
   - the head must be **still** for the first 0.4 s after the prompt (a looping video that is
     already moving fails with `motion-before-prompt`);
   - the movements must happen **in the order asked**, each within its time limit (the whole round
     is capped at 12.4 s, counted from the first camera frame);
   - if too many of the face frames look like a **screen** (and the room is not too dark to judge),
     the round fails with `screen-suspected`.
   Blinking is no longer accepted as the proof of life. The direction of "left" is calibrated per
   camera in the setup wizard, so a mirrored camera does not swap the movements.
3. **Release.** Only then does the service hand the saved password to the tile, which submits it to
   Windows. The tile reports back whether Windows accepted it; only an accepted sign-in resets the
   failure counter. A password Windows rejects is flagged ("update your saved password").
4. **Failures count.** A face that does not match, failed movements, `motion-before-prompt` and
   `screen-suspected` are strikes. After 5 strikes in a row face sign-in is locked for 5 minutes
   (both configurable); the PIN and password are never locked by Face Unlock. If the failure counter
   cannot be saved, face sign-in refuses to work rather than run without it.

With the **"only when in doubt" (fast)** setting, phase 2 is asked for only when the match is not
clearly strong or a screen is suspected: a confident match with no screen suspicion signs in **with
no movement at all**. That mode is faster and clearly weaker against a good photo or video. New
installations use "every time".

## 3. What was measured

On the development PC (one USB webcam, one person, bright room), on 26 September 2026, against
**version 0.1.1** -- before the two-movement round described above existed:

- **27 spoof attempts, 0 passes** -- phone photo and phone video in fast mode (20 attempts), phone
  video against the real lock screen in the movement mode of that version (7 attempts).
- Live face: 2 of 2 unlocks granted.
- What that run also showed, and what 0.2.0 changed because of it:
  - the screen check was **unstable** (0 to 5 of 5 frames flagged per attempt) -- in 0.2.0 a high
    share of screen frames fails the movement round;
  - the replayed video was **accepted as the owner's identity** in the movement round (108 and 114
    identity frames); the attempts failed only because the movements drawn were head turns that a
    flat phone cannot produce. **A blink round was never drawn** -- and a blink is what a replay can
    show. 0.2.0 removed blinking as a gesture and requires two head movements plus a still start.
- **Not tested:** paper prints, 3-D masks, other cameras, other lighting.

**The 0.2.0 protections have not been measured yet.** A new measurement (ordinary video, a video
made to show head movements, phone photo, a check that a blink alone no longer passes, and live
face latency) is planned before release; this page will be updated with the numbers.

## 4. Known limitations

- **Printed photo moved by hand.** Someone who holds a good print of your face and tilts or turns it
  in front of the camera may be able to imitate head movements. The screen check does not apply to
  paper, and an RGB camera cannot measure depth. This is a known limitation of RGB-only face
  sign-in and was deliberately not measured.
- **3-D masks, look-alikes and identical twins** are not defended against beyond the threshold.
- **Malware or another program running as you** can read your data folder, decrypt the saved
  password (it is protected by DPAPI for *your* account, which by design any program running as you
  can use), change settings, or stop the service. Face Unlock does not try to defend against your
  own account.
- **The password in memory.** The service decrypts the saved password in memory for each release
  and hands it to the sign-in screen. The Python service cannot reliably wipe its copies from
  memory (Python strings are immutable); this is accepted, because anything able to read that
  process's memory can already decrypt the password. The lock-screen component (C++) wipes its
  copies.
- **Administrators** of the PC can read the data folder (face photos and templates) and change the
  program.
- **One user per PC.** Only the owner recorded at installation gets the face tile. The tile is not
  offered to other users, in UAC/credential prompts, or in Remote Desktop sessions.
- **Brute force.** With the defaults, an attacker at the keyboard gets 5 attempts per 5 minutes --
  about 60 attempts an hour, indefinitely. Each attempt still needs the movements.
- **Unsigned builds.** Until the release signing is in place (a later stage of the project), the
  programs and the lock-screen DLL are not signed; on PCs with Smart App Control they are blocked.
  Only install builds whose SHA-256 you can compare with the published `.sha256`.

Not yet verified on real systems (planned): Microsoft Entra ID accounts, passwordless Microsoft
accounts, a second user on the same PC, Smart App Control, CPU-only speed, Windows 10.

## 5. How the parts are protected

- **The pipe** between the lock screen and the service accepts **local** callers only: remote
  clients are rejected and the NETWORK group is denied first in its access list; otherwise only
  your account and SYSTEM may open it. The service refuses to run for anyone but the recorded owner.
  The lock-screen component talks to the service only if **both** the server process and the pipe
  object belong to the owner's account, and the unlock commands are accepted only from SYSTEM (the
  sign-in screen). Tokens and grants are single-use and expire within seconds.
- **The data folder** `%USERPROFILE%\.face-unlock` is re-secured at every service start: only your
  account, SYSTEM and Administrators; the password and its key file only your account and SYSTEM.
  Reparse points (junctions, symbolic links) and foreign owners make the check fail -- and then every
  face function stays **off** until it is fixed; settings and the face profile are not read from an
  insecure folder.
- **Settings** are validated key by key; a bad value falls back to that key's safe default (for
  the face sign-in check: "every time"), with a warning in the log. The pipe protections above have
  no setting at all -- they cannot be switched off.
- **Models** are checked against pinned SHA-256 fingerprints at every start; missing or changed
  model files turn face sign-in off.
- **The installation** is always in `C:\Program Files\WindowsFaceUnlock`, which only administrators
  can change; Setup verifies that before registering the lock-screen tile.
- **Updates** are only reported, never downloaded or run automatically.

## Privacy

Everything stays on your PC. There is no account, no cloud service and no usage statistics.

### What is stored, and where

All in `%USERPROFILE%\.face-unlock\` (the owner's profile):

| Item | Contains |
|---|---|
| `enroll\` | **your face photos** from the setup wizard (the frames that were kept) |
| `embeddings.npz` | your face template: 512 numbers per photo, derived from your face |
| `adaptive.npz` | extra templates learned from successful unlocks -- only if "adaptive gallery" is turned on (off by default) |
| `calibration.json` | the head-turn direction for your camera, by camera name |
| `credentials.bin`, `pipe_entropy.bin` | your Windows user name, domain and **password**, encrypted with DPAPI for your account, plus a random per-installation key |
| `config.toml` | settings you changed |
| `lockout.json`, `watchdog.pause`, `presence_paused.json`, `update_state.json`, `password_rejected.flag` | small state files |
| `audit.jsonl` | one line per sign-in attempt and per setup action: time, result, match distance, the liveness measurements, the movements asked. No images, no password. On by default (`audit_log`), rotated by size. |
| `logs\` | `service.log`, `presence.log`, `enroll.log`, `watchdog.log`: technical logs, rotated at 5 MB. They contain file paths (which include your Windows user name), match distances, presence results, the names of detected remote-support programs, and which local program connected to the pipe. No images and no password. |
| `debug_frames\` | raw camera frames -- **only** if the diagnostic switch `debug_dump_frames` is turned on (off by default) |

The installation folder holds the program and the face-recognition models, nothing personal.

When you ask someone for help, send only the files in `logs\` (tray → Open log folder opens exactly
that folder). The rest of the data folder is personal.

### What goes over the network

- **Installation:** the face-recognition models are downloaded once from InsightFace's release on
  GitHub (`github.com/deepinsight/insightface/releases/...`), after you accept their terms -- or not
  at all if you choose a local copy.
- **Update check:** at most once a day, the tray sends one unauthenticated request to
  `https://api.github.com/repos/xbaox/windows-face-unlock/releases/latest` with the header
  `User-Agent: windows-face-unlock/<version>`. GitHub therefore sees your IP address and the program
  version. Turn it off in Settings → Basic → *Look for updates*. "Check for updates…" in the tray
  sends the same request on demand.
- **Nothing else.** ONNX Runtime (the recognition engine) has its own telemetry that is on by
  default in Microsoft's builds; Face Unlock **switches it off** in every process that loads it.
  Face Unlock itself collects no telemetry.

### Deleting your data

- Setup wizard → **Delete face profile** removes the face photos, templates, learned templates and
  any diagnostic frames.
- Password dialog → **Delete saved password** removes the saved password (it asks first).
- Uninstalling offers to delete the whole data folder (a silent uninstall needs `/REMOVEDATA`). You
  can also delete `%USERPROFILE%\.face-unlock` yourself after uninstalling.
