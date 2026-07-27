#!/usr/bin/env python3
"""Self-test: config.example.toml must not drift from face_service.config.Config.

The example doubles as the user-facing reference for every default, and
setup.ps1 used to copy it straight into %USERPROFILE%\\.face-unlock\\config.toml
-- so a stale value in it was a live behaviour change, not a doc typo. This
pins the two together:

  1. the example's key set equals Config.__dataclass_fields__ exactly
     (no missing keys, no leftovers from removed knobs)
  2. every value equals the Config() default

``language`` is the one documented exception on (2): its default is resolved at
runtime from the system locale, so the example carries a fixed, valid code
instead. It is still required to be present, and still has to be a code the
service accepts.

No camera, no service, no filesystem writes.

Run from repo root:
    python tools\\config_example_selftest.py
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service.config import Config
from face_service.i18n import LANG_CODES

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"

# Fields whose default is environment-derived, so the example cannot mirror it.
# Each entry must still be present in the example and still has to validate.
RUNTIME_DEFAULT_FIELDS = {"language"}


class T:
    def __init__(self):
        self.fail = 0

    def ok(self, cond, msg):
        print(("  ok  " if cond else "  FAIL") + "  " + msg)
        if not cond:
            self.fail += 1


def main(argv=None) -> int:
    t = T()
    defaults = Config()
    declared = set(Config.__dataclass_fields__)

    print(f"[0] parse {EXAMPLE.name}")
    t.ok(EXAMPLE.is_file(), f"{EXAMPLE} exists")
    if not EXAMPLE.is_file():
        return 1
    raw = EXAMPLE.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        print(f"  FAIL  config.example.toml is not valid TOML: {e}")
        return 1
    t.ok(True, f"valid TOML, {len(data)} top-level keys")

    # A [section] header would nest keys into sub-tables and Config.load() would
    # silently drop the lot, so the file has to stay flat.
    nested = sorted(k for k, v in data.items() if isinstance(v, dict))
    t.ok(not nested, f"flat file, no TOML tables (found: {nested or 'none'})")

    print("\n[1] key set == Config.__dataclass_fields__")
    present = set(data)
    missing = sorted(declared - present)
    extra = sorted(present - declared)
    t.ok(not missing, f"no Config field missing from the example (missing: {missing or 'none'})")
    t.ok(not extra, f"no key in the example that Config would ignore (extra: {extra or 'none'})")
    t.ok(len(present) == len(declared),
         f"counts match: example {len(present)} == Config {len(declared)}")

    print("\n[2] values == Config() defaults")
    mismatched = []
    for name in sorted(declared & present):
        if name in RUNTIME_DEFAULT_FIELDS:
            continue
        want = getattr(defaults, name)
        got = data[name]
        # TOML has no int/float distinction on the way in for whole numbers, so
        # compare type too: a float knob written as `5` would come back int and
        # then propagate an int where the code expects a float.
        if got != want or type(got) is not type(want):
            mismatched.append(f"{name}: example={got!r} ({type(got).__name__}) "
                              f"vs default={want!r} ({type(want).__name__})")
    t.ok(not mismatched, f"all {len(declared) - len(RUNTIME_DEFAULT_FIELDS)} "
                         f"non-runtime defaults match")
    for m in mismatched:
        print(f"          {m}")

    print("\n[3] runtime-default fields")
    for name in sorted(RUNTIME_DEFAULT_FIELDS):
        t.ok(name in data, f"{name} is present in the example (documented exception on value)")
    if "language" in data:
        t.ok(data["language"] in LANG_CODES,
             f"language={data['language']!r} is a supported code")

    print("\n[4] the example still loads into a valid Config")
    try:
        cfg = Config(**{k: v for k, v in data.items() if k in declared})
        cfg.validate()
        t.ok(True, "Config(**example).validate() passes")
    except Exception as e:  # noqa: BLE001 - surface whatever validate() rejects
        t.ok(False, f"Config(**example).validate() raised: {e}")

    print()
    if t.fail:
        print(f"CONFIG EXAMPLE SELFTEST FAILED: {t.fail} check(s) failed.")
        return 1
    print("CONFIG EXAMPLE SELFTEST OK: config.example.toml lists every Config field "
          "exactly once, every value equals the code default (language excepted), "
          "and the result validates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
