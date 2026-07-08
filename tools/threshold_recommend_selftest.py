"""Stage 2, Step 9 -- synthetic tests for measure_threshold.recommend_threshold (no camera).

Run:  python -m tools.threshold_recommend_selftest
Exit 0 = all green, 1 = any failure.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from tools.measure_threshold import recommend_threshold
except ImportError:
    from measure_threshold import recommend_threshold  # type: ignore


_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


def approx(name: str, got, want, tol=1e-6) -> None:
    ok = got is not None and abs(got - want) <= tol
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want~={want}")
    if not ok:
        _fails.append(name)


def test_separated() -> None:
    print("recommend -- genuine well below impostor (typical ArcFace)")
    r = recommend_threshold({"self": [0.08, 0.10, 0.14], "impostor": [1.0, 1.1]},
                            current=0.45)
    approx("genuine_max", r["genuine_max"], 0.14)
    approx("impostor_min", r["impostor_min"], 1.0)
    check("separated", r["separated"], True)
    approx("gap", r["gap"], 0.86)
    # security-biased: gmax + 0.12 buffer = 0.26 (well clear of impostor)
    approx("suggested", r["suggested"], 0.26)
    check("suggested between gmax and imin",
          r["genuine_max"] < r["suggested"] < r["impostor_min"], True)


def test_overlap_flagged() -> None:
    print("recommend -- overlapping genuine/impostor -> flagged, not separated")
    r = recommend_threshold({"self": [0.10, 0.50], "impostor": [0.40, 1.0]}, current=0.45)
    check("separated False", r["separated"], False)
    approx("overlap", r["overlap"], 0.10)
    check("suggested present", "suggested" in r, True)


def test_genuine_only() -> None:
    print("recommend -- genuine only (no impostor samples yet)")
    r = recommend_threshold({"self": [0.10, 0.14, 0.09]}, current=0.45)
    approx("suggested = gmax+buffer", r["suggested"], 0.26)
    check("no separated key", "separated" in r, False)
    check("impostor_n zero", r["impostor_n"], 0)


def test_clamp_below_impostor() -> None:
    print("recommend -- buffer clamped to stay clear below a close impostor min")
    # gmax 0.30, buffer -> 0.42, but impostor_min 0.40 -> clamp to 0.35
    r = recommend_threshold({"self": [0.20, 0.30], "impostor": [0.40, 0.55]}, current=0.45)
    check("separated", r["separated"], True)
    approx("suggested clamped", r["suggested"], 0.35)  # min(0.42, 0.40-0.05)=0.35


def test_stats() -> None:
    print("recommend -- basic stat correctness")
    r = recommend_threshold({"self-a": [0.1, 0.2], "self-b": [0.3]}, current=0.4)
    approx("genuine_max across self-* labels", r["genuine_max"], 0.3)
    approx("genuine_mean", r["genuine_mean"], round((0.1 + 0.2 + 0.3) / 3, 4))
    check("genuine_n", r["genuine_n"], 3)


def main() -> int:
    for t in (test_separated, test_overlap_flagged, test_genuine_only,
              test_clamp_below_impostor, test_stats):
        t()
        print()
    print(f"{'FAILED' if _fails else 'OK'}: all checks "
          f"({len(_fails)} failing{': ' + ', '.join(_fails) if _fails else ''})")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
