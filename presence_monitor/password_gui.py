"""Minimal tkinter dialog for the DPAPI-stored Windows password.

Stage 7d-E. The tray has always offered "Set Windows password…", and it did it by spawning
``python -m tools.set_password`` in a new console: an interactive ``getpass`` prompt. That cannot
survive freezing. There is no ``-m`` entry point in a bundle, ``tools/`` is not shipped inside the
installer at all, and ``tools.set_password`` is not even a hidden import -- so the menu item would
have started a second tray instead of a password prompt.

This is the GUI half of that fix: same three inputs, same call into ``face_service.credentials``,
same DPAPI read-back sanity check, no console. ``tools/set_password.py`` stays exactly as it is --
it is the dev/scriptable path (``--clear`` included) and nothing here replaces it.

Deliberately small: no camera, no pipe, no service. The only side effect is the one the dialog
exists to perform -- writing (or clearing) ``credentials.bin`` through the existing helpers, which
own the DPAPI entropy and the file DACL.

Stage 8b (F-04). Defect: the password was saved after pw == pw2 and a DPAPI read-back only; nothing
ever asked Windows whether it is the account's password. Consequence: a typo at onboarding, or a
later Windows password change, turned every successful face scan into a failed logon at the lock
screen, and repeated failures can trip the account-lockout policy -- which blocks PIN sign-in too.
Fix: before saving, ONE network-logon check through LogonUserW. A plain local or domain account
whose password Windows rejects is not saved. Where that check cannot be trusted either way (a
Microsoft-account-linked or Entra ID account, logon type denied by policy, an expired password,
...) the password is saved and the user is WARNED instead of blocked. The check runs once per Save
click, never in a loop, so it costs the account at most the one bad attempt a typo at the lock
screen would.

How the account is stored: ``username`` is %USERNAME% (the SAM name) and ``domain`` is %USERDOMAIN%
-- the computer name for a local account, including one linked to a Microsoft account, "AzureAD"
for Entra ID. That is exactly what the Credential Provider packs into the logon.
"""
from __future__ import annotations

import ctypes
import logging
import os
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

from face_service.config import Config
from face_service.credentials import clear_password, load_password, save_password
from face_service.i18n import set_language, t

log = logging.getLogger(__name__)

_MASK = "•"

# LogonUserW network logon: validates the password without a profile load or an interactive
# session. Win32 error codes that mean "Windows looked at this password and said no".
_LOGON32_LOGON_NETWORK = 3
_LOGON32_PROVIDER_DEFAULT = 0
_ERROR_LOGON_FAILURE = 1326


class _UserInfo24(ctypes.Structure):
    _fields_ = [("internet_identity", wintypes.BOOL), ("flags", wintypes.DWORD),
                ("provider_name", wintypes.LPWSTR), ("principal_name", wintypes.LPWSTR),
                ("user_sid", ctypes.c_void_p)]


def _is_internet_linked(user: str) -> "bool | None":
    """True when the LOCAL account ``user`` is linked to a Microsoft account (USER_INFO_24
    internet_identity), False when it is a plain local account, None when Windows would not say.
    Read-only account metadata, no logon attempt."""
    try:
        netapi = ctypes.WinDLL("netapi32")
        buf = ctypes.c_void_p()
        rc = netapi.NetUserGetInfo(None, ctypes.c_wchar_p(user), 24, ctypes.byref(buf))
        if rc != 0 or not buf:
            return None
        try:
            return bool(ctypes.cast(buf, ctypes.POINTER(_UserInfo24)).contents.internet_identity)
        finally:
            netapi.NetApiBufferFree(buf)
    except Exception:
        return None


def _logon_user(user: str, domain: str, password: str) -> int:
    """One LogonUserW network logon. Returns 0 on success, else the Win32 error code."""
    import pywintypes      # type: ignore
    import win32security   # type: ignore
    try:
        h = win32security.LogonUser(user, domain or None, password,
                                    _LOGON32_LOGON_NETWORK, _LOGON32_PROVIDER_DEFAULT)
    except pywintypes.error as e:
        return int(e.winerror or -1)
    h.Close()
    return 0


def check_windows_password(user: str, domain: str, password: str, *, logon=_logon_user,
                           internet_linked=_is_internet_linked) -> "tuple[str, int]":
    """``("ok", 0)`` | ``("rejected", err)`` | ``("unverifiable", err)``. Calls ``logon`` ONCE.

    "rejected" only when the answer is conclusive: ERROR_LOGON_FAILURE for an account whose
    password this machine validates itself (a plain local account, or a domain account whose
    domain controller answered). A Microsoft-account-linked local account (or one whose link state
    is unknown) and an Entra ID account ("AzureAD") are validated against a cloud password the
    local check may not have yet, so a refusal there is "unverifiable", as is every other error."""
    err = logon(user, domain, password)
    if err == 0:
        return "ok", 0
    if err == _ERROR_LOGON_FAILURE:
        local = domain in (".", "") or domain.upper() == os.environ.get("COMPUTERNAME", "").upper()
        if domain.upper() == "AZUREAD":
            return "unverifiable", err
        if not local:
            return "rejected", err
        if internet_linked(user) is False:
            return "rejected", err
    return "unverifiable", err


