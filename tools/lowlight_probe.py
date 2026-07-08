"""tools/lowlight_probe.py -- Stage 3 / Step 3.1 low-light characterization (READ-ONLY).

Purpose: collect the numbers needed to set a low-light gate (Step 3.2) and decide a
mechanism -- WITHOUT changing any production logic. This tool only reads: it never
writes embeddings.npz / adaptive.npz / config / lockout.json / audit.jsonl and never
goes through the service unlock path (no adaptation, no audit as a side effect). It
writes exactly ONE artifact: its own CSV under ~/.face-unlock.

What it measures, reusing the SAME code the engine uses (nothing re-invented):

  scene_luma  full-frame mean gray (0..255). This is the BINNING axis: it exists on
              every frame, including ones where no face is detected, so we can show how
              the face-detection rate falls off as the room gets dark. (crop luma, below,
              only exists when there is a face, so it can't bin the no-face frames.)
  crop_luma   enroll_qc.frame_quality(...).luma -- the aligned-112 crop luma, i.e. the
              EXACT metric that will feed the Step-3.2 gate.
  det         face.det_score (via frame_quality) -- detector confidence.
  distance    min cosine distance to the ENROLLMENT baseline (_enroll_refs), using the
              recognizer's own cosine. Deliberately NOT the adaptive-union distance, so the
              measurement is independent of adaptive-gallery state.
  hf/peak/lap raw anti-screen features from the recognizer's ScreenDetector (its .last,
              the same object analyze_frame collects). FrameAnalysis.screen collapses these
              to one bool; we log the raw hf so we can see how much the anti-screen signal
              "drifts with light" (per the sub-spec) as luma drops.
  screen      the anti-screen per-frame verdict (hf < HF_THRESH).

The camera is opened EXACTLY like the production service: face_service.camera.Camera with
cfg.camera_index / cfg.camera_warmup_frames (same backend order DSHOW->MSMF->ANY, same
640x480, same warmup) so the numbers transfer to prod. If the camera can't be opened /
is busy, we exit with a clear one-line message (no traceback / crash).

--boost checks whether THIS webcam actually honors a manual gain/exposure lift: it records
the current gain/exposure/auto-exposure, captures a no-boost burst, tries to disable auto
exposure and raise gain+exposure (logging each set->get roundtrip so you can see whether the
driver honored or silently ignored it), captures a boost burst, and ALWAYS restores the
original camera settings in a finally block.

Run from the repo root:
    python -m tools.lowlight_probe                 # live window; dim the room and watch
    python -m tools.lowlight_probe --seconds 20    # headless, fixed duration
    python -m tools.lowlight_probe --boost         # gain/exposure roundtrip + boost vs no-boost

Kill the running face-unlock service first: the webcam has a single consumer.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

# Allow both `python -m tools.lowlight_probe` (from repo root) and a direct
# `python tools/lowlight_probe.py` invocation to import face_service.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BIN_WIDTH_DEFAULT = 12.0        # luma bin width for the summary (sub-spec asks ~10-15)
BOOST_FRAMES_DEFAULT = 60       # frames per burst in --boost mode
HEADLESS_DEFAULT_SECONDS = 20.0 # passive capture length when there is no window

# Boost targets. Values are driver-dependent; we set then read back and report the
# roundtrip rather than assuming any of these "took".
BOOST_AUTO_EXPOSURE_MANUAL = 0.25  # DSHOW convention: 0.25=manual, 0.75=auto (driver-specific)
BOOST_GAIN_TARGET = 255.0          # deliberately high -> the driver clamps to its max (reveals it)
BOOST_EXPOSURE_STEP = 2.0          # raise exposure by this (less-negative == brighter on log2 scales)


# --------------------------------------------------------------------------- data model

class ProbeRow(NamedTuple):
    ts: float
    scene_luma: float              # full-frame mean gray (binning axis; always present)
    face: bool                     # a face was detected this frame
    det: "float | None"            # detector confidence, or None when no face
    crop_luma: "float | None"      # enroll_qc aligned-crop luma (gate-3.2 metric), or None
    distance: "float | None"       # min cosine to enrollment baseline, or None (no face / no enroll)
    hf: "float | None"             # anti-screen high-freq fraction (raw), or None
    peak: "float | None"           # anti-screen mid-band peakiness (raw), or None
    lap: "float | None"            # anti-screen Laplacian variance (raw), or None
    screen: "bool | None"          # anti-screen verdict (hf<thresh); None if crop unusable
    boost: bool                    # was a manual gain/exposure boost active for this frame


class BinStat(NamedTuple):
    lo: float
    hi: float
    n: int                         # frames in this luma bin (face or not)
    n_face: int                    # of those, how many had a detected face
    face_frac: float               # n_face / n
    dist_mean: "float | None"      # over face frames with a distance
    dist_max: "float | None"
    crop_luma_mean: "float | None"
    det_mean: "float | None"
    hf_mean: "float | None"
    screen_flag_frac: "float | None"  # over face frames with a screen verdict


class GroupStat(NamedTuple):
    label: str
    n: int
    n_face: int
    face_frac: float
    scene_luma_mean: "float | None"
    crop_luma_mean: "float | None"
    dist_mean: "float | None"
    dist_max: "float | None"
    hf_mean: "float | None"


# ------------------------------------------------------------------- pure aggregation

def _mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def summarize_bins(rows, bin_width: float = BIN_WIDTH_DEFAULT):
    """Bin ``rows`` by scene_luma and reduce each populated bin to a BinStat.

    Pure (no camera / no engine / stdlib only) so tools.lowlight_probe_selftest can cover
    it on synthetic data. Rows with no face are counted in ``n`` (and drag face_frac down)
    but are excluded from the distance / crop_luma / det / hf / screen aggregates. Only
    populated bins are returned, ascending by luma.
    """
    if bin_width <= 0:
        raise ValueError("bin_width must be > 0")
    buckets: dict[int, list] = {}
    for r in rows:
        idx = int(r.scene_luma // bin_width)
        buckets.setdefault(idx, []).append(r)

    out: list[BinStat] = []
    for idx in sorted(buckets):
        rs = buckets[idx]
        face_rows = [r for r in rs if r.face]
        dists = [r.distance for r in face_rows if r.distance is not None]
        clumas = [r.crop_luma for r in face_rows if r.crop_luma is not None]
        dets = [r.det for r in face_rows if r.det is not None]
        hfs = [r.hf for r in face_rows if r.hf is not None]
        screens = [r.screen for r in face_rows if r.screen is not None]
        out.append(BinStat(
            lo=idx * bin_width,
            hi=(idx + 1) * bin_width,
            n=len(rs),
            n_face=len(face_rows),
            face_frac=(len(face_rows) / len(rs)) if rs else 0.0,
            dist_mean=_mean(dists),
            dist_max=(max(dists) if dists else None),
            crop_luma_mean=_mean(clumas),
            det_mean=_mean(dets),
            hf_mean=_mean(hfs),
            screen_flag_frac=(sum(1 for s in screens if s) / len(screens)) if screens else None,
        ))
    return out


def _group_stat(rows, label: str) -> GroupStat:
    face_rows = [r for r in rows if r.face]
    dists = [r.distance for r in face_rows if r.distance is not None]
    clumas = [r.crop_luma for r in face_rows if r.crop_luma is not None]
    hfs = [r.hf for r in face_rows if r.hf is not None]
    scenes = [r.scene_luma for r in rows]
    return GroupStat(
        label=label,
        n=len(rows),
        n_face=len(face_rows),
        face_frac=(len(face_rows) / len(rows)) if rows else 0.0,
        scene_luma_mean=_mean(scenes),
        crop_luma_mean=_mean(clumas),
        dist_mean=_mean(dists),
        dist_max=(max(dists) if dists else None),
        hf_mean=_mean(hfs),
    )


def boost_comparison(rows):
    """Split rows into (no-boost, boost) groups and reduce each. Pure; both groups always
    returned (an empty group has n=0)."""
    noboost = _group_stat([r for r in rows if not r.boost], "no-boost")
    boost = _group_stat([r for r in rows if r.boost], "boost")
    return noboost, boost


# --------------------------------------------------------------------------- CSV output

CSV_COLUMNS = ["ts", "iso", "scene_luma", "face", "det", "crop_luma",
               "distance", "hf", "peak", "lap", "screen", "boost"]


def _num(v, nd):
    return "" if v is None else f"{v:.{nd}f}"


def _write_csv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.ts))
            screen = "" if r.screen is None else int(bool(r.screen))
            w.writerow([
                f"{r.ts:.3f}", iso, _num(r.scene_luma, 2), int(bool(r.face)),
                _num(r.det, 3), _num(r.crop_luma, 2), _num(r.distance, 4),
                _num(r.hf, 5), _num(r.peak, 3), _num(r.lap, 2), screen, int(bool(r.boost)),
            ])


# ------------------------------------------------------------------------ pretty print

def _f(v, nd, width):
    return (f"{v:{width}.{nd}f}" if v is not None else f"{'n/a':>{width}}")


def _print_bin_summary(rows, bin_width: float, threshold: float, luma_min: float) -> None:
    bins = summarize_bins(rows, bin_width)
    print(f"\n=== luma-binned summary (bin width {bin_width:g}, {len(rows)} frame(s)) ===")
    if not bins:
        print("  (no frames)")
        return
    print(f"{'luma bin':>13}{'n':>6}{'face%':>7}{'cropL':>8}{'dist~':>8}"
          f"{'distMax':>9}{'det~':>7}{'hf~':>8}{'scrn%':>7}  flags")
    for b in bins:
        flags = []
        if b.dist_max is not None and b.dist_max >= threshold:
            flags.append("DIST>=thr")           # a genuine frame reaching the match threshold
        if b.crop_luma_mean is not None and b.crop_luma_mean < luma_min:
            flags.append("cropL<enroll_min")     # crop luma below the current enroll dark gate
        if b.face_frac < 0.5:
            flags.append("DETECT-falloff")       # detector losing the face in this light
        scrn = (f"{b.screen_flag_frac * 100:>6.0f}%" if b.screen_flag_frac is not None
                else f"{'n/a':>7}")
        print(f"{b.lo:6.0f}-{b.hi:<6.0f}{b.n:>6}{b.face_frac * 100:>6.0f}%"
              f"{_f(b.crop_luma_mean, 1, 8)}{_f(b.dist_mean, 3, 8)}{_f(b.dist_max, 3, 9)}"
              f"{_f(b.det_mean, 3, 7)}{_f(b.hf_mean, 4, 8)}{scrn}"
              f"  {', '.join(flags)}")
    print(f"  thr={threshold:g}  enroll_luma_min={luma_min:g}  | dist~=mean cosine to enrollment; "
          f"scrn%=share of face frames anti-screen flagged (rises in dark = drift).")


def _print_boost_comparison(rows, threshold: float) -> None:
    nb, bo = boost_comparison(rows)
    if bo.n == 0 and nb.n == 0:
        return
    print("\n=== boost vs no-boost (same lighting) ===")
    print(f"{'group':>9}{'n':>5}{'face%':>7}{'sceneL':>9}{'cropL':>8}{'dist~':>8}{'distMax':>9}{'hf~':>8}")
    for g in (nb, bo):
        print(f"{g.label:>9}{g.n:>5}{g.face_frac * 100:>6.0f}%"
              f"{_f(g.scene_luma_mean, 1, 9)}{_f(g.crop_luma_mean, 1, 8)}"
              f"{_f(g.dist_mean, 3, 8)}{_f(g.dist_max, 3, 9)}{_f(g.hf_mean, 4, 8)}")
    # Plain-language verdict.
    if nb.scene_luma_mean is not None and bo.scene_luma_mean is not None:
        dl = bo.scene_luma_mean - nb.scene_luma_mean
        print(f"  scene luma {'rose' if dl > 1 else 'did NOT rise'} with boost "
              f"(delta {dl:+.1f}).")
    if nb.dist_mean is not None and bo.dist_mean is not None:
        dd = bo.dist_mean - nb.dist_mean
        print(f"  mean distance-to-enrollment {'improved' if dd < -0.005 else ('worsened' if dd > 0.005 else 'unchanged')} "
              f"with boost (delta {dd:+.3f}; lower is better, thr={threshold:g}).")


# ------------------------------------------------------------- camera + per-frame probe

def _open_service_camera(index: int, warmup: int):
    """Open the webcam exactly like the production service. Raises RuntimeError (already a
    clean message) if the device can't be opened / is busy."""
    from face_service.camera import Camera
    cam = Camera(index, warmup)
    cam.open()
    return cam


