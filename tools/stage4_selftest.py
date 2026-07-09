"""tools/stage4_selftest.py -- run the whole Stage 4 (channel + custody) selftest set in one go.

Aggregates the Stage-4 reproducible selftests so a single command proves the batch:
  * tools.pipe_hardening_selftest -- DACL + mandatory label, server/client SID reads, unlock SID-gate
  * tools.credentials_selftest    -- per-install entropy, v1->v2 migration, corrupt/missing, DACL

Run:  python -m tools.stage4_selftest
Exit 0 iff every Stage-4 selftest passes.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import credentials_selftest, pipe_hardening_selftest


def main() -> int:
    rc = 0
    for label, mod in (("pipe-hardening (channel)", pipe_hardening_selftest),
                       ("credentials (custody)", credentials_selftest)):
        print(f"\n===== Stage-4 selftest: {label} =====")
        rc |= mod.main()
    print("\n" + ("STAGE 4 SELFTESTS: ALL OK" if rc == 0 else "STAGE 4 SELFTESTS: FAILURE(S) ABOVE"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
