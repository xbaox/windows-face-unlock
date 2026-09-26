"""tools/password_check_selftest.py -- the password dialog asks Windows once (8b F-04; Stage 9 R13).

LogonUserW, the account-name lookup and the save/read-back are MOCKED: no logon is attempted on any
real account, no window opens, nothing is written.
  [1] a password Windows accepts is saved, status green ("ok").
  [2] unambiguous refusals -- wrong password 1326, restriction 1327 (e.g. blank), disabled 1331,
      expired account 1793, locked out 1909 -- save NOTHING, each with its own text (red).
  [3] ambiguous answers (no logon server, expired password, logon type not granted, anything else)
      save the password with an amber warning and the next step.
  [4] exactly ONE logon attempt per Save, and the real one is the INTERACTIVE logon type through
      the default (Negotiate) provider -- the form the lock screen uses.
  [5] the identity comes from the token: COMPUTER\\SAM for a local / Microsoft-linked account,
      the UPN with an empty domain for Entra ID (S-1-12-1), the SAM form flagged when Entra
      reports no UPN.

Run:  python -m tools.password_check_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import testhome  # noqa: E402  (Stage 9, R20: isolation before any product import)
testhome.isolate("faceunlock_password_check_")

from face_service.i18n import set_language, t
from presence_monitor import password_gui as G

FAILS: list[str] = []


def check(name, cond, got=None):
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f" (got={got!r})" if got is not None else ""))
        FAILS.append(name)


class _Logon:
    def __init__(self, err: int):
        self.err = err
        self.calls: list = []

    def __call__(self, user, domain, password):
        self.calls.append((user, domain, password))
        return self.err


class _Store:
    def __init__(self):
        self.saved: list = []

    def save(self, u, p, d):
        self.saved.append((u, p, d))

    def load(self):
        u, p, d = self.saved[-1]
        return {"u": u, "p": p, "d": d}


def _run(err, *, user="alice", domain="PC1"):
    logon, store = _Logon(err), _Store()
    level, text = G.store_password_checked(
        user, "pw", domain, check=lambda u, d, p: G.check_windows_password(u, d, p, logon=logon),
        save=store.save, load=store.load)
    return level, text, logon, store


def main() -> int:
    set_language("en")

    print("[1] accepted password")
    level, text, logon, store = _run(0)
    check("saved, green", level == "ok" and store.saved == [("alice", "pw", "PC1")], (level, store.saved))
    check("says Windows accepted it", text == t("pwd.status.saved"), text)

    print("[2] unambiguous refusals save nothing")
    for err, key in ((1326, "pwd.err.rejected"), (1327, "pwd.err.restricted"),
                     (1331, "pwd.err.disabled"), (1793, "pwd.err.account_expired"),
                     (1909, "pwd.err.locked_out")):
        level, text, logon, store = _run(err)
        check(f"{err}: nothing saved, red, own text", level == "err" and not store.saved
              and text == t(key, user="alice"), (level, text))

    print("[3] ambiguous answers save with an amber warning")
    for err in (1311, 1330, 1385, 1907, 5, 87):
        level, text, logon, store = _run(err)
        check(f"{err}: saved with a warning", level == "warn" and store.saved and str(err) in text,
              (level, text))

    print("[4] one interactive logon per Save")
    for err in (0, 1326, 1311):
        _l, _t, logon, _s = _run(err)
        check(f"err={err}: exactly one attempt", len(logon.calls) == 1, logon.calls)
    src = Path(G.__file__).read_text(encoding="utf-8")
    check("the real check uses LOGON32_LOGON_INTERACTIVE", "_LOGON32_LOGON_INTERACTIVE = 2" in src
          and "_LOGON32_LOGON_INTERACTIVE, _LOGON32_PROVIDER_DEFAULT" in src)
    check("no network logon any more", "LOGON32_LOGON_NETWORK" not in src)

    print("[5] identity from the token")
    names = {2: "PC1\\alice", 8: ""}
    check("local / MSA-linked: COMPUTER\\SAM",
          G.account_identity(sid="S-1-5-21-1-2-3-1001", name_ex=lambda f: names[f]) == ("alice", "PC1", "local"))
    upn = {2: "AzureAD\\ann", 8: "ann@contoso.com"}
    check("Entra: UPN with an empty domain",
          G.account_identity(sid="S-1-12-1-1-2-3-4", name_ex=lambda f: upn[f]) == ("ann@contoso.com", "", "entra"))
    noupn = {2: "AzureAD\\ann", 8: ""}
    check("Entra without a UPN: the SAM form, flagged",
          G.account_identity(sid="S-1-12-1-1-2-3-4", name_ex=lambda f: noupn[f]) == ("ann", "AzureAD", "entra-sam"))
    check("the identity is not taken from %USERNAME% when the token answers",
          "os.environ.get(\"USERNAME\"" not in src.split("def account_identity", 1)[1].split("return user", 1)[0]
          .split("else:", 1)[0])

    print()
    if FAILS:
        print(f"PASSWORD CHECK SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("PASSWORD CHECK SELFTEST OK: one interactive logon per Save; unambiguous refusals save "
          "nothing, ambiguous answers save with a warning; the identity comes from the token "
          "(UPN for Entra ID).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
