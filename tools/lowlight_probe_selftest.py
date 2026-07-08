"""tools/lowlight_probe_selftest.py -- Stage 3 / Step 3.1 aggregation self-test (no camera).

Covers the PURE binning/aggregation of tools.lowlight_probe on synthetic frames:
summarize_bins (luma binning, face-detection fraction, distance stats, no-face and
missing-distance handling, bin width) and boost_comparison (no-boost vs boost split).
This is not security-critical logic, so the camera path is intentionally not tested here.

Run from the repo root:
    python -m tools.lowlight_probe_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tools.lowlight_probe import ProbeRow, summarize_bins, boost_comparison
except ImportError:  # allow a direct `python tools/lowlight_probe_selftest.py`
    from lowlight_probe import ProbeRow, summarize_bins, boost_comparison  # type: ignore


_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


def approx(name: str, got, want, tol=1e-9) -> None:
    ok = got is not None and abs(got - want) <= tol
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want~={want}")
    if not ok:
        _fails.append(name)


def row(scene_luma, *, face=True, det=0.90, crop_luma=100.0, distance=0.10,
        hf=0.20, peak=8.0, lap=200.0, screen=False, boost=False, ts=0.0) -> ProbeRow:
    """Build a ProbeRow; when face=False all face-derived fields are None (as in production)."""
    if not face:
        return ProbeRow(ts, scene_luma, False, None, None, None, None, None, None, None, boost)
    return ProbeRow(ts, scene_luma, True, det, crop_luma, distance, hf, peak, lap, screen, boost)


def test_binning_and_ranges() -> None:
    print("summarize_bins -- populated bins only, ascending, correct ranges + stats")
    rows = [
        row(5.0, distance=0.30),                 # bin 0: [0,12)
        row(8.0, face=False),                    # bin 0: no face
        row(10.0, distance=0.28),                # bin 0
        row(30.0, distance=0.10),                # bin 2: [24,36)
        row(33.0, distance=0.12),                # bin 2
    ]
    bins = summarize_bins(rows, bin_width=12.0)
    check("two populated bins (bin 1 empty -> skipped)", len(bins), 2)
    b0, b2 = bins
    approx("bin0 lo", b0.lo, 0.0)
    approx("bin0 hi", b0.hi, 12.0)
    check("bin0 n", b0.n, 3)
    check("bin0 n_face", b0.n_face, 2)
    approx("bin0 face_frac (2/3)", b0.face_frac, 2 / 3)
    approx("bin0 dist_mean (0.30,0.28)", b0.dist_mean, 0.29)
    approx("bin0 dist_max", b0.dist_max, 0.30)
    approx("bin2 lo", b2.lo, 24.0)
    approx("bin2 dist_mean (0.10,0.12)", b2.dist_mean, 0.11)
    approx("bin2 face_frac all faces", b2.face_frac, 1.0)


def test_no_face_and_missing_distance() -> None:
    print("summarize_bins -- no-face bin, and face frames with distance=None excluded from stats")
    # A bin with only no-face frames: counted in n, but no distance/crop stats.
    only_noface = summarize_bins([row(3.0, face=False), row(4.0, face=False)], 12.0)
    check("one bin", len(only_noface), 1)
    b = only_noface[0]
    check("n counts no-face frames", b.n, 2)
    check("n_face zero", b.n_face, 0)
    approx("face_frac zero", b.face_frac, 0.0)
    check("dist_mean None (no faces)", b.dist_mean, None)
    check("crop_luma_mean None", b.crop_luma_mean, None)
    check("screen_flag_frac None", b.screen_flag_frac, None)

    # Faces present but no enrollment -> distance=None: face still counts, distance excluded.
    no_enroll = summarize_bins([row(50.0, distance=None), row(52.0, distance=None)], 12.0)
    bb = no_enroll[0]
    check("n_face counts faces w/o distance", bb.n_face, 2)
    approx("face_frac 1.0 despite no distance", bb.face_frac, 1.0)
    check("dist_mean None when all distances missing", bb.dist_mean, None)
    approx("crop_luma_mean still computed", bb.crop_luma_mean, 100.0)


def test_screen_flag_frac() -> None:
    print("summarize_bins -- screen_flag_frac counts flagged face frames only")
    rows = [
        row(6.0, screen=True), row(7.0, screen=False), row(8.0, screen=True),
        row(9.0, face=False),   # no face -> no screen verdict, excluded from screen frac
    ]
    b = summarize_bins(rows, 12.0)[0]
    check("n includes the no-face frame", b.n, 4)
    check("n_face", b.n_face, 3)
    approx("screen_flag_frac 2 of 3 face frames", b.screen_flag_frac, 2 / 3)


def test_bin_width_param() -> None:
    print("summarize_bins -- bin width changes the grouping")
    rows = [row(10.0), row(20.0), row(25.0)]
    b12 = summarize_bins(rows, 12.0)     # 10->bin0, 20->bin1, 25->bin2  => 3 bins
    check("width 12 -> 3 bins", len(b12), 3)
    b15 = summarize_bins(rows, 15.0)     # 10->bin0, 20->bin1, 25->bin1  => 2 bins
    check("width 15 -> 2 bins", len(b15), 2)
    b30 = summarize_bins(rows, 30.0)     # all -> bin0            => 1 bin
    check("width 30 -> 1 bin", len(b30), 1)
    approx("width 30 single bin hi", b30[0].hi, 30.0)


def test_empty() -> None:
    print("summarize_bins -- empty input -> empty list; invalid width raises")
    check("empty -> []", summarize_bins([], 12.0), [])
    raised = False
    try:
        summarize_bins([row(1.0)], 0.0)
    except ValueError:
        raised = True
    check("bin_width<=0 raises ValueError", raised, True)


def test_boost_comparison() -> None:
    print("boost_comparison -- splits by boost flag; boost brightens + lowers distance")
    rows = [
        row(20.0, crop_luma=60.0, distance=0.25, boost=False),
        row(22.0, crop_luma=64.0, distance=0.23, boost=False),
        row(80.0, crop_luma=120.0, distance=0.11, boost=True),
        row(84.0, crop_luma=124.0, distance=0.09, boost=True),
    ]
    nb, bo = boost_comparison(rows)
    check("no-boost label", nb.label, "no-boost")
    check("boost label", bo.label, "boost")
    check("no-boost n", nb.n, 2)
    check("boost n", bo.n, 2)
    approx("no-boost scene mean", nb.scene_luma_mean, 21.0)
    approx("boost scene mean", bo.scene_luma_mean, 82.0)
    approx("no-boost dist mean", nb.dist_mean, 0.24)
    approx("boost dist mean", bo.dist_mean, 0.10)
    check("boost brighter than no-boost", bo.scene_luma_mean > nb.scene_luma_mean, True)
    check("boost distance lower (better)", bo.dist_mean < nb.dist_mean, True)

    # No boost frames at all -> boost group is empty (n=0), no crash.
    nb2, bo2 = boost_comparison([row(30.0), row(31.0)])
    check("empty boost group n", bo2.n, 0)
    check("empty boost group means None", bo2.dist_mean, None)
    check("no-boost group populated", nb2.n, 2)


def main() -> int:
    for t in (test_binning_and_ranges, test_no_face_and_missing_distance,
              test_screen_flag_frac, test_bin_width_param, test_empty, test_boost_comparison):
        t()
        print()
    print(f"{'FAILED' if _fails else 'OK'}: all checks "
          f"({len(_fails)} failing{': ' + ', '.join(_fails) if _fails else ''})")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
