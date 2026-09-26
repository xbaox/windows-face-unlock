"""tools/contract_selftest.py -- the service <-> Credential Provider contract stays in sync (Stage 9).

B14 N-08 (B2a-17, B2a-20) plus the protocol-v2 constants of act 9b §2.1. Static: no service, no
pipe, no camera. It reads face_service/service.py and credential_provider/PipeClient.{h,cpp}.
  [1] every reason the service can put on the wire for unlock / unlock_gesture has an EXPLICIT
      entry in the CP's FailureClass (the generic "Failed" text is allowed only for the reasons
      listed in GENERIC below, each for a stated reason);
  [2] the protocol version is 2 on both sides, and the CP's budgets exceed the service deadlines
      by at least the 0.5 s reply reserve;
  [3] the CP request builders name exactly the commands and fields the service reads;
  [4] every refusal token of FaceService._refusal() is mapped to "needs attention" on the tile;
  [5] the EN and RU tile tables have the same entries (R3 / R16).

Run:  python -m tools.contract_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import re
from pathlib import Path
import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_contract_")

REPO = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

# Reasons that deliberately fall to the generic "did not complete" text, and why.
GENERIC = {
    "deadline-exceeded": "the service was slow; nothing the user can fix from the tile",
    "engine-error": "an engine fault; the log has the detail",
    "internal-error": "a handler fault; the log has the detail",
    "bad-request": "a CP defect, never user-caused",
    "not-authorized": "the CP is SYSTEM; only a wrong caller sees it",
    "gesture-token-invalid": "phase-2 protocol state; a fresh scan fixes it",
    "grant-unknown": "report_result only; never shown",
    "unknown-command": "transport level",
    "needs-gesture": "not a failure: phase 2 follows",
}


def check(name, cond, got=None):
    print(("  ok    " if cond else "  FAIL  ") + name + ("" if cond or got is None else f" (got={got})"))
    if not cond:
        FAILS.append(name)


def service_reasons(src: str) -> set:
    """Every literal "reason": "<token>" in service.py, plus the refusal tokens."""
    reasons = set(re.findall(r'"reason":\s*"([a-z0-9-]+)"', src))
    reasons |= set(re.findall(r'return "((?:not-owner|custody|no-models|lockout-store-error))"', src))
    # _burst_fault / low-light helpers return tokens by name
    reasons |= set(re.findall(r'return "(no-frames|no-enrollment|engine-error)"', src))
    reasons.add("too-dark")          # lowlight.TOO_DARK_REASON, returned as ll_reason
    return reasons


def cp_explicit(cpp: str) -> set:
    body = cpp.split("Text FailureClass(const std::string& r)", 1)[1].split("\n}\n", 1)[0]
    return set(re.findall(r'r == "([a-z0-9-]+)"', body))


def main() -> int:
    svc = (REPO / "face_service" / "service.py").read_text(encoding="utf-8")
    cpp = (REPO / "credential_provider" / "PipeClient.cpp").read_text(encoding="utf-8")
    hdr = (REPO / "credential_provider" / "PipeClient.h").read_text(encoding="utf-8")

    print("[1] every wire reason is mapped on the tile")
    reasons = service_reasons(svc)
    explicit = cp_explicit(cpp)
    # Not unlock / unlock_gesture replies: an audit-detail tag, clear_enrollment, build_enrollment
    # and calibrate_turn replies.
    wire_only = {"grant-unknown", "camera-leased", "partial", "not-leased", "staging-failed",
                 "turn-too-small"}
    for r in sorted(reasons - wire_only):
        check(f"reason '{r}' has a tile text", r in explicit or r in GENERIC,
              "unmapped -- add it to FailureClass or to GENERIC with a reason")
    check("the explicit map is not shadowing GENERIC", not (explicit & set(GENERIC)), explicit & set(GENERIC))

    print("[2] protocol version and budgets")
    pv = re.search(r"^PROTOCOL_VERSION = (\d+)", svc, re.M)
    cv = re.search(r"constexpr int kProtocolVersion = (\d+);", hdr)
    check("service PROTOCOL_VERSION == CP kProtocolVersion == 2",
          pv and cv and pv.group(1) == cv.group(1) == "2", (pv and pv.group(1), cv and cv.group(1)))
    sd = {k: float(v) for k, v in re.findall(r"^(UNLOCK(?:_GESTURE)?_DEADLINE_S) = ([\d.]+)", svc, re.M)}
    cb = {k: int(v) for k, v in re.findall(r"constexpr DWORD (k\w+TimeoutMs)\s*=\s*(\d+);", hdr)}
    check("phase 1: CP budget - 0.5 s >= service deadline",
          cb["kUnlockTimeoutMs"] / 1000 - 0.5 >= sd["UNLOCK_DEADLINE_S"], (cb, sd))
    check("phase 2: CP budget - 0.5 s >= service deadline",
          cb["kGestureTimeoutMs"] / 1000 - 0.5 >= sd["UNLOCK_GESTURE_DEADLINE_S"], (cb, sd))
    check("service reserve is 0.5 s", re.search(r"^CLIENT_BUDGET_RESERVE_S = 0\.5$", svc, re.M) is not None)
    check("grant report TTL is 30 s (§2.1)", re.search(r"^GRANT_REPORT_TTL_S = 30\.0$", svc, re.M) is not None)

    print("[3] request shapes")
    for cmd, fields in (("unlock", ("v", "budget_ms")), ("unlock_gesture", ("v", "token", "budget_ms")),
                        ("report_result", ("v", "grant_id", "ok"))):
        check(f"CP builds {cmd}", f'\\"cmd\\":\\"{cmd}\\"' in cpp, cmd)
        for f in fields:
            check(f"  ... with {f}", f'\\"{f}\\":' in cpp.split(f'\\"cmd\\":\\"{cmd}\\"', 1)[1][:200], f)
            check(f"  service reads {f}", f'req.get("{f}")' in svc, f)

    print("[4] refusal tokens -> needs attention")
    refusals = set(re.findall(r'return "((?:not-owner|custody|no-models|lockout-store-error))"', svc))
    check("the service has refusal tokens", bool(refusals), refusals)
    na = cpp.split("return Text::NeedsAttention;", 1)[0].rsplit("if (", 1)[1]
    for r in sorted(refusals):
        check(f"refusal '{r}' -> NeedsAttention", f'"{r}"' in na, na[:120])

    print("[5] EN / RU tile tables")
    pairs = re.findall(r"static const TextPair k\w+\s*\{\s*(L\"[^\"]*\")\s*,\s*(L\"[^\"]*\")\s*\};", cpp, re.S)
    check("every tile text has both languages", len(pairs) >= 18 and all(e and r for e, r in pairs), len(pairs))
    lockedsecs = [p for p in pairs if "%u" in p[0]]
    check("the seconds placeholder exists in EN and RU alike", lockedsecs and all("%u" in r for _e, r in lockedsecs))

    if FAILS:
        print(f"\nCONTRACT SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nCONTRACT SELFTEST OK: service reasons, protocol v2, budgets, request fields and tile texts agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
