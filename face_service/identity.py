"""Who is who: the SID of this process and the SID of the product's owner (Stage 9, R1).

Stage 9 (D-74). Defect: the "current user SID" helper existed five times (config, credentials,
datadir, pipe_io, service). Consequence: the owner check R1 adds would have needed a sixth copy.
Fix: one module, no heavy imports, safe to load from any process.

Stage 9 (R1, F-83 / F-103). The product is strictly single-user. The owner is the user of the
ACTIVE CONSOLE SESSION when the product was installed; the installer records that user's SID in
``HKLM\\Software\\WindowsFaceUnlock\\OriginalUserSid`` (the key is writable by administrators
only). The service refuses face functions unless its own SID is that owner, the Credential
Provider shows its tile only to that owner and trusts a pipe server only when the server runs as
that owner. Other accounts on the PC sign in exactly as before.

Accepted owner SID forms: a local or domain account (``S-1-5-21-...``) and an Entra ID account
(``S-1-12-1-...``). SYSTEM and service accounts can never be the owner.
"""
from __future__ import annotations

import functools
import re

OWNER_REG_KEY = r"Software\WindowsFaceUnlock"
OWNER_REG_VALUE = "OriginalUserSid"
SYSTEM_SID = "S-1-5-18"

# S-1-5-21-a-b-c-RID (local/domain) or S-1-12-1-a-b-c-d (Entra ID). Nothing else is a person.
_USER_SID_RE = re.compile(r"^S-1-(?:5-21|12-1)(?:-\d{1,10}){4,}$")


def is_user_sid(sid: object) -> bool:
    """True only for a SID that can belong to a person: S-1-5-21-* or S-1-12-1-*."""
    return isinstance(sid, str) and bool(_USER_SID_RE.match(sid))


@functools.lru_cache(maxsize=1)
def current_user_sid() -> str:
    """The process token's user SID, read once per process (it cannot change). A failure raises
    and is not cached, so the next call tries again."""
    import win32api, win32con, win32security  # noqa: E401  (lazy: keeps plain imports cheap)
    th = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(th)
    s = win32security.ConvertSidToStringSid(sid)
    if not s:
        raise ValueError("current-user SID resolved empty")
    return s


def owner_sid() -> "str | None":
    """The recorded owner SID, or None when it is missing or not a person's SID. Read fresh on
    every call from the 64-bit registry view (the installer writes it from a 32-bit process in
    64-bit install mode, which lands in the native view)."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, OWNER_REG_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            value, kind = winreg.QueryValueEx(k, OWNER_REG_VALUE)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
        return None
    value = str(value).strip()
    return value if is_user_sid(value) else None


def owner_check(recorded: "str | None" = None, me: "str | None" = None) -> "str | None":
    """None when this process runs as the owner; otherwise a short reason for the log.

    Both inputs are injectable for the selftests; production passes nothing."""
    recorded = owner_sid() if recorded is None else recorded
    if not is_user_sid(recorded):
        return "no owner recorded (HKLM\\%s\\%s missing or invalid)" % (OWNER_REG_KEY,
                                                                       OWNER_REG_VALUE)
    try:
        me = current_user_sid() if me is None else me
    except Exception as e:
        return "own SID unreadable (%r)" % (e,)
    if me != recorded:
        return "this process runs as %s, the owner is %s" % (me, recorded)
    return None
