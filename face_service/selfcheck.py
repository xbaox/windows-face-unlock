"""Hidden frozen self-checks the build gate runs out of the BUILT exe (Stage 8b-2; 9d).

    face_service.exe --selfcheck-custody <dir> --out <file.json>
    face_service.exe --selfcheck-engine <models_dir> --out <file.json>     (9d, V-61)

Both run only with FU_BUILD_GATE=1 (face_service/__main__.py).

Defect -> consequence -> fix: the 8b build passed every static check and every venv selftest,
yet the FROZEN service could not heal the data directory at all (a lazily imported pywin32 module
missing from the bundle). Consequence: the first place that failure showed was a manual dist smoke
-- one step before the installer. Fix: the build gate runs THIS mode out of the built exe on a
seeded scratch directory, so the real code path is exercised in the real bundle before the stamp.

What it does: heal_data_dir(<dir>) then verify_data_dir(<dir>), and writes one JSON object to
<file>. Exit 0 when the heal succeeded, the verification walk is clean and every secret file is
protected SELF + SYSTEM; exit 1 otherwise; exit 2 on a usage error. It opens no pipe, no camera, no
engine, reads no config and never touches APP_DIR: <dir> is the only path it acts on. The exe is
windowed (stdout / stderr may be None), so the file is the only output channel.
"""
from __future__ import annotations

import json
import logging
import os
import sys


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list = []

    def emit(self, record):
        try:
            self.lines.append("%s %s: %s" % (record.levelname, record.name, record.getMessage()))
        except Exception:
            pass


def _secret_state(path: str, self_sid: str) -> dict:
    import win32security  # type: ignore
    sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    sids = sorted({win32security.ConvertSidToStringSid(dacl.GetAce(i)[-1])
                   for i in range(dacl.GetAceCount())}) if dacl is not None else []
    protected = bool(sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED)
    return {"protected": protected, "sids": sids,
            "self_system_only": protected and set(sids) <= {self_sid, "S-1-5-18"} and bool(sids)}


def selfcheck_custody_main(argv) -> int:
    """argv: ["--selfcheck-custody", <dir>, "--out", <file>]."""
    try:
        target = argv[argv.index("--selfcheck-custody") + 1]
        out = argv[argv.index("--out") + 1]
    except (ValueError, IndexError):
        return 2
    handler = _ListHandler()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    result: dict = {"dir": target, "ok": False}
    try:
        from face_service import datadir as D
        rep = D.heal_data_dir(target)
        problems = D.verify_data_dir(target)
        self_sid = D.self_sid_string()
        secrets = {}
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if not D.is_reparse(os.path.join(root, d))]   # never follow
            for name in files:
                if name.lower().startswith(D.SECRET_PREFIXES):
                    full = os.path.join(root, name)
                    secrets[os.path.relpath(full, target)] = _secret_state(full, self_sid)
        foreign = sum(1 for p in problems if "foreign ACE" in p)
        result.update({
            "heal": {"ok": rep.ok, "aces_removed": rep.aces_removed, "relocked": rep.relocked,
                     "rewritten": rep.rewritten, "objects": rep.objects, "problems": rep.problems},
            "verify_problems": problems,
            "foreign_ace_problems": foreign,
            "secrets": secrets,
            "win32timezone_loaded": "win32timezone" in sys.modules,
            "frozen": bool(getattr(sys, "frozen", False)),
        })
        result["ok"] = (rep.ok and not problems and foreign == 0
                        and all(s["self_system_only"] for s in secrets.values()))
    except Exception as e:   # the report must be written whatever happened
        result["exception"] = repr(e)
    result["log"] = handler.lines
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    except Exception:
        return 1
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------------------------
# 9d (V-61): the engine, frozen
# ---------------------------------------------------------------------------------------------

def _capture_native_stderr(path: str):
    """Point the C-level stderr (fd 2) at ``path``: ONNX Runtime reports a provider that failed to
    load ("LoadLibrary failed with error 126 ...") there, and the windowed exe has no console."""
    try:
        f = open(path, "w+b")
        os.dup2(f.fileno(), 2)
        return f
    except Exception:
        return None


def pack_layout_problems(pack) -> "list[str]":
    """9d-r2 (W-33): FaceAnalysis looks for <root>\\models\\buffalo_l and DOWNLOADS the pack when
    it is not there -- so a pack anywhere else is a pack problem, never a reason to fetch one."""
    from pathlib import Path
    from face_service.model_pins import PACK_NAME
    pack = Path(pack)
    out = []
    if pack.name != PACK_NAME:
        out.append(f"the pack folder must be named {PACK_NAME}")
    if pack.parent.name.lower() != "models":
        out.append(f"the pack must sit in a folder named models (...\\models\\{PACK_NAME}), "
                   f"not in {pack.parent.name or pack.parent}")
    return out