def _load_enroll_refs(rec):
    """Load the ENROLLMENT baseline embeddings, read-only, for distance measurement.

    Guarded so Recognizer.load()'s no-enrollment branch -- which calls _adaptive.clear()
    and DELETES adaptive.npz -- can never run: the probe must not write any state. When
    embeddings.npz exists, load() is read-only (it only reads adaptive when the opt-in
    toggle is on, and even then only reads). Returns (refs | None, note).
    """
    from face_service.config import EMBED_PATH
    if not EMBED_PATH.exists():
        return None, f"no enrollment ({EMBED_PATH.name} absent) -> distance-to-enrollment disabled"
    ok = rec.load()  # read-only when EMBED_PATH exists; validates engine tag / dim
    refs = getattr(rec, "_enroll_refs", None)
    if not ok or refs is None or not refs.size:
        return None, "enrollment present but not loadable (engine/dim mismatch?) -> distance disabled"
    return refs, f"enrollment loaded: {refs.shape[0]} baseline embedding(s)"


def _analyze_frame(app, rec, refs, frame, boost: bool, now: float) -> ProbeRow:
    """One detect -> a ProbeRow. Mirrors Recognizer.analyze_frame's single-detect path
    (same _lazy_app / _largest_face / normed_embedding / _screen.check) but inlined so we
    can ALSO surface det_score, the aligned-crop luma, and the raw hf/peak/lap that
    FrameAnalysis collapses into one bool -- without adding anything to the recognizer."""
    import numpy as np
    from face_service import enroll_qc
    from face_service.lowlight import scene_luma as _scene_luma  # canonical: shared with the prod gate

    scene_luma = _scene_luma(frame)  # identical formula to Step 3.2's gate (single source of truth)

    faces = app.get(frame)
    if not faces:
        return ProbeRow(now, scene_luma, False, None, None, None, None, None, None, None, boost)

    face = rec._largest_face(faces)
    q = enroll_qc.frame_quality(frame, face)  # gate-3.2 metric; None if the crop is unusable
    det = float(q.det) if q is not None else float(getattr(face, "det_score", 0.0))
    crop_luma = float(q.luma) if q is not None else None

    distance = None
    if refs is not None and refs.size:
        emb = np.asarray(face.normed_embedding, dtype=np.float32)  # == FrameAnalysis.embedding
        distance = min(rec._cosine(emb, r) for r in refs)         # recognizer's own cosine, to enroll baseline

    # Anti-screen: same ScreenDetector instance the engine uses. .last is only fresh when
    # check() returns non-None (it does NOT update .last on an unusable crop), so gate on it.
    screen = rec._screen.check(frame, face.bbox)
    if screen is None:
        hf = peak = lap = None
    else:
        sf = rec._screen.last
        hf, peak, lap = float(sf.hf), float(sf.peak), float(sf.lap)

    return ProbeRow(now, scene_luma, True, det, crop_luma, distance, hf, peak, lap, screen, boost)


