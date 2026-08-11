#!/usr/bin/env python3
"""Self-test: Config.load() degrades safely, and validate() covers every field.

Stage 7d-A. Two behaviours are pinned here, both of which used to be missing:

  1. THE LOAD PATH VALIDATES. Config.load() used to construct a Config straight from the TOML
     and hand it back unchecked -- validate() ran only on reload_config and on GUI save. A
     hand-edited config.toml with threshold = 5.0 was therefore rejected loudly if you touched
     anything through Settings, and accepted in silence on a cold service start.

  2. THE LOAD PATH SURVIVES A BROKEN FILE. tomllib.loads() was unwrapped and main() calls
     Config.load() before the pipe exists, so one stray character killed the service at startup
     -- and the watchdog then restarted it into the same failure forever.

The two modes are exercised separately:
  * Config.load()             -> never raises; logs ERROR and returns full defaults
  * Config.load(strict=True)  -> raises ValueError; used by reload_config, where a good config
                                 is already live and must be kept rather than replaced

Every case runs in a SUBPROCESS with FACE_UNLOCK_HOME pointed at a fresh temp directory, because
APP_DIR / CONFIG_PATH are module-level constants resolved at import time -- they cannot be
repointed inside a running interpreter. The child asserts it is looking at the temp path before it
touches anything, so this test can never read or write the real ~/.face-unlock.

No camera, no service, no pipe, no mutex, no writes outside the temp directory.

Run from repo root:
    python tools\\config_validation_selftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "@@RESULT@@"

# Runs inside the child. Captures the config logger's records, then reports what each mode did.
# __REPO_ROOT__ is substituted by str.replace, not %-formatting: the body uses % itself.
CHILD = r'''
import json, logging, os, sys
sys.path.insert(0, __REPO_ROOT__)

records = []


class _Capture(logging.Handler):
    def emit(self, record):
        records.append((record.levelname, record.getMessage()))


_log = logging.getLogger("face_service.config")
_log.addHandler(_Capture())
_log.setLevel(logging.DEBUG)

from face_service.config import Config, CONFIG_PATH, APP_DIR

# Hard safety gate: refuse to run against anything but the temp home handed to us.
expected_home = os.environ["FACE_UNLOCK_HOME"]
if os.path.normcase(str(APP_DIR)) != os.path.normcase(expected_home):
    print("@@RESULT@@" + json.dumps(
        {"fatal": "APP_DIR is " + str(APP_DIR) + ", expected " + expected_home}))
    raise SystemExit(0)

out = {}

try:
    cfg = Config.load()
    ref = Config()
    got = {f: getattr(cfg, f) for f in Config.__dataclass_fields__}
    want = {f: getattr(ref, f) for f in Config.__dataclass_fields__}
    got.pop("language", None)
    want.pop("language", None)
    out["lenient"] = {
        "raised": None,
        "defaults": got == want,
        "probe": cfg.presence_interval_s,
    }
except BaseException as e:
    out["lenient"] = {"raised": type(e).__name__, "msg": str(e)}

try:
    Config.load(strict=True)
    out["strict"] = {"raised": None}
except BaseException as e:
    out["strict"] = {"raised": type(e).__name__, "msg": str(e)}

out["levels"] = [lvl for lvl, _ in records]
out["messages"] = [msg for _, msg in records]
print("@@RESULT@@" + json.dumps(out))
'''


# content: str -> written UTF-8; bytes -> written raw; None -> no file at all.
# expect_lenient: "defaults" | "loaded"
# expect_strict:  "raise" | "ok"
# needle: substring required in the ERROR message (None = no ERROR expected)
CASES = [
    dict(name="no config file at all",
         content=None, expect_lenient="defaults", expect_strict="ok",
         needle=None, warn=None),
    dict(name="valid config loads and is kept",
         content="presence_interval_s = 61\n",
         expect_lenient="loaded", expect_strict="ok", needle=None, warn=None),
    dict(name="unknown keys warn by name, config still loads",
         content="presence_interval_s = 61\ntreshold = 0.9\nzzz_bogus = 1\n",
         expect_lenient="loaded", expect_strict="ok", needle=None,
         warn=["treshold", "zzz_bogus"]),
    dict(name="malformed TOML",
         content="threshold = = 0.5\n",
         expect_lenient="defaults", expect_strict="raise", needle="not valid TOML", warn=None),
    dict(name="non-UTF-8 bytes",
         content=b"threshold = 0.5\n# \xff\xfe not utf-8\n",
         expect_lenient="defaults", expect_strict="raise", needle="not valid UTF-8", warn=None),
    dict(name="threshold out of range",
         content="threshold = 5.0\n",
         expect_lenient="defaults", expect_strict="raise", needle="threshold", warn=None),
    dict(name="threshold wrong type (string) -> TypeError path",
         content='threshold = "0.5"\n',
         expect_lenient="defaults", expect_strict="raise", needle="failed validation", warn=None),
    dict(name="verify_required > verify_frames",
         content="verify_frames = 3\nverify_required = 5\n",
         expect_lenient="defaults", expect_strict="raise", needle="verify_required", warn=None),
    dict(name="verify_frames = 0",
         content="verify_frames = 0\nverify_required = 0\n",
         expect_lenient="defaults", expect_strict="raise", needle="verify_frames", warn=None),
    dict(name="distance_metric not implemented",
         content='distance_metric = "euclidean"\n',
         expect_lenient="defaults", expect_strict="raise", needle="not implemented", warn=None),
    dict(name="camera_index negative",
         content="camera_index = -1\n",
         expect_lenient="defaults", expect_strict="raise", needle="camera_index", warn=None),
    dict(name="camera_warmup_frames negative",
         content="camera_warmup_frames = -5\n",
         expect_lenient="defaults", expect_strict="raise",
         needle="camera_warmup_frames", warn=None),
    dict(name="persistent_camera not a boolean",
         content="persistent_camera = 1\n",
         expect_lenient="defaults", expect_strict="raise",
         needle="persistent_camera", warn=None),
    dict(name="warmup_on_start not a boolean",
         content="warmup_on_start = 0\n",
         expect_lenient="defaults", expect_strict="raise", needle="warmup_on_start", warn=None),
    dict(name="challenge_on_doubt not a boolean",
         content="challenge_on_doubt = 1\n",
         expect_lenient="defaults", expect_strict="raise",
         needle="challenge_on_doubt", warn=None),
    dict(name="anti_screen not a boolean",
         content='anti_screen = "yes"\n',
         expect_lenient="defaults", expect_strict="raise", needle="anti_screen", warn=None),
    dict(name="audit_log not a boolean",
         content="audit_log = 1\n",
         expect_lenient="defaults", expect_strict="raise", needle="audit_log", warn=None),
    # Proves the isinstance check sits OUTSIDE `if self.adaptive_gallery:` -- a truthy non-bool
    # used to enter that branch and be judged only by the checks inside it.
    dict(name="adaptive_gallery not a boolean (checked outside the adaptive branch)",
         content="adaptive_gallery = 1\n",
         expect_lenient="defaults", expect_strict="raise",
         needle="adaptive_gallery must be a boolean", warn=None),
    # 7h. The diagnostics knob writes RAW FACE IMAGERY to disk when it is on, so a truthy
    # non-bool must not be able to switch it on by accident -- same shape as the toggles above,
    # stricter consequence if it were missed.
    dict(name="debug_dump_frames not a boolean",
         content="debug_dump_frames = 1\n",
         expect_lenient="defaults", expect_strict="raise",
         needle="debug_dump_frames must be a boolean", warn=None),
]


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def run_case(case) -> dict:
    with tempfile.TemporaryDirectory(prefix="fu-cfgtest-") as tmp:
        home = Path(tmp)
        content = case["content"]
        if content is not None:
            target = home / "config.toml"
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")

        env = dict(os.environ)
        env["FACE_UNLOCK_HOME"] = str(home)
        proc = subprocess.run(
            [sys.executable, "-c", CHILD.replace("__REPO_ROOT__", repr(str(REPO_ROOT)))],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        line = ""
        for candidate in proc.stdout.splitlines():
            if candidate.startswith(SENTINEL):
                line = candidate[len(SENTINEL):]
        if not line:
            return {"fatal": f"child produced no result (rc={proc.returncode}); "
                             f"stderr={proc.stderr.strip()[:400]}"}
        result = json.loads(line)
        # The file must survive the read untouched -- load() is not allowed to rewrite it.
        if isinstance(content, str):
            result["file_intact"] = (home / "config.toml").read_text(encoding="utf-8") == content
        elif isinstance(content, bytes):
            result["file_intact"] = (home / "config.toml").read_bytes() == content
        else:
            result["file_intact"] = True
        return result


def main(argv=None) -> int:
    t = T()
    print(f"[0] harness: {len(CASES)} cases, each in its own subprocess with a temp "
          f"FACE_UNLOCK_HOME")
    print(f"      interpreter: {sys.executable}")

    for i, case in enumerate(CASES, 1):
        print(f"\n[{i}] {case['name']}")
        r = run_case(case)
        if "fatal" in r:
            t.ok(False, f"child aborted: {r['fatal']}")
            continue

        lenient = r.get("lenient", {})
        strict = r.get("strict", {})
        errors = [m for lvl, m in zip(r["levels"], r["messages"]) if lvl == "ERROR"]
        warnings = [m for lvl, m in zip(r["levels"], r["messages"]) if lvl == "WARNING"]

        # 1. lenient mode never raises, whatever the file looks like
        t.ok(lenient.get("raised") is None,
             f"Config.load() did not raise (got {lenient.get('raised')!r})")

        # 2. lenient mode landed on the right config
        if case["expect_lenient"] == "defaults":
            t.ok(lenient.get("defaults") is True,
                 "fell back to full built-in defaults")
        else:
            t.ok(lenient.get("defaults") is False and lenient.get("probe") == 61,
                 f"kept the file's values (presence_interval_s={lenient.get('probe')!r})")

        # 3. strict mode raises exactly where it should
        if case["expect_strict"] == "raise":
            t.ok(strict.get("raised") == "ValueError",
                 f"Config.load(strict=True) raised ValueError (got {strict.get('raised')!r})")
        else:
            t.ok(strict.get("raised") is None,
                 f"Config.load(strict=True) accepted the file (got {strict.get('raised')!r})")

        # 4. the failure was reported, and names the offending field
        if case["needle"]:
            t.ok(len(errors) == 1, f"exactly one ERROR logged (got {len(errors)})")
            t.ok(any(case["needle"] in m for m in errors),
                 f"ERROR names the problem ({case['needle']!r})")
        else:
            t.ok(not errors, f"no ERROR logged (got {errors or 'none'})")

        # 5. unknown keys are named in a WARNING, and are not fatal.
        # The child loads TWICE (lenient then strict) and the warning is emitted per call, so two
        # is the correct count here. The ERROR above is one, not two, because strict mode raises
        # instead of logging -- which is exactly the difference the two modes are meant to have.
        if case["warn"]:
            t.ok(len(warnings) == 2,
                 f"a WARNING per load() call, both loads warned (got {len(warnings)})")
            t.ok(all(any(k in m for m in warnings) for k in case["warn"]),
                 f"WARNING names every unknown key ({', '.join(case['warn'])})")
        else:
            t.ok(not warnings, f"no WARNING logged (got {warnings or 'none'})")

        # 6. reading a config never rewrites it
        t.ok(r.get("file_intact") is True, "the config file was not modified by load()")

    print()
    if t.fail:
        print(f"CONFIG VALIDATION SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("CONFIG VALIDATION SELFTEST OK: Config.load() degrades to defaults with a loud ERROR "
          "on every broken-file mode, names unknown keys, validates on the load path, and "
          "Config.load(strict=True) raises instead so reload_config can keep the live config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
