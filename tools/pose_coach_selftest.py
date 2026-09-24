"""tools/pose_coach_selftest.py -- Stage 8b package J proof: the enrollment pose warning (D-17).

Calibration data are LITERALS copied from C:\\dev\\d-series\\7l\\c\\gallery-diag.txt (pitch, yaw per
enrolled frame, degrees) -- numbers only, no image is read. Expectations are literals too, never
derived from the thresholds under test (lesson of 7-i).
  [1] every July frame (2026-07-19 and 2026-07-20, 30 frames) -> no warning.
  [2] every September frame (2026-09-23, 15 frames) -> warning.
  [3] the session medians: July-19 (-10.6, 3.8) and July-20 (-13.0, 7.8) no; September (-18.9, 1.6)
      yes -- the service reports medians, the wizard judges them.
  [4] literal boundary points: pitch -15.0 no / -15.1 yes; yaw +/-15.0 no / +/-15.1 yes.
  [5] the build reply carries the median pose (service side), and a build without a pose
      carries none; acceptance is not involved.

Run:  python -m tools.pose_coach_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import statistics
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["FACE_UNLOCK_HOME"] = tempfile.mkdtemp(prefix="faceunlock_pose_")

from presence_monitor.enroll_gui import pose_warning
from face_service.config import Config
from face_service.service import FaceService

FAILS: list[str] = []

# (pitch, yaw) from gallery-diag.txt, rows 1-15 / 16-30 / 31-45.
JULY_19 = [(-10.8, 1.6), (-11.8, 3.8), (-10.9, 3.6), (-10.3, 4.0), (-10.4, 4.5), (-11.4, 3.3),
           (-11.9, 1.0), (-12.4, 5.2), (-10.8, 4.6), (-10.0, 3.5), (-10.2, 3.8), (-10.6, 3.9),
           (-10.0, 3.9), (-8.8, 3.0), (-10.0, 3.6)]
JULY_20 = [(-13.4, 6.5), (-13.1, 5.7), (-13.3, 6.0), (-12.9, 6.4), (-12.6, 6.5), (-13.1, 7.0),
           (-13.8, 8.0), (-13.5, 7.8), (-13.0, 7.8), (-12.8, 7.9), (-12.5, 8.1), (-13.4, 8.5),
           (-12.9, 8.6), (-12.1, 9.4), (-12.0, 9.0)]
SEPT_23 = [(-24.1, 3.4), (-21.6, 3.5), (-21.1, 3.1), (-20.9, 4.0), (-20.8, 3.5), (-19.8, 1.3),
           (-19.1, 1.0), (-18.9, 1.4), (-18.5, 1.5), (-18.1, 1.7), (-18.3, 1.5), (-17.7, 1.2),
           (-17.0, 1.4), (-16.9, 1.7), (-16.5, 1.6)]


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


def _median(frames):
    return (statistics.median(p for p, _y in frames), statistics.median(y for _p, y in frames))


def main() -> int:
    print("[1] July frames -> no warning")
    for i, (p, y) in enumerate(JULY_19 + JULY_20, start=1):
        if pose_warning(p, y):
            check(f"July frame {i} ({p}, {y})", False, True)
    check("all 30 July frames pass without a warning",
          not any(pose_warning(p, y) for p, y in JULY_19 + JULY_20))

    print("[2] September frames -> warning")
    check("all 15 September frames are warned about",
          all(pose_warning(p, y) for p, y in SEPT_23),
          [(p, y) for p, y in SEPT_23 if not pose_warning(p, y)])

    print("[3] session medians")
    for label, frames, want in (("2026-07-19", JULY_19, False), ("2026-07-20", JULY_20, False),
                                ("2026-09-23", SEPT_23, True)):
        mp, my = _median(frames)
        check(f"{label} median ({mp:.1f}, {my:.1f}) -> warn={want}", pose_warning(mp, my) is want)

    print("[4] literal boundaries")
    for p, y, want in ((-15.0, 0.0, False), (-15.1, 0.0, True), (0.0, 15.0, False),
                       (0.0, 15.1, True), (0.0, -15.0, False), (0.0, -15.1, True),
                       (-14.9, 14.9, False)):
        check(f"({p}, {y}) -> {want}", pose_warning(p, y) is want)

    print("[5] build reply carries the median pose")
    s = FaceService.__new__(FaceService)
    s.cfg = Config()
    s._cam_lock = threading.Lock()

    class _Recog:
        last_enroll_pose = {"pitch": -18.94, "yaw": 1.64, "n": 15}

        def enroll_from_dir(self, _d):
            return 15

    s.recog = _Recog()
    r = s._handle({"cmd": "build_enrollment"}, None)
    check("pose in the reply, rounded", r.get("pose") == {"pitch": -18.9, "yaw": 1.6, "n": 15}, r)
    s.recog.last_enroll_pose = None
    r = s._handle({"cmd": "build_enrollment"}, None)
    check("no pose -> no key, count unchanged", "pose" not in r and r.get("count") == 15, r)

    if FAILS:
        print(f"\nPOSE COACH SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nPOSE COACH SELFTEST OK: July frames and sessions pass, every September frame and its "
          "session are warned about, the boundaries sit at pitch -15 / |yaw| 15, and the build "
          "reply reports the median pose without touching acceptance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