def _draw_overlay(frame, row: ProbeRow, threshold: float) -> None:
    import cv2
    g = (0, 220, 0)
    y = 24
    cv2.putText(frame, f"sceneL={row.scene_luma:6.1f}  face={'Y' if row.face else 'N'}",
                (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, g, 1, cv2.LINE_AA)
    if row.face:
        dstr = "n/a" if row.distance is None else f"{row.distance:.3f}"
        near = row.distance is not None and row.distance >= threshold - 0.06
        col = (0, 0, 255) if near else (0, 255, 255)
        cv2.putText(frame, f"cropL={_fmt(row.crop_luma)}  det={_fmt(row.det)}  dist={dstr}(thr{threshold:g})",
                    (8, y + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 1, cv2.LINE_AA)
        cv2.putText(frame, f"hf={_fmt(row.hf, 4)}  screen={'Y' if row.screen else 'N'}",
                    (8, y + 50), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 200, 0), 1, cv2.LINE_AA)
    cv2.putText(frame, "dim the room; hold still ~2-3s per level;  q/ESC = quit",
                (8, frame.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1, cv2.LINE_AA)


def _fmt(v, nd=1):
    return "n/a" if v is None else f"{v:.{nd}f}"


# --------------------------------------------------------------------------- boost mode

def _cap_props(cap):
    import cv2
    return {
        "auto_exposure": cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
        "gain": cap.get(cv2.CAP_PROP_GAIN),
        "exposure": cap.get(cv2.CAP_PROP_EXPOSURE),
    }


def _try_set(cap, prop, value):
    """set(prop, value) then get(prop). Returns (before, after, honored). 'honored' means the
    driver's read-back moved to ~our request; a driver that silently ignores the write leaves
    'after' unchanged and honored=False."""
    before = cap.get(prop)
    cap.set(prop, float(value))
    after = cap.get(prop)
    honored = abs(after - float(value)) < 1e-3
    return before, after, honored


def _capture_burst(app, rec, refs, cam, n_frames: int, boost: bool):
    rows = []
    for _ in range(2):     # drain stale buffered frames (matches the service)
        cam.read()
    got = 0
    misses = 0
    while got < n_frames:
        frame = cam.read()
        if frame is None:
            misses += 1
            if misses > 100:  # camera went away mid-burst -> end early rather than spin forever
                print("[lowlight] camera stopped returning frames; ending burst early.")
                break
            continue
        misses = 0
        rows.append(_analyze_frame(app, rec, refs, frame, boost, time.time()))
        got += 1
    return rows


def _run_boost(app, rec, refs, cam, n_frames: int):
    """No-boost burst, then a manual-boost burst at the SAME lighting, restoring the camera
    settings in a finally block. Returns (rows, report_lines)."""
    import cv2
    cap = cam._cap  # the underlying VideoCapture; the probe needs driver-level prop control
    if cap is None:
        raise RuntimeError("camera has no underlying capture handle")

    report: list[str] = []
    originals = _cap_props(cap)
    report.append("original camera props: "
                  f"auto_exposure={originals['auto_exposure']:.3f} "
                  f"gain={originals['gain']:.3f} exposure={originals['exposure']:.3f}")

    print("[boost] capturing NO-BOOST baseline -- hold still...")
    rows = _capture_burst(app, rec, refs, cam, n_frames, boost=False)

    try:
        b1, a1, h1 = _try_set(cap, cv2.CAP_PROP_AUTO_EXPOSURE, BOOST_AUTO_EXPOSURE_MANUAL)
        report.append(f"set AUTO_EXPOSURE -> {BOOST_AUTO_EXPOSURE_MANUAL} : "
                      f"{b1:.3f} -> {a1:.3f}  [{'honored' if h1 else 'IGNORED'}]")
        b2, a2, h2 = _try_set(cap, cv2.CAP_PROP_GAIN, BOOST_GAIN_TARGET)
        report.append(f"set GAIN -> {BOOST_GAIN_TARGET:g} (max-out) : "
                      f"{b2:.3f} -> {a2:.3f}  [{'moved' if abs(a2 - b2) > 1e-3 else 'IGNORED'}]")
        exp_target = originals["exposure"] + BOOST_EXPOSURE_STEP
        b3, a3, h3 = _try_set(cap, cv2.CAP_PROP_EXPOSURE, exp_target)
        report.append(f"set EXPOSURE -> {exp_target:.3f} (+{BOOST_EXPOSURE_STEP:g}) : "
                      f"{b3:.3f} -> {a3:.3f}  [{'honored' if h3 else 'IGNORED'}]  "
                      f"(exposure scale is driver-defined)")
        for _ in range(8):     # let auto->manual + new gain/exposure settle
            cam.read()
        print("[boost] capturing BOOST burst -- keep holding still...")
        rows += _capture_burst(app, rec, refs, cam, n_frames, boost=True)
    finally:
        # ALWAYS restore, so the service never inherits a manual/boosted camera.
        cap.set(cv2.CAP_PROP_EXPOSURE, originals["exposure"])
        cap.set(cv2.CAP_PROP_GAIN, originals["gain"])
        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, originals["auto_exposure"])
        now = _cap_props(cap)
        restored = (abs(now["auto_exposure"] - originals["auto_exposure"]) < 1e-2
                    and abs(now["gain"] - originals["gain"]) < 1e-2
                    and abs(now["exposure"] - originals["exposure"]) < 1e-2)
        report.append(f"restored camera props: auto_exposure={now['auto_exposure']:.3f} "
                      f"gain={now['gain']:.3f} exposure={now['exposure']:.3f}  "
                      f"[{'OK' if restored else 'MISMATCH -- check camera'}]")

    return rows, report


# ------------------------------------------------------------------ passive sweep mode

def _passive_capture(app, rec, refs, cam, seconds, no_window: bool, threshold: float):
    """Live-window sweep (dim the room, watch the numbers) or a headless timed capture.
    Logs every frame as boost=False."""
    import cv2
    rows = []
    for _ in range(2):
        cam.read()

    use_window = not no_window
    t_end = (time.monotonic() + seconds) if seconds else None
    if not use_window and t_end is None:
        t_end = time.monotonic() + HEADLESS_DEFAULT_SECONDS
    win = "lowlight_probe :: dim the room; q/ESC to finish"

    misses = 0
    while True:
        frame = cam.read()
        if frame is None:
            misses += 1
            if misses > 200:  # dead camera and no window to quit -> bail instead of hanging
                print("[lowlight] camera stopped returning frames; stopping.")
                break
            if t_end is not None and time.monotonic() >= t_end:
                break
            continue
        misses = 0
        row = _analyze_frame(app, rec, refs, frame, boost=False, now=time.time())
        rows.append(row)
        if use_window:
            try:
                _draw_overlay(frame, row, threshold)
                cv2.imshow(win, frame)
                k = cv2.waitKey(1) & 0xFF
                if k in (27, ord('q')):
                    break
            except cv2.error:
                use_window = False           # no GUI available -> fall back to a timed run
                if t_end is None:
                    t_end = time.monotonic() + HEADLESS_DEFAULT_SECONDS
                print(f"[lowlight] no display available; running headless "
                      f"~{HEADLESS_DEFAULT_SECONDS:g}s...")
        if t_end is not None and time.monotonic() >= t_end:
            break

    if not no_window:
        try:
            cv2.destroyAllWindows()
        except cv2.error:
            pass
    return rows


# --------------------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Low-light characterization probe (Stage 3 / Step 3.1, read-only)."
    )
    ap.add_argument("--camera", type=int, default=None,
                    help="camera index (default: cfg.camera_index)")
    ap.add_argument("--seconds", type=float, default=None,
                    help="passive mode: capture this long headless instead of a live window")
    ap.add_argument("--no-window", action="store_true",
                    help="passive mode: never open a preview window")
    ap.add_argument("--boost", action="store_true",
                    help="run the gain/exposure roundtrip + boost-vs-no-boost comparison")
    ap.add_argument("--boost-frames", type=int, default=BOOST_FRAMES_DEFAULT,
                    help=f"frames per burst in --boost mode (default {BOOST_FRAMES_DEFAULT})")
    ap.add_argument("--bin", type=float, default=BIN_WIDTH_DEFAULT,
                    help=f"luma bin width for the summary (default {BIN_WIDTH_DEFAULT:g})")
    args = ap.parse_args(argv)

    from face_service.config import Config, APP_DIR
    from face_service.recognizer import Recognizer

    cfg = Config.load()
    index = args.camera if args.camera is not None else cfg.camera_index
    threshold = float(cfg.threshold)
    luma_min = float(cfg.enroll_luma_min)

    rec = Recognizer(cfg)                       # in-memory only; constructing writes nothing
    refs, note = _load_enroll_refs(rec)
    print(f"[lowlight] {note}")
    print(f"[lowlight] threshold={threshold:g}  enroll_luma_min={luma_min:g}  camera_index={index}")
    print("[lowlight] loading engine (buffalo_l, det_size=640); warms CUDA once...")
    app = rec._lazy_app()

    try:
        cam = _open_service_camera(index, cfg.camera_warmup_frames)
    except RuntimeError as e:
        print(f"[lowlight] ERROR: cannot open camera index {index}: {e}")
        print("[lowlight] Is the face-unlock service (or another app) holding the webcam? "
              "Stop it and retry.")
        return 2

    boost_report: list[str] = []
    try:
        if args.boost:
            rows, boost_report = _run_boost(app, rec, refs, cam, max(1, args.boost_frames))
        else:
            rows = _passive_capture(app, rec, refs, cam, args.seconds, args.no_window, threshold)
    finally:
        cam.close()

    if not rows:
        print("[lowlight] no frames captured.")
        return 1

    ts = time.strftime("%Y%m%d_%H%M%S")
    csv_path = APP_DIR / f"lowlight_probe_{ts}.csv"
    _write_csv(csv_path, rows)

    if boost_report:
        print("\n=== boost gain/exposure roundtrip ===")
        for line in boost_report:
            print("  " + line)
        _print_boost_comparison(rows, threshold)

    # The luma-binned characterization uses the no-boost frames (the honest ambient axis);
    # the boost frames are compared separately above.
    _print_bin_summary([r for r in rows if not r.boost], args.bin, threshold, luma_min)

    n_face = sum(1 for r in rows if r.face)
    print(f"\n[lowlight] {len(rows)} frame(s), {n_face} with a face. CSV -> {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
