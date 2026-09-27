"""tools/lock_check_selftest.py -- the lock completeness check and the frozen-engine gate (9d).

  [1] V-60: tools/lock_check.py -- a missing dependency, a wrong version and a missing pin are
      found; extras and other-platform markers are ignored; onnxruntime is covered by
      onnxruntime-gpu (and only by it).
  [2] V-61: face_service --selfcheck-engine runs only under FU_BUILD_GATE=1 and reports a bad pack;
      installer/build.py's gate_engine applies the variant criteria and SKIPs loudly without a pack.

Run:  python -m tools.lock_check_selftest
"""
from __future__ import annotations

import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tools import testhome  # noqa: E402
testhome.isolate("faceunlock_lockcheck_")

import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

from tools import lock_check as L  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FAILS: list = []


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f"  (got={got!r})"))
    if not cond:
        FAILS.append(name)


class _Dist:
    def __init__(self, name, requires):
        self.metadata = {"Name": name}
        self.requires = requires


def test_lock_check():
    print("[1] V-60: lock_check")
    have = {"insightface": "1.0.1", "onnxruntime-gpu": "1.26.0", "numpy": "2.4.4", "pillow": "12.2.0"}
    d = [_Dist("insightface", ["numpy", "onnxruntime", "matplotlib; extra == 'plot'",
                               "pywin32; sys_platform == 'linux'"])]
    check("onnxruntime is covered by onnxruntime-gpu; extras and other-platform markers ignored",
          L.missing_requirements(d, have) == [], L.missing_requirements(d, have))
    d2 = [_Dist("insightface", ["numpy>=3", "scipy"])]
    got = L.missing_requirements(d2, have)
    check("a wrong version and a missing package are both reported",
          len(got) == 2 and any("numpy>=3" in g for g in got) and any("scipy" in g for g in got), got)
    d3 = [_Dist("x", ["onnxruntime-gpu"])]
    check("onnxruntime does NOT cover onnxruntime-gpu (one-way substitution)",
          L.missing_requirements(d3, {"onnxruntime": "1.26.0"}) != [])
    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "x.lock"
        lock.write_text("numpy==2.4.4 \\\n    --hash=sha256:00\npillow==11.0 \\\n    --hash=sha256:11\n"
                        "scipy==1.18.0 \\\n    --hash=sha256:22\n", encoding="utf-8")
        mm = L.lock_mismatches(lock, have)
        check("lock pins: a different version and an absent package are reported",
              len(mm) == 2 and any("pillow==11.0" in m for m in mm) and any("scipy" in m for m in mm), mm)
    r = subprocess.run([_sys.executable, "-m", "tools.lock_check"], cwd=REPO, capture_output=True, text=True)
    check("this environment is complete (lock_check exit 0)", r.returncode == 0, r.stdout[-400:])


def test_engine_gate():
    print("[2] V-61: the frozen engine self-check and its gate")
    env = dict(_os.environ)
    env.pop("FU_BUILD_GATE", None)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "e.json"
        r = subprocess.run([_sys.executable, "-m", "face_service", "--selfcheck-engine", td, "--out",
                            str(out)], cwd=REPO, env=env, capture_output=True, timeout=120)
        check("without FU_BUILD_GATE=1 the mode refuses (exit 2) and writes nothing",
              r.returncode == 2 and not out.exists(), r.returncode)
        env["FU_BUILD_GATE"] = "1"
        bad = Path(td) / "buffalo_l"
        bad.mkdir()
        r = subprocess.run([_sys.executable, "-m", "face_service", "--selfcheck-engine", str(bad), "--out",
                            str(out)], cwd=REPO, env=env, capture_output=True, timeout=300)
        data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        check("a pack that fails check_pack -> exit 1, the problems in the JSON, no engine built",
              r.returncode == 1 and data.get("pack_problems") and not data.get("sessions"), data)
    _sys.path.insert(0, str(REPO / "installer"))
    import build as B
    saved_run, saved_env = B.subprocess.run, _os.environ.get("FU_GATE_MODELS")
    loud = []
    saved_loud = B.loud
    B.loud = lambda m: loud.append(m)
    try:
        _os.environ.pop("FU_GATE_MODELS", None)
        res = B.gate_engine("cpu", Path("C:/nowhere"))
        check("no FU_GATE_MODELS -> a LOUD skip recorded in the stamp",
              res == {"state": "skipped-no-models"} and loud and "SKIPPED" in loud[0], (res, loud))
        _os.environ["FU_GATE_MODELS"] = "C:/pack/buffalo_l"

        def fake(report):
            def run(cmd, **kw):
                Path(cmd[cmd.index("--out") + 1]).write_text(json.dumps(report), encoding="utf-8")
                return subprocess.CompletedProcess(cmd, report.get("rc", 0), b"", b"")
            return run
        four = ("detection", "recognition", "landmark_2d_106", "landmark_3d_68")
        ok_cpu = {"rc": 0, "frozen": True, "variant": "cpu", "provider_load_errors": [],
                  "sessions": {k: ["CPUExecutionProvider"] for k in four}}
        B.subprocess.run = fake(ok_cpu)
        check("cpu: CPU-only in all four sessions -> passed", B.gate_engine("cpu", Path("."))["state"] == "passed")
        B.subprocess.run = fake({**ok_cpu, "provider_load_errors": ["LoadLibrary failed ... cudnn"]})
        try:
            B.gate_engine("cpu", Path("."))
            check("cpu: a CUDA load message fails the gate", False)
        except B.BuildAbort:
            check("cpu: a CUDA load message fails the gate", True)
        gpu = {"rc": 0, "frozen": True, "variant": "gpu", "provider_load_errors": [],
               "sessions": {k: ["CUDAExecutionProvider", "CPUExecutionProvider"] for k in four}}
        B.subprocess.run = fake(gpu)
        check("gpu: CUDA first in all four sessions -> passed", B.gate_engine("gpu", Path("."))["state"] == "passed")
        bad_gpu = {**gpu, "sessions": {**gpu["sessions"], "recognition": ["CPUExecutionProvider"]}}
        B.subprocess.run = fake(bad_gpu)
        try:
            B.gate_engine("gpu", Path("."))
            check("gpu: one session on the CPU fails the gate (an incomplete NVIDIA allowlist)", False)
        except B.BuildAbort:
            check("gpu: one session on the CPU fails the gate (an incomplete NVIDIA allowlist)", True)
        B.subprocess.run = fake({**ok_cpu, "variant": "gpu"})
        try:
            B.gate_engine("cpu", Path("."))
            check("a bundle built from the other onnxruntime package fails the gate", False)
        except B.BuildAbort:
            check("a bundle built from the other onnxruntime package fails the gate", True)
    finally:
        B.subprocess.run, B.loud = saved_run, saved_loud
        if saved_env is None:
            _os.environ.pop("FU_GATE_MODELS", None)
        else:
            _os.environ["FU_GATE_MODELS"] = saved_env


def main() -> int:
    test_lock_check()
    test_engine_gate()
    print()
    if FAILS:
        print(f"LOCK-CHECK SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("LOCK-CHECK SELFTEST OK: incomplete environments are found (onnxruntime-gpu stands in for "
          "onnxruntime only); the engine self-check is gate-only and the gate applies the variant rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
