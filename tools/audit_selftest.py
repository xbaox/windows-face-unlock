"""Stage 2, Step 7 synthetic tests for the AuditLog JSONL writer.

No camera, no service -- a temp dir + a fake clock, small max size to force rotation.

Run:  python -m tools.audit_selftest      (or: python tools/audit_selftest.py)
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from face_service.audit import AuditLog
except ImportError:
    from audit import AuditLog  # type: ignore


_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_append_and_parse() -> None:
    print("audit -- appends valid JSONL with ts/epoch/event")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        a = AuditLog(p, max_mb=5.0, clock=FakeClock())
        a.write("verify", {"verdict": "PASS", "distance": 0.14})
        a.write("unlock", {"verdict": "NOT_LIVE", "outcome": "no-match"})
        rows = _lines(p)
        check("two records written", len(rows), 2)
        check("first event", rows[0]["event"], "verify")
        check("first verdict passthrough", rows[0]["verdict"], "PASS")
        check("has ts", "ts" in rows[0], True)
        check("has epoch", "epoch" in rows[0], True)
        check("second outcome", rows[1]["outcome"], "no-match")


def test_disabled_writes_nothing() -> None:
    print("audit -- disabled -> no file, no write")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        a = AuditLog(p, max_mb=5.0, enabled=False, clock=FakeClock())
        a.write("verify", {"verdict": "PASS"})
        check("no file created", p.exists(), False)
        a.reconfigure(enabled=True, max_mb=5.0)
        a.write("verify", {"verdict": "PASS"})
        check("writes after re-enable", len(_lines(p)), 1)


def test_rotation() -> None:
    print("audit -- rotates by size, keeps numbered backups")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        # ~120-byte records; cap at 300 bytes -> rotate every ~2-3 lines. 2 backups.
        a = AuditLog(p, max_mb=300 / 1_000_000, backups=2, clock=FakeClock())
        for i in range(30):
            a.write("verify", {"verdict": "PASS", "i": i, "pad": "x" * 60})
        check("current file exists", p.exists(), True)
        check("backup .1 exists", p.with_name(p.name + ".1").exists(), True)
        check("backup .2 exists", p.with_name(p.name + ".2").exists(), True)
        # only `backups` numbered files are kept (no .3)
        check("no .3 (bounded)", p.with_name(p.name + ".3").exists(), False)
        # current file stays under the cap-ish (last write always lands)
        check("current under 2x cap", p.stat().st_size <= 600, True)
        # every line across all files is valid JSON
        allp = [p, p.with_name(p.name + ".1"), p.with_name(p.name + ".2")]
        bad = 0
        for fp in allp:
            for ln in fp.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    try:
                        json.loads(ln)
                    except ValueError:
                        bad += 1
        check("all lines valid JSON", bad, 0)


def test_backups_zero_truncates() -> None:
    print("audit -- backups=0 truncates instead of keeping history")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        a = AuditLog(p, max_mb=200 / 1_000_000, backups=0, clock=FakeClock())
        for i in range(20):
            a.write("verify", {"verdict": "PASS", "i": i, "pad": "y" * 60})
        check("no .1 backup with backups=0", p.with_name(p.name + ".1").exists(), False)
        check("file kept small", p.stat().st_size <= 400, True)


def main() -> int:
    for t in (
        test_append_and_parse,
        test_disabled_writes_nothing,
        test_rotation,
        test_backups_zero_truncates,
    ):
        t()
        print()
    print(f"{'FAILED' if _fails else 'OK'}: all checks "
          f"({len(_fails)} failing{': ' + ', '.join(_fails) if _fails else ''})")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