def store_password_checked(user: str, password: str, domain: str, *, check=check_windows_password,
                           save=None, load=None) -> "tuple[bool, str]":
    """The Save button without Tk: check once, then save and read back. Returns ``(ok, status)``
    where status is the localized line to show. Nothing is written when the check is conclusive
    and negative."""
    save = save or save_password
    load = load or load_password
    verdict, err = check(user, domain, password)
    if verdict == "rejected":
        return False, t("pwd.err.rejected").format(user=user)
    save(user, password, domain)
    # Same read-back the console tool does: proves DPAPI round-tripped under this user,
    # rather than reporting success on a write nobody has verified.
    rec = load()
    if not rec or rec.get("u") != user:
        raise RuntimeError("stored credential did not read back")
    if verdict == "unverifiable":
        return True, t("pwd.status.saved_unverified").format(err=err)
    return True, t("pwd.status.saved")


class PasswordWindow:
    """One modal-ish window. Never raises out of a button handler: every failure becomes a status
    line, because a traceback into a detached pythonw/frozen process goes nowhere a user can see."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(t("pwd.title"))
        self.root.resizable(False, False)

        frm = ttk.Frame(self.root, padding=14)
        frm.grid(row=0, column=0, sticky="nsew")

        ttk.Label(frm, text=t("pwd.intro"), wraplength=420, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.user = tk.StringVar(value=os.environ.get("USERNAME", ""))
        self.domain = tk.StringVar(value=os.environ.get("USERDOMAIN", "."))
        self.pw1 = tk.StringVar()
        self.pw2 = tk.StringVar()

        rows = (
            (t("pwd.user"), self.user, None),
            (t("pwd.domain"), self.domain, None),
            (t("pwd.password"), self.pw1, _MASK),
            (t("pwd.confirm"), self.pw2, _MASK),
        )
        for i, (label, var, show) in enumerate(rows, start=1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            kwargs = {"show": show} if show else {}
            ttk.Entry(frm, textvariable=var, width=34, **kwargs).grid(
                row=i, column=1, sticky="ew", pady=3)

        self.status = ttk.Label(frm, text="", wraplength=420, justify="left")
        self.status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text=t("pwd.btn.save"), command=self._save).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text=t("pwd.btn.clear"), command=self._clear).grid(row=0, column=1, padx=4)
        ttk.Button(btns, text=t("pwd.btn.close"), command=self.root.destroy).grid(
            row=0, column=2, padx=4)

        frm.columnconfigure(1, weight=1)
        self._show_existing()

    # -- helpers ------------------------------------------------------------------------------
    def _set_status(self, text: str, ok: bool = True) -> None:
        self.status.configure(text=text, foreground=("#1a7f37" if ok else "#b3261e"))

    def _show_existing(self) -> None:
        """State on open: whether anything is stored at all. load_password never raises."""
        try:
            rec = load_password()
        except Exception:
            log.exception("reading the stored credential failed")
            rec = None
        if rec and rec.get("u"):
            self._set_status(t("pwd.status.existing").format(user=rec["u"]))
        else:
            self._set_status(t("pwd.status.none"))

    # -- actions ------------------------------------------------------------------------------
    def _save(self) -> None:
        pw, pw2 = self.pw1.get(), self.pw2.get()
        if not pw:
            self._set_status(t("pwd.err.empty"), ok=False)
            return
        if pw != pw2:
            self._set_status(t("pwd.err.mismatch"), ok=False)
            return
        user = self.user.get().strip()
        domain = self.domain.get().strip() or "."
        try:
            ok, status = store_password_checked(user, pw, domain)
        except Exception as e:
            log.exception("saving the password failed")
            self._set_status(t("pwd.err.save").format(err=e), ok=False)
            return
        if not ok:
            # Keep what was typed: the user corrects it rather than retyping both fields.
            self._set_status(status, ok=False)
            return
        self.pw1.set("")
        self.pw2.set("")
        self._set_status(status)

    def _clear(self) -> None:
        try:
            clear_password()
        except Exception as e:
            log.exception("clearing the stored credential failed")
            self._set_status(t("pwd.err.save").format(err=e), ok=False)
            return
        self.pw1.set("")
        self.pw2.set("")
        self._set_status(t("pwd.status.cleared"))

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    setup_logging(LOG_PATH.with_name("enroll.log"))
    try:
        # i18n state is per-process: this window is its own process (spawned by the tray, or the
        # frozen exe re-execing itself), so the language has to be applied here too.
        set_language(Config.load().language)
        PasswordWindow().run()
    except Exception:
        log.exception("password dialog crashed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
