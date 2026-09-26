"""tools/models_selftest.py -- the model pins and the service's no-models refusal (Stage 9, act 9b R7).

Decision 9-02: buffalo_l is not redistributed; the installer downloads the official archive and
checks it against face_service/model_pins.py, and the service refuses face functions unless the
model directory holds EXACTLY the five pinned files. No network, no engine, no 340 MB copies:
the pin table is swapped for small synthetic files where a real pack would be needed.
  [1] check_pack: exact set -> []; a missing file, an extra file, a wrong size and a wrong hash
      are each named; hashes can be skipped for the cheap check.
  [2] the pins are the single source: build.py and the recognizer read them from model_pins.
  [3] the service: a model problem at start -> ping {"state":"refusing","why":"no-models"}, every
      face function answers "no-models", the CP maps it to "needs attention".
  [4] YuNet: the repo model matches its pin; a modified copy is refused; the old upstream
      (facewinunlock-tauri) fallback path is gone.
  [5] the frozen layout looks for the pack next to the executables ({app}\\models\\buffalo_l),
      never inside the bundle.

Run:  python -m tools.models_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import hashlib
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_models_")

from face_service import model_pins as MP

FAILS: list[str] = []
REPO = Path(__file__).resolve().parents[1]


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got!r})"))
    if not cond:
        FAILS.append(name)


def _fake_pack(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    pins = {}
    for i, name in enumerate(MP.BUFFALO_FILES):
        data = (f"fake {name} ".encode() * (i + 3))
        (root / name).write_bytes(data)
        pins[name] = (len(data), hashlib.sha256(data).hexdigest())
    return pins


def test_check_pack(tmp: Path):
    print("[1] check_pack: exactly the pinned five")
    pack = tmp / "models" / "buffalo_l"
    fake = _fake_pack(pack)
    real = MP.BUFFALO_FILES
    MP.BUFFALO_FILES = fake
    try:
        check("the exact set -> no problem", MP.check_pack(pack) == [], MP.check_pack(pack))
        (pack / "extra.onnx").write_bytes(b"x")
        check("an extra file is named", any("extra.onnx" in p for p in MP.check_pack(pack)))
        (pack / "extra.onnx").unlink()
        name = next(iter(fake))
        data = (pack / name).read_bytes()
        (pack / name).write_bytes(data[:-1] + b"#")
        check("same size, wrong content -> the hash mismatch is named",
              any(name in p and "SHA-256" in p for p in MP.check_pack(pack)), MP.check_pack(pack))
        check("... but the cheap check (sizes only) does not look at content",
              MP.check_pack(pack, hashes=False) == [])
        (pack / name).write_bytes(data + b"++")
        check("a wrong size is named", any("bytes" in p for p in MP.check_pack(pack)))
        (pack / name).unlink()
        check("a missing file is named", any(f"missing: {name}" in p for p in MP.check_pack(pack)))
        check("a missing directory is one problem", len(MP.check_pack(tmp / "nope")) == 1)
    finally:
        MP.BUFFALO_FILES = real


def test_single_source():
    print("[2] one source of the pins")
    check("five files pinned, sizes and SHA-256 present",
          len(MP.BUFFALO_FILES) == 5 and all(len(h) == 64 and n > 0 for n, h in MP.BUFFALO_FILES.values()))
    check("the archive pin and URL are the insightface ones",
          MP.BUFFALO_ZIP_URL.endswith("/v0.7/buffalo_l.zip") and len(MP.BUFFALO_ZIP_SHA256) == 64)
    from face_service import recognizer as R
    check("recognizer.MODEL_FILES comes from model_pins", R.MODEL_FILES == tuple(MP.BUFFALO_FILES))
    build = (REPO / "installer" / "build.py").read_text(encoding="utf-8")
    check("build.py carries no SHA literal of its own (it imports model_pins)",
          "model_pins" in build and "5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91" not in build)


def test_service_refusal():
    print("[3] the service refuses with no-models")
    from face_service.config import Config
    from face_service.service import FaceService

    class _Lk:
        store_ok = True

        def remaining(self):
            return 0.0

        def record(self, ok):
            raise AssertionError("no strike on a model problem")

        def status(self):
            return {}

    s = FaceService.__new__(FaceService)
    s._caller_sid = lambda h: "S-1-5-18"
    s.cfg = Config()
    s._lockout = _Lk()
    s._audit = type("A", (), {"write": lambda *a: None})()
    s._cam_lock = threading.Lock()
    s._camera_paused_until = 0.0
    s._models_problem = "missing: det_10g.onnx"
    r = s._handle({"cmd": "ping"}, None)
    check("ping: alive, refusing, why=no-models",
          r == {"ok": True, "pong": True, "state": "refusing", "why": "no-models"}, r)
    for req in ({"cmd": "unlock", "v": 2}, {"cmd": "presence"}, {"cmd": "build_enrollment"},
                {"cmd": "unlock_gesture", "v": 2, "token": "a" * 32}):
        r = s._handle(req, None)
        check(f"{req['cmd']} -> no-models", r == {"ok": False, "reason": "no-models"}, r)
    cpp = (REPO / "credential_provider" / "PipeClient.cpp").read_text(encoding="utf-8")
    check("the tile maps no-models to 'needs attention'",
          '"no-models"' in cpp.split("return Text::NeedsAttention;", 1)[0].rsplit("if (", 1)[1])
    from face_service import recognizer as R
    real = R.model_problems
    R.model_problems = lambda hashes=True: ["missing: det_10g.onnx"]
    try:
        rec = R.Recognizer(Config())
        try:
            rec._build_app()
            check("the engine build refuses a bad pack", False)
        except R.ModelsUnavailable as e:
            check("the engine build refuses a bad pack with ModelsUnavailable", True)
            check("... and says how to fix it (run the installer), not 'go online'",
                  "installer" in str(e) and "internet" not in str(e), str(e))
    finally:
        R.model_problems = real


def test_yunet(tmp: Path):
    print("[4] YuNet is pinned; no upstream fallback")
    from face_service import detector as D
    check("the repo model matches its pin", MP.check_yunet(REPO / "models" / MP.YUNET_FILE) == [])
    bad = tmp / MP.YUNET_FILE
    shutil.copy(REPO / "models" / MP.YUNET_FILE, bad)
    with open(bad, "r+b") as f:
        f.seek(100)
        f.write(b"\x00\x01")
    check("a modified copy is refused", MP.check_yunet(bad) != [])
    src = (REPO / "face_service" / "detector.py").read_text(encoding="utf-8")
    check("no facewinunlock-tauri path in the detector", "Program Files\\\\facewinunlock" not in src
          and "_LEGACY_MODEL" not in src)
    check("yunet_model_path() resolves the pinned model", D.yunet_model_path().name == MP.YUNET_FILE)
    lic = REPO / "models" / "LICENSE-yunet"
    check("models/LICENSE-yunet ships with the model (D-154)", lic.is_file() and "MIT" in lic.read_text("utf-8"))


def test_frozen_layout():
    print("[5] the frozen layout")
    from face_service import recognizer as R
    real_frozen = getattr(sys, "frozen", None)
    real_exe = sys.executable
    try:
        sys.frozen = True
        sys.executable = r"C:\Program Files\WindowsFaceUnlock\face_service.exe"
        d = R.model_dir()
        check("frozen: {app}\\models\\buffalo_l next to the executables",
              str(d).lower() == r"c:\program files\windowsfaceunlock\models\buffalo_l", str(d))
    finally:
        sys.executable = real_exe
        if real_frozen is None:
            del sys.frozen
        else:
            sys.frozen = real_frozen


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fu-models-"))
    try:
        test_check_pack(tmp)
        test_single_source()
        test_service_refusal()
        test_yunet(tmp)
        test_frozen_layout()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILS:
        print(f"\nMODELS SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nMODELS SELFTEST OK: exactly five pinned files or a clear no-models refusal; one source "
          "of pins; YuNet pinned without an upstream fallback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