def forbid_model_downloads() -> "list[str]":
    """9d-r2 (W-33): the engine self-check never downloads: every insightface download entry point
    is replaced by one that raises. Returns what was replaced."""
    import importlib

    def _refuse(*_a, **_k):
        raise RuntimeError("a model download was attempted during the engine self-check -- "
                           "refused (9d-r2, W-33)")
    done = []
    for mod, attr in (("insightface.utils.download", "download_file"),
                      ("insightface.utils.storage", "download_file"),
                      ("insightface.utils.storage", "download_onnx"),
                      ("insightface.utils", "download_onnx"),
                      ("insightface.model_zoo.model_zoo", "download_onnx")):
        try:
            m = importlib.import_module(mod)
        except Exception:
            continue
        if hasattr(m, attr):
            setattr(m, attr, _refuse)
            done.append(f"{mod}.{attr}")
    return done


_ORT_NP_TYPES = {"tensor(float)": "float32", "tensor(float16)": "float16", "tensor(double)": "float64",
                 "tensor(uint8)": "uint8", "tensor(int64)": "int64", "tensor(int32)": "int32"}


def zero_feeds(inputs, spatial: "tuple[int, int]") -> "tuple[dict, dict]":
    """9d-r2 (W-34): a zero input for every session input, in its own shape; a dynamic axis is 1
    for the batch and ``spatial`` (height, width) for the image axes."""
    import numpy as np
    feeds, shapes = {}, {}
    for i in inputs:
        shape = []
        for k, d in enumerate(i.shape):
            if isinstance(d, int) and d > 0:
                shape.append(d)
            elif k == 0:
                shape.append(1)
            elif k == len(i.shape) - 2:
                shape.append(int(spatial[0]))
            elif k == len(i.shape) - 1:
                shape.append(int(spatial[1]))
            else:
                shape.append(1)
        feeds[i.name] = np.zeros(shape, dtype=_ORT_NP_TYPES.get(i.type, "float32"))
        shapes[i.name] = shape
    return feeds, shapes


def run_session_profiled(ort, model_file: str, providers, spatial, tmpdir: str, task: str) -> dict:
    """9d-r2 (W-34): run ONE model on a zero input of its own shape in a fresh session with ONNX
    Runtime profiling, and report what really ran: the session's providers, the provider of
    every executed node (from the profile), and the median of three timed runs."""
    import os
    import statistics
    import time as _time
    so = ort.SessionOptions()
    so.enable_profiling = True
    so.profile_file_prefix = os.path.join(tmpdir, f"prof_{task}")
    sess = ort.InferenceSession(model_file, sess_options=so, providers=providers)
    feeds, shapes = zero_feeds(sess.get_inputs(), spatial)
    sess.run(None, feeds)                                  # warm-up (CUDA kernels, allocators)
    times = []
    for _ in range(3):
        t0 = _time.perf_counter()
        sess.run(None, feeds)
        times.append((_time.perf_counter() - t0) * 1000.0)
    prof = sess.end_profiling()
    nodes: dict = {}
    with open(prof, "r", encoding="utf-8") as f:
        for ev in json.load(f):
            if ev.get("cat") == "Node":
                p = (ev.get("args") or {}).get("provider")
                if p:
                    nodes[p] = nodes.get(p, 0) + 1
    return {"session_providers": list(sess.get_providers()), "input_shapes": shapes,
            "run_ms": round(statistics.median(times), 2), "node_providers": nodes}


def session_runs_ok(variant: str, runs: dict) -> bool:
    """9d-r2 (W-34): GPU -- CUDA runs nodes in all four sessions; CPU -- the CPU and only the CPU."""
    if len(runs) != 4:
        return False
    if variant == "gpu":
        return all(r.get("session_providers", [None])[:1] == ["CUDAExecutionProvider"]
                   and (r.get("node_providers") or {}).get("CUDAExecutionProvider", 0) > 0
                   for r in runs.values())
    return all(r.get("session_providers") == ["CPUExecutionProvider"]
               and set(r.get("node_providers") or {}) == {"CPUExecutionProvider"}
               for r in runs.values())


