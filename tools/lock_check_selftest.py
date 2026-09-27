"""tools/lock_check_selftest.py -- the lock completeness check and the frozen-engine gate (9d).

  [1] V-60: tools/lock_check.py -- a missing dependency, a wrong version and a missing pin are
      found; extras and other-platform markers are ignored; onnxruntime is covered by
      onnxruntime-gpu (and only by it).
  [1b] W-32: onnxruntime and onnxruntime-gpu installed together are refused; a package the lock
       does not pin is refused, pip and the build tools of installer/requirements-build.txt
       excepted.
  [2b] W-33: the self-check never downloads -- a pack outside ...\\models\\buffalo_l is a pack
       problem, and every insightface download entry point raises; W-34: each of the four models
       is run on a zero input of its own shape, and the gate judges the actual runs.
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


def test_r2_lock_check():
    print("[1b] W-32: both runtimes; packages not in the lock")
    check("W-32: onnxruntime + onnxruntime-gpu together -> a problem",
          len(L.both_runtimes({"onnxruntime": "1.26.0", "onnxruntime-gpu": "1.26.0"})) == 1)
    check("W-32: one of them alone is fine", L.both_runtimes({"onnxruntime-gpu": "1.26.0"}) == []
          and L.both_runtimes({"onnxruntime": "1.26.0"}) == [])
    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "x.lock"
        lock.write_text("numpy==2.4.4 \\\n    --hash=sha256:00\n", encoding="utf-8")
        build = Path(td) / "build.txt"
        build.write_text("pyinstaller==6.11.1 \\\n    --hash=sha256:11\n", encoding="utf-8")
        have = {"numpy": "2.4.4", "pip": "25.0", "pyinstaller": "6.11.1", "requests": "2.32.0"}
        got = L.not_in_lock(lock, have, allowed_files=(build,))
        check("W-32: a package the lock does not pin is reported; pip and the build tools are not",
              got == ["requests 2.32.0 is installed but not pinned in x.lock"], got)
        check("W-32: the default allowlist is installer/requirements-build.txt",
              L.BUILD_REQUIREMENTS == REPO / "installer" / "requirements-build.txt"
              and "pyinstaller" in L.lock_pins(L.BUILD_REQUIREMENTS))
    r = subprocess.run([_sys.executable, "-c",
                        "import sys; sys.path.insert(0, r'" + str(REPO) + "');"
                        "from tools import lock_check as L;"
                        "L.installed = lambda: {'onnxruntime': '1', 'onnxruntime-gpu': '1'};"
                        "L.missing_requirements = lambda: [];"
                        "sys.exit(L.main([]))"], capture_output=True, text=True, timeout=60)
    check("W-32: main() fails (rc 1) with both runtimes installed", r.returncode == 1
          and "both installed" in r.stdout, (r.returncode, r.stdout[-300:]))


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
        cpu_runs = {k: {"session_providers": ["CPUExecutionProvider"],
                        "node_providers": {"CPUExecutionProvider": 300}, "run_ms": 9.0} for k in four}
        gpu_runs = {k: {"session_providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
                        "node_providers": {"CUDAExecutionProvider": 500, "CPUExecutionProvider": 40},
                        "run_ms": 2.0} for k in four}
        ok_cpu = {"rc": 0, "frozen": True, "variant": "cpu", "provider_load_errors": [],
                  "sessions": {k: ["CPUExecutionProvider"] for k in four}, "session_runs": cpu_runs}
        B.subprocess.run = fake(ok_cpu)
        check("cpu: CPU-only in all four sessions -> passed", B.gate_engine("cpu", Path("."))["state"] == "passed")
        B.subprocess.run = fake({**ok_cpu, "provider_load_errors": ["LoadLibrary failed ... cudnn"]})
        try:
            B.gate_engine("cpu", Path("."))
            check("cpu: a CUDA load message fails the gate", False)
        except B.BuildAbort:
            check("cpu: a CUDA load message fails the gate", True)
        gpu = {"rc": 0, "frozen": True, "variant": "gpu", "provider_load_errors": [],
               "sessions": {k: ["CUDAExecutionProvider", "CPUExecutionProvider"] for k in four},
               "session_runs": gpu_runs}
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
        # W-34: the verdict is taken from the ACTUAL runs
        check("W-34: GPU, CUDA ran nodes in all four runs -> no objection", B.engine_verdict("gpu", gpu) == [])
        cpu_run_rec = {**gpu_runs, "recognition": {"session_providers": ["CUDAExecutionProvider",
                                                                         "CPUExecutionProvider"],
                                                   "node_providers": {"CPUExecutionProvider": 520}}}
        check("W-34: GPU, a session that lists CUDA but ran every node on the CPU -> refused",
              any("CUDA did not run" in b for b in B.engine_verdict("gpu", {**gpu, "session_runs": cpu_run_rec})))
        check("W-34: GPU, only three models run -> refused",
              any("4 session runs" in b for b in B.engine_verdict(
                  "gpu", {**gpu, "session_runs": {k: gpu_runs[k] for k in four[:3]}})))
        leak = {**cpu_runs, "detection": {"session_providers": ["CPUExecutionProvider"],
                                          "node_providers": {"CPUExecutionProvider": 600,
                                                             "CUDAExecutionProvider": 1}}}
        check("W-34: CPU, a node on anything but the CPU -> refused",
              any("not CPU-only" in b for b in B.engine_verdict("cpu", {**ok_cpu, "session_runs": leak})))
        check("W-34: CPU, no runs recorded (an old self-check) -> refused",
              B.engine_verdict("cpu", {**ok_cpu, "session_runs": {}}) != [])
    finally:
        B.subprocess.run, B.loud = saved_run, saved_loud
        if saved_env is None:
            _os.environ.pop("FU_GATE_MODELS", None)
        else:
            _os.environ["FU_GATE_MODELS"] = saved_env


def test_r2_engine():
    print("[2b] W-33 no downloads; W-34 each session on its own zero input")
    from face_service import selfcheck as SC
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "models" / "buffalo_l"
        check("W-33: ...\\models\\buffalo_l is the layout FaceAnalysis reads",
              SC.pack_layout_problems(good) == [], SC.pack_layout_problems(good))
        elsewhere = Path(td) / "packs" / "buffalo_l"
        check("W-33: a pack outside a 'models' folder is a pack problem (never a download)",
              len(SC.pack_layout_problems(elsewhere)) == 1, SC.pack_layout_problems(elsewhere))
        check("W-33: a folder with another name is a pack problem",
              any("named buffalo_l" in x for x in SC.pack_layout_problems(Path(td) / "models" / "pack")))
        done = SC.forbid_model_downloads()
        check("W-33: every insightface download entry point is replaced",
              {"insightface.utils.storage.download_file", "insightface.utils.download.download_file",
               "insightface.model_zoo.model_zoo.download_onnx"} <= set(done), done)
        from insightface.utils import storage
        try:
            storage.ensure_available("models", "buffalo_l", root=str(Path(td) / "empty_root"))
            check("W-33: a missing pack makes ensure_available RAISE instead of downloading", False)
        except RuntimeError as e:
            check("W-33: a missing pack makes ensure_available RAISE instead of downloading",
                  "W-33" in str(e), str(e))

    class _In:
        def __init__(self, name, shape, typ="tensor(float)"):
            self.name, self.shape, self.type = name, shape, typ
    feeds, shapes = SC.zero_feeds([_In("input.1", [None, 3, "h", "w"])], (640, 640))
    check("W-34: dynamic axes -> batch 1 and the model's own image size; zeros",
          shapes == {"input.1": [1, 3, 640, 640]} and feeds["input.1"].dtype.name == "float32"
          and not feeds["input.1"].any(), shapes)
    feeds, shapes = SC.zero_feeds([_In("data", [1, 3, 192, 192])], (640, 640))
    check("W-34: a fixed shape is kept as it is", shapes == {"data": [1, 3, 192, 192]}, shapes)
    runs4 = {k: {"session_providers": ["CPUExecutionProvider"], "node_providers": {"CPUExecutionProvider": 1}}
             for k in ("a", "b", "c", "d")}
    check("W-34: session_runs_ok -- CPU: four runs on the CPU only", SC.session_runs_ok("cpu", runs4)
          and not SC.session_runs_ok("gpu", runs4)
          and not SC.session_runs_ok("cpu", {k: runs4[k] for k in ("a", "b", "c")}))
    src = Path(SC.__file__).read_text(encoding="utf-8")
    body = src.split("def selfcheck_engine_main", 1)[1]
    check("W-33/W-34: the self-check forbids downloads BEFORE FaceAnalysis and runs every model",
          body.find("forbid_model_downloads()") < body.find("FaceAnalysis(")
          and "run_session_profiled(" in body and "session_runs_ok(" in body)


def main() -> int:
    test_lock_check()
    test_r2_lock_check()
    test_engine_gate()
    test_r2_engine()
    print()
    if FAILS:
        print(f"LOCK-CHECK SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("LOCK-CHECK SELFTEST OK: incomplete environments are found (onnxruntime-gpu stands in for "
          "onnxruntime only); the engine self-check is gate-only and the gate applies the variant rules.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
