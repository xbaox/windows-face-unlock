"""tools/calibration_selftest.py -- the turn-sign calibration (Stage 9, act 9b R6 / F-117; B14 N-14).

The wizard saves a few frontal frames and a few frames of a turn to the user's own left into
<data>\\calibration\\ and asks the service (calibrate_turn) to measure them with the same pose
estimator the lock screen uses. No camera, no engine: the pose estimator is stubbed per file.
  [1] SELF only, and only while the wizard holds the camera lease; malformed names refused.
  [2] a left turn that raises yaw -> left_is_negative_yaw false; one that lowers it (a mirroring
      camera) -> true; stored in calibration.json bound to the camera; the frames are deleted.
  [3] a turn smaller than the gesture's own YAW_DELTA is refused (turn-too-small), nothing stored.
  [4] the round uses the stored sign for that camera only.

Run:  python -m tools.calibration_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FACE_UNLOCK_HOME", tempfile.mkdtemp(prefix="faceunlock_calib_"))

import numpy as np

from face_service import config as C
from face_service import imio
from face_service import service as S
from face_service.config import Config
from face_service.liveness import YAW_DELTA

FAILS: list[str] = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


class _PoseRecog:
    """pose_of() answers from the file's first pixel: yaw encoded in a (lossless) PNG."""
    def pose_of(self, bgr):
        return (-13.0, float(bgr[0, 0, 0]) - 100.0)


def _frames(names_yaws):
    C.CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for name, yaw in names_yaws:
        img = np.zeros((8, 8, 3), np.uint8)
        img[0, 0, 0] = int(yaw + 100)
        imio.imwrite(C.CALIBRATION_DIR / name, img)
        out.append(name)
    return out


def _svc(leased=True):
    s = S.FaceService.__new__(S.FaceService)
    s._caller_sid = lambda h: S.current_user_sid()
    s.cfg = Config()
    s.recog = _PoseRecog()
    s._audit = type("A", (), {"write": lambda *a: None})()
    s._cam_lock = threading.Lock()
    s._camera_paused_until = time.monotonic() + (60.0 if leased else -1.0)
    s._calibration = {}
    return s


def main() -> int:
    print("[1] who and when")
    s = _svc()
    s._caller_sid = lambda h: "S-1-5-18"
    orig_diag = S._pipe_client_diag
    S._pipe_client_diag = lambda h: "stub"
    try:
        r = s._handle({"cmd": "calibrate_turn", "frontal": ["a.jpg"], "left": ["b.jpg"]}, None)
    finally:
        S._pipe_client_diag = orig_diag
    check("SYSTEM caller -> not-authorized (SELF only)", r == {"ok": False, "reason": "not-authorized"}, r)
    s = _svc(leased=False)
    r = s._handle({"cmd": "calibrate_turn", "frontal": ["a.jpg"], "left": ["b.jpg"]}, None)
    check("no camera lease -> not-leased", r == {"ok": False, "reason": "not-leased"}, r)
    s = _svc()
    for bad in ({"frontal": "a.jpg", "left": ["b.jpg"]}, {"frontal": ["..\\x.jpg"], "left": ["b.jpg"]},
                {"frontal": [], "left": ["b.jpg"]}, {"frontal": ["a/b.jpg"], "left": ["c.jpg"]}):
        r = s._handle({"cmd": "calibrate_turn", **bad}, None)
        check(f"malformed {bad} -> bad-request", r == {"ok": False, "reason": "bad-request"}, r)

    print("[2] the sign, stored per camera; the frames deleted")
    s = _svc()
    fr = _frames([("f1.png", -3.0), ("f2.png", -2.0), ("f3.png", -4.0)])
    lf = _frames([("l1.png", 32.0), ("l2.png", 35.0), ("l3.png", 30.0)])
    r = s._handle({"cmd": "calibrate_turn", "frontal": fr, "left": lf}, None)
    check("a left turn that RAISES yaw -> left_is_negative_yaw false (the reference camera)",
          r.get("ok") is True and r.get("left_is_negative_yaw") is False and r.get("delta_deg") > YAW_DELTA, r)
    data = json.loads(C.CALIBRATION_PATH.read_text(encoding="utf-8"))
    check("calibration.json holds it for this camera",
          data["cameras"][s._camera_id()]["left_is_negative_yaw"] is False, data)
    check("the calibration frames are deleted", not C.CALIBRATION_DIR.exists())
    fr = _frames([("f1.png", 2.0), ("f2.png", 3.0)])
    lf = _frames([("l1.png", -30.0), ("l2.png", -33.0)])
    r = s._handle({"cmd": "calibrate_turn", "frontal": fr, "left": lf}, None)
    check("a mirroring camera (left turn LOWERS yaw) -> left_is_negative_yaw true",
          r.get("ok") is True and r.get("left_is_negative_yaw") is True, r)
    check("the service uses it at once", s._left_sign() == -1.0)

    print("[3] too small a turn")
    s2 = _svc()
    fr = _frames([("f1.png", 0.0)])
    lf = _frames([("l1.png", YAW_DELTA - 2.0)])
    before = C.CALIBRATION_PATH.read_text(encoding="utf-8")
    r = s2._handle({"cmd": "calibrate_turn", "frontal": fr, "left": lf}, None)
    check("|delta| <= YAW_DELTA -> turn-too-small", r.get("reason") == "turn-too-small", r)
    check("... nothing stored, frames deleted", C.CALIBRATION_PATH.read_text(encoding="utf-8") == before
          and not C.CALIBRATION_DIR.exists())

    print("[4] bound to the camera")
    s3 = _svc()
    s3._calibration = s._calibration
    check("same camera -> the stored sign", s3._left_sign() == -1.0)
    s3.cfg = Config(camera_index=1)
    check("another camera -> no calibration (the default applies)", s3._left_sign() is None)

    if FAILS:
        print(f"\nCALIBRATION SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nCALIBRATION SELFTEST OK: SELF-only during the lease; the sign measured with the lock "
          "screen's estimator, stored per camera, frames deleted; small turns refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