def selfcheck_engine_main(argv) -> int:
    """argv: ["--selfcheck-engine", <models_dir>, "--out", <file>].

    <models_dir> is a buffalo_l pack (read only). Writes one JSON object: frozen, variant (what the
    onnxruntime package in THIS bundle is), the pack check, the providers requested and those
    each session actually runs, init ms, the median of three runs on a synthetic 640x480 frame,
    errors, and any provider-load failure ONNX Runtime printed. Exit 0 = the engine ran on the
    requested providers in every session; 1 = not; 2 = usage."""
    import statistics
    import time as _time
    try:
        models_dir = argv[argv.index("--selfcheck-engine") + 1]
        out = argv[argv.index("--out") + 1]
    except (ValueError, IndexError):
        return 2
    result: dict = {"frozen": bool(getattr(sys, "frozen", False)), "models_dir": models_dir,
                    "errors": [], "sessions": {}, "session_runs": {}, "provider_load_errors": []}
    rc = 1
    err_path = out + ".stderr.txt"
    cap = _capture_native_stderr(err_path)
    log_lines = _ListHandler()
    logging.getLogger().addHandler(log_lines)
    logging.getLogger().setLevel(logging.INFO)
    try:
        from pathlib import Path
        import numpy as np
        from face_service.model_pins import PACK_NAME, check_pack
        pack = Path(models_dir)
        result["pack_problems"] = check_pack(pack, hashes=True) + pack_layout_problems(pack)
        from importlib import metadata
        variant = "unknown"
        for dist, v in (("onnxruntime-gpu", "gpu"), ("onnxruntime", "cpu")):
            try:
                result["onnxruntime_dist"] = f"{dist} {metadata.version(dist)}"
                variant = v
                break
            except metadata.PackageNotFoundError:
                continue
        if variant == "unknown":
            # 9d-build: a frozen bundle carries no dist-info -- the package tells by what it was
            # COMPILED with: the GPU wheel lists the CUDA provider, the CPU wheel never does.
            import onnxruntime as _ort
            variant = "gpu" if "CUDAExecutionProvider" in _ort.get_available_providers() else "cpu"
            result["onnxruntime_dist"] = f"onnxruntime {getattr(_ort, '__version__', '?')} ({variant} build)"
        result["variant"] = variant
        if not result["pack_problems"]:
            from face_service import recognizer as R
            R._prep_cuda_dlls()
            import onnxruntime as ort
            from face_service.ort_privacy import disable_ort_telemetry
            disable_ort_telemetry()
            try:
                ort.preload_dlls()
            except Exception as e:
                result["errors"].append(f"preload_dlls: {e!r}")
            providers, ctx_id = R._select_providers(ort)
            result["available_providers"] = list(ort.get_available_providers())
            result["providers_requested"] = providers
            result["downloads_forbidden"] = forbid_model_downloads()      # W-33
            from insightface.app import FaceAnalysis
            t0 = _time.perf_counter()
            app = FaceAnalysis(name=PACK_NAME, allowed_modules=R.ALLOWED_MODULES,
                               providers=providers, root=str(pack.parent.parent))
            app.prepare(ctx_id=ctx_id, det_size=(R.DET_SIZE, R.DET_SIZE))
            result["init_ms"] = round((_time.perf_counter() - t0) * 1000.0, 1)
            for task, model in getattr(app, "models", {}).items():
                try:
                    result["sessions"][task] = list(model.session.get_providers())
                except Exception as e:
                    result["errors"].append(f"{task}: {e!r}")
            rng = np.random.default_rng(0)
            frame = (rng.random((480, 640, 3)) * 255).astype(np.uint8)   # synthetic, no face
            runs = []
            for _ in range(3):
                t1 = _time.perf_counter()
                app.get(frame)
                runs.append((_time.perf_counter() - t1) * 1000.0)
            result["run_ms"] = [round(x, 1) for x in runs]
            result["run_ms_median"] = round(statistics.median(runs), 1)
            # W-34: every one of the four models run on its own, on a zero input of its shape
            import shutil
            import tempfile
            prof_dir = tempfile.mkdtemp(prefix="fu_engine_prof_")
            try:
                for task, model in getattr(app, "models", {}).items():
                    size = getattr(model, "input_size", None) or (R.DET_SIZE, R.DET_SIZE)
                    try:
                        result["session_runs"][task] = run_session_profiled(
                            ort, model.model_file, providers, (int(size[1]), int(size[0])), prof_dir, task)
                    except Exception as e:
                        result["errors"].append(f"{task} run: {e.__class__.__name__}: {e}")
            finally:
                shutil.rmtree(prof_dir, ignore_errors=True)
            want = "CUDAExecutionProvider" if variant == "gpu" else "CPUExecutionProvider"
            sessions = result["sessions"]
            all_on = bool(sessions) and all(p and p[0] == want for p in sessions.values())
            cpu_only = variant != "cpu" or all(p == ["CPUExecutionProvider"] for p in sessions.values())
            runs_ok = session_runs_ok(variant, result["session_runs"])
            result["all_sessions_on"] = want
            result["ok_sessions"] = all_on and cpu_only and runs_ok
            rc = 0 if (all_on and cpu_only and runs_ok and not result["errors"]) else 1
    except Exception as e:
        result["errors"].append(f"{e.__class__.__name__}: {e}")
        rc = 1
    finally:
        logging.getLogger().removeHandler(log_lines)
        try:
            sys.stderr.flush()
        except Exception:
            pass
        native = ""
        try:
            with open(err_path, "r", encoding="utf-8", errors="replace") as f:
                native = f.read()
        except Exception:
            pass
        # 9d-build: only what ONNX Runtime itself reports about loading a provider library (native
        # stderr) and a session that fell back -- not the recognizer's own provider-selection
        # messages, which name CUDA on every CPU machine.
        low = [ln for ln in native.splitlines()
               if any(k in ln.lower() for k in ("loadlibrary", "onnxruntime_providers_cuda", "cudnn",
                                                  "cublas", "cudart", "error"))]
        low += [ln for ln in log_lines.lines if "fell back" in ln or "LoadLibrary" in ln]
        result["provider_load_errors"] = low[:40]
        if result.get("variant") == "cpu" and any(("cuda" in ln.lower() or "cudnn" in ln.lower())
                                                  for ln in low):
            result["errors"].append("CPU variant: a CUDA provider load was attempted / reported")
            rc = 1
        result["rc"] = rc
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception:
            rc = 1
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass
    return rc
