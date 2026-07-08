"""Stage 2, Step 5c synthetic tests -- no camera, no GPU, no InsightFace.

Covers:
  A) liveness.verdict() decision matrix (fast + paranoid, both challenge_on_doubt values,
     recognition gate, screen/blink/margin doubt, boundary values, unknown-mode fallback).
  B) config.py Stage-2 fields: defaults, validate() accepts good values and rejects bad ones,
     and an old config dict (missing the new keys) still constructs with defaults.

Run from the repo root:  python tools/liveness_verdict_selftest.py
Exit code 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import sys

try:  # repo layout on the target machine
    from face_service.liveness import verdict, Verdict
    from face_service.config import Config
except ImportError:  # flat layout (container check)
    from liveness import verdict, Verdict  # type: ignore
    from config import Config  # type: ignore


REQ = 2  # cfg.verify_required in these cases

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got}, want={want}")
    if not ok:
        _fails.append(name)


def v(**kw) -> Verdict:
    kw.setdefault("required_matches", REQ)
    return verdict(**kw)


def test_verdict_fast() -> None:
    print("verdict() -- fast mode (challenge_on_doubt=True)")
    # happy path: clean texture + confident match -> in, blink not required
    check("strong self (blink)",
          v(matches=3, blinked=True,  screen_frac=0.0, margin=0.38, mode="fast"), Verdict.PASS)
    check("strong self (no blink, clean)",
          v(matches=3, blinked=False, screen_frac=0.0, margin=0.38, mode="fast"), Verdict.PASS)
    # recognition gate
    check("too few matches",
          v(matches=1, blinked=True,  screen_frac=0.0, margin=0.30, mode="fast"), Verdict.NOT_LIVE)
    check("zero match (neg margin)",
          v(matches=0, blinked=False, screen_frac=0.0, margin=-0.1, mode="fast"), Verdict.NOT_LIVE)
    # static spoof: flagged screen/photo that never blinked -> hard reject
    check("photo / static screen",
          v(matches=3, blinked=False, screen_frac=0.60, margin=0.30, mode="fast"), Verdict.NOT_LIVE)
    # video replay: flagged screen but it 'blinks' -> escalate
    check("video replay (flagged+blink)",
          v(matches=3, blinked=True,  screen_frac=0.60, margin=0.30, mode="fast"),
          Verdict.NEEDS_GESTURE)
    # thin recognition margin on clean texture -> escalate
    check("thin margin, clean, blink",
          v(matches=2, blinked=True,  screen_frac=0.0, margin=0.05, mode="fast"),
          Verdict.NEEDS_GESTURE)
    check("thin margin, clean, no blink",
          v(matches=2, blinked=False, screen_frac=0.0, margin=0.05, mode="fast"),
          Verdict.NEEDS_GESTURE)
    # boundaries
    check("screen_frac at boundary (0.34 == thr)",
          v(matches=3, blinked=True,  screen_frac=0.34, margin=0.30, mode="fast"),
          Verdict.NEEDS_GESTURE)
    check("one noisy flagged frame (0.2 < thr) -> not doubt",
          v(matches=3, blinked=False, screen_frac=0.20, margin=0.30, mode="fast"), Verdict.PASS)
    check("margin at boundary (0.10 == strong) -> confident",
          v(matches=3, blinked=True,  screen_frac=0.0, margin=0.10, mode="fast"), Verdict.PASS)
    check("matches == required exactly",
          v(matches=2, blinked=False, screen_frac=0.0, margin=0.30, mode="fast"), Verdict.PASS)


def test_verdict_fast_no_challenge() -> None:
    print("verdict() -- fast mode (challenge_on_doubt=False -> doubt hard-denies)")
    check("video replay -> deny",
          v(matches=3, blinked=True, screen_frac=0.60, margin=0.30, mode="fast",
            challenge_on_doubt=False), Verdict.NOT_LIVE)
    check("thin margin -> deny",
          v(matches=2, blinked=True, screen_frac=0.0, margin=0.05, mode="fast",
            challenge_on_doubt=False), Verdict.NOT_LIVE)
    check("clean + confident still passes",
          v(matches=3, blinked=True, screen_frac=0.0, margin=0.38, mode="fast",
            challenge_on_doubt=False), Verdict.PASS)


def test_verdict_paranoid() -> None:
    print("verdict() -- paranoid mode (always challenge a valid match)")
    check("strong self -> gesture",
          v(matches=3, blinked=True,  screen_frac=0.0, margin=0.38, mode="paranoid"),
          Verdict.NEEDS_GESTURE)
    check("strong self, no blink -> gesture",
          v(matches=3, blinked=False, screen_frac=0.0, margin=0.38, mode="paranoid"),
          Verdict.NEEDS_GESTURE)
    check("static spoof -> hard reject",
          v(matches=3, blinked=False, screen_frac=0.60, margin=0.30, mode="paranoid"),
          Verdict.NOT_LIVE)
    check("video replay (flagged+blink) -> gesture",
          v(matches=3, blinked=True,  screen_frac=0.60, margin=0.30, mode="paranoid"),
          Verdict.NEEDS_GESTURE)
    check("too few matches -> deny",
          v(matches=1, blinked=True,  screen_frac=0.0, margin=0.30, mode="paranoid"),
          Verdict.NOT_LIVE)
    check("thin margin still gestures (paranoid ignores margin shortcut)",
          v(matches=2, blinked=True,  screen_frac=0.0, margin=0.05, mode="paranoid"),
          Verdict.NEEDS_GESTURE)


def test_verdict_unknown_mode() -> None:
    print("verdict() -- unknown mode falls back to fast")
    check("mode='banana' behaves as fast",
          v(matches=3, blinked=True, screen_frac=0.0, margin=0.38, mode="banana"), Verdict.PASS)


def test_config_stage2() -> None:
    print("config.py -- Stage 2 fields + validate()")
    c = Config()
    check("default liveness_mode", c.liveness_mode, "fast")
    check("default blink_timeout_s", c.blink_timeout_s, 4.0)
    check("default challenge_on_doubt", c.challenge_on_doubt, True)
    check("default anti_screen", c.anti_screen, True)
    check("default max_face_attempts", c.max_face_attempts, 5)
    check("default lockout_seconds", c.lockout_seconds, 300)

    try:
        c.validate()
        check("validate() accepts defaults", True, True)
    except Exception as e:  # noqa: BLE001
        check("validate() accepts defaults", f"raised {e!r}", True)

    bad_cases = {
        "liveness_mode=turbo": dict(liveness_mode="turbo"),
        "blink_timeout_s=0": dict(blink_timeout_s=0.0),
        "max_face_attempts=0": dict(max_face_attempts=0),
        "lockout_seconds=-1": dict(lockout_seconds=-1),
    }
    for label, kw in bad_cases.items():
        try:
            Config(**kw).validate()
            check(f"validate() rejects {label}", "no error", "ValueError")
        except ValueError:
            check(f"validate() rejects {label}", "ValueError", "ValueError")

    # An old config dict (pre-Stage-2) still constructs with the new defaults.
    old = Config(**{"threshold": 0.4, "verify_frames": 5})
    check("old config -> new field defaulted", old.max_face_attempts, 5)


def main() -> int:
    for t in (
        test_verdict_fast,
        test_verdict_fast_no_challenge,
        test_verdict_paranoid,
        test_verdict_unknown_mode,
        test_config_stage2,
    ):
        t()
        print()
    total = "some" if _fails else "all"
    print(f"{'FAILED' if _fails else 'OK'}: {total} checks "
          f"({len(_fails)} failing{': ' + ', '.join(_fails) if _fails else ''})")
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
