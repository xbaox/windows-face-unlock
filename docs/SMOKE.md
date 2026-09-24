# Live smoke protocol

How a live smoke of the service, presence and verify is run and judged. Written in
Stage 8b (D-10) from rules learned the hard way in 7k and 7l (`audit-notes.md`,
7k А–Г and 7l Е/Ж/З). A smoke that does not follow these rules is not evidence.

## 1. Which copy is under test

- **Say which one, by path.** The scheduled tasks always run the INSTALLED copy
  (`<InstallDir>\<InstalledExe>`; `{#BuildRoot}` exists only at compile time). A
  smoke of a fresh build is valid only when the exe is started explicitly out of
  `dist\WindowsFaceUnlock\` (7k Д).
- **Isolate the data.** A dist smoke runs with `FACE_UNLOCK_HOME` pointed at a
  scratch directory created for the smoke, never at the real `%USERPROFILE%\.face-unlock`.
- **One service at a time.** The pipe name and the `Local\FaceUnlockService` mutex
  are global to the session: stop the production stack first, in this order —
  `FaceUnlock-Watchdog` task, pipe `shutdown`, `FaceUnlock-Presence` task, confirm
  zero processes, `FaceUnlock-Service` task — and bring it back afterwards
  (`Start-ScheduledTask` ×3, then three processes from Program Files and a `ping`).

## 2. Scene and posture (recognition smokes)

- **Light:** daylight or bright, even room light on the face. Evening light,
  backlight and a dark room change the result class (7l Е: `real=False` in evening
  light), and the probe then measures the scene, not the code.
- **Posture:** sit as at the lock screen — camera at eye level, face straight on,
  no headphones (they raise the distance, known since 7-i), nothing covering the
  face. Record anything unusual in the verdict file.

## 3. The probe is named, and the answer is quoted

- The command is named in the task (`presence`, `verify`, `ping`, `status`) and
  the report quotes the JSON reply next to it. `status` opens no camera and proves
  nothing about recognition (7k А).
- **READY pattern v2** before the first probe (7k Г): the process being up is not
  enough — wait for the log lines `InsightFace ready` and `camera warmup ok`, then
  poll `ping` until it answers, pause ~2 s, and only then send the first probe.

## 4. The criterion includes the scene and the frame class

A count alone ("3/3") is not a criterion (7k Б). State the expected result class:

| Probe | Expected (pass) | Frame classes that explain a miss |
|---|---|---|
| `presence` × 3 | `ok:true present:true real:true`, `state=present` | `none` (no face), `weak`/`suspect` (face at a distance or flagged as screen), `error` (engine could not judge — since 8b answered `ok:false engine-error`) |
| `verify`, `liveness_mode = "paranoid"` | `match:false verdict:NEEDS_GESTURE` with `distance ≤ 0.32` and `real:true` (paranoid asks every recognised face for a gesture — 7l З) | `NOT_LIVE`, distance above the threshold |
| `verify`, `liveness_mode = "fast"` | `match:true verdict:PASS` | as above |

A miss with a face in every frame at `d≈0.4–0.6` is "face found, not matched" (pose,
light, headphones), not blindness; a miss with `frames_ok=0` or black luma is the
camera; `engine-error` is the engine. Name the class in the verdict.

## 5. Verdict files

- Every live probe goes to a file under the window's evidence folder
  (`C:\dev\d-series\<window>\...`) together with the service's reply and the
  matching log lines. The report cites the file, never memory (7k В).
- The window's outcome goes to `<window>-verdict.txt`: `GREEN` or `RED` plus the
  reason, written before the report.
- Results travel as a short text excerpt in the message body (command, reply, two
  or three log lines); an attachment is only ever a duplicate (7k Е).

## 6. Harness pitfalls (PowerShell 5.1)

- Do not combine `2>&1` on native executables with `$ErrorActionPreference = 'Stop'`.
- Wrap single results in `@(...)` before `.Count` or `[-1]` — a lone string indexes
  by character (7l Е).
- Take the JSON reply as the `{…}` block of the client output: the timing line
  `[N ms]` may come before or after it (7l Е).
- Read logs with `-Encoding utf8`.
- Zip evidence with the `ZipFile` API and `/` in entry names.
