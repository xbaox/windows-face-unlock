"""tools/password_check_selftest.py -- Stage 8b (F-04) proof: the password dialog asks Windows once.

LogonUserW and the Microsoft-account lookup are MOCKED: this test never attempts a logon on any
real account and never opens a window. The save / read-back side is mocked too, so nothing is
written anywhere.
  [1] a password Windows accepts is saved and reported as checked.
  [2] ERROR_LOGON_FAILURE on a plain local account (and on a domain account) -> nothing saved.
  [3] where the refusal is not conclusive -- a Microsoft-account-linked or link-unknown local
      account, an Entra ID ("AzureAD") account, any other error (logon type not granted, expired
      password, ...) -- the password is saved and the status WARNS instead of blocking.
  [4] exactly ONE logon attempt per Save, whatever the outcome (no retries against the lockout
      policy), and the real LogonUser is called with the network logon type.

Run:  python -m tools.password_check_selftest
Exit 0 = all pass; 1 = a failure.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from face_service.i18n import set_language, t
from presence_monitor import password_gui as G

FAILS: list[str] = []
COMPUTER = os.environ.get("COMPUTERNAME", "PC1")


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


def _run(err, *, domain=COMPUTER, linked=False):
    logon, store = _Logon(err), _Store()

    def chk(u, d, p):
        return G.check_windows_password(u, d, p, logon=logon, internet_linked=lambda _u: linked)

    ok, status = G.store_password_checked("alice", "pw", domain, check=chk,
                                          save=store.save, load=store.load)
    return ok, status, logon, store


def main() -> int:
    set_language("en")

    print("[1] accepted password")
    ok, status, logon, store = _run(0)
    check("saved", ok and store.saved == [("alice", "pw", COMPUTER)], store.saved)
    check("status says Windows accepted it", status == t("pwd.status.saved"), status)
    check("one logon attempt", len(logon.calls) == 1, logon.calls)

    print("[2] conclusive refusal -> nothing saved")
    for label, dom in (("local (computer name)", COMPUTER), ("local ('.')", "."),
                       ("domain account", "CONTOSO")):
        ok, status, logon, store = _run(1326, domain=dom, linked=False)
        check(f"{label}: not saved", ok is False and store.saved == [], store.saved)
        check(f"{label}: status is the rejection text",
              status == t("pwd.err.rejected").format(user="alice"), status)
        check(f"{label}: exactly one logon attempt", len(logon.calls) == 1, logon.calls)

    print("[3] inconclusive -> saved with a warning")
    cases = (("MSA-linked local account", 1326, COMPUTER, True),
             ("link state unknown", 1326, COMPUTER, None),
             ("Entra ID account", 1326, "AzureAD", False),
             ("logon type not granted", 1385, COMPUTER, False),
             ("password expired", 1330, COMPUTER, False),
             ("account restriction", 1327, COMPUTER, False))
    for label, err, dom, linked in cases:
        ok, status, logon, store = _run(err, domain=dom, linked=linked)
        check(f"{label}: saved", ok is True and len(store.saved) == 1, store.saved)
        check(f"{label}: status warns and names the error",
              status == t("pwd.status.saved_unverified").format(err=err), status)
        check(f"{label}: exactly one logon attempt", len(logon.calls) == 1, logon.calls)

    print("[4] the real LogonUser call shape (win32security mocked)")
    import win32security  # type: ignore
    seen = []

    class _H:
        def Close(self):
            seen.append("closed")

    real = win32security.LogonUser
    win32security.LogonUser = lambda *a: seen.append(a) or _H()
    try:
        rc = G._logon_user("alice", COMPUTER, "pw")
    finally:
        win32security.LogonUser = real
    check("success -> 0", rc == 0, rc)
    check("network logon, default provider",
          seen and seen[0] == ("alice", COMPUTER, "pw", 3, 0), seen)
    check("the token handle is closed", "closed" in seen, seen)

    ru = set_language("ru") or t("pwd.status.saved_unverified")
    check("ru text exists for the warning", ru != "pwd.status.saved_unverified" and "PIN" in ru, ru)
    set_language("en")

    if FAILS:
        print(f"\nPASSWORD CHECK SELFTEST FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("\nPASSWORD CHECK SELFTEST OK: one network-logon check per Save; a conclusive refusal "
          "saves nothing; an inconclusive one (MSA-linked, Entra ID, other errors) saves and warns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
