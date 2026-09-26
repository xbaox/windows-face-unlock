"""The Windows-password dialog (Stage 9, act 9b R13).

The Credential Provider signs the user in with this password after a face match; it is stored
DPAPI-sealed for this Windows account only (face_service.credentials).

What Stage 9 changed:

* **Identity from the token, read-only.** The account is the one this process runs as -- the
  owner -- and its name is read from the token (``GetUserNameExW``), not from %USERNAME% /
  %USERDOMAIN% in two editable fields (F-159, F-84):
    - a local or Microsoft-account-linked local account: ``COMPUTER\\SAM`` -- the form the lock
      screen accepts, confirmed live on PC-1 for an MSA-linked account;
    - an Entra ID account (SID S-1-12-1-...): the UPN (``user@tenant``) with an empty domain; if
      Windows reports no UPN, ``AzureAD\\user``. Final confirmation of the Entra form is a VM run
      (9e).
* **The check is the logon the lock screen performs**: ``LogonUserW`` with LOGON32_LOGON_INTERACTIVE
  through the default (Negotiate) provider, once per Save -- never in a loop, because every failed
  logon counts toward the account-lockout policy. An unambiguous refusal (wrong password 1326,
  account restriction 1327, disabled 1331, expired account 1793, locked out 1909) saves NOTHING.
  An ambiguous answer (no logon server, expired password, logon type not granted, ...) saves the
  password with a yellow warning and the next step (F-160).
* It runs off the Tk thread; the result comes back through a queue drained by ``after`` (F-163).
* **Passwordless device mode** (``DevicePasswordLessBuildVersion = 2``) is explained, with a
  button to the sign-in options: face unlock signs in with a password, so the account needs one.
* Clearing the stored password asks first (F-161); Enter saves, Escape closes, the password field
  has the focus (R12); one dialog at a time (F-173, F-109).
* The state on open is three-valued (F-108): nothing stored / stored but unreadable on this account
  (save again) / stored for <user>; plus the lock screen's verdict when Windows rejected the stored
  password there (protocol v2 ``report_result``).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from face_service.config import Config
from face_service.credentials import (clear_password, load_password, password_rejected,
                                      password_state, save_password)
from face_service.i18n import set_language, t

log = logging.getLogger("presence_monitor.password_gui")

_MASK = "•"
PASSWORD_MUTEX = "Local\\FaceUnlockPassword"

_LOGON32_LOGON_INTERACTIVE = 2
_LOGON32_PROVIDER_DEFAULT = 0
# Unambiguous refusals: nothing is saved.
_REJECT = {
    1326: "pwd.err.rejected",           # ERROR_LOGON_FAILURE -- wrong user name or password
    1327: "pwd.err.restricted",         # ERROR_ACCOUNT_RESTRICTION -- e.g. a blank password
    1331: "pwd.err.disabled",           # ERROR_ACCOUNT_DISABLED
    1793: "pwd.err.account_expired",    # ERROR_ACCOUNT_EXPIRED
    1909: "pwd.err.locked_out",         # ERROR_ACCOUNT_LOCKED_OUT
}

_NAME_SAM_COMPATIBLE = 2
_NAME_USER_PRINCIPAL = 8

GREEN, AMBER, RED = "#1a7f37", "#8a5a00", "#b3261e"


def _user_name_ex(fmt: int) -> str:
    size = ctypes.c_ulong(0)
    secur32 = ctypes.WinDLL("secur32")
    secur32.GetUserNameExW(fmt, None, ctypes.byref(size))
    if not size.value:
        return ""
    buf = ctypes.create_unicode_buffer(size.value + 1)
    if not secur32.GetUserNameExW(fmt, buf, ctypes.byref(size)):
        return ""
    return buf.value


def account_identity(*, sid: "str | None" = None, name_ex=_user_name_ex) -> "tuple[str, str, str]":
    """``(user, domain, kind)`` of the account this process runs as. ``kind`` is "local" (a local
    or Microsoft-account-linked local account, and AD), "entra" (UPN form) or "entra-sam" (Entra
    with no UPN reported -- the SAM form, to be confirmed on a VM)."""
    if sid is None:
        from face_service.identity import current_user_sid
        sid = current_user_sid()
    if sid.startswith("S-1-12-1-"):
        upn = name_ex(_NAME_USER_PRINCIPAL)
        if upn and "@" in upn:
            return upn, "", "entra"
    sam = name_ex(_NAME_SAM_COMPATIBLE)
    if "\\" in sam:
        domain, user = sam.split("\\", 1)
    else:
        import os
        domain, user = os.environ.get("USERDOMAIN", "."), sam or os.environ.get("USERNAME", "")
    return user, domain, ("entra-sam" if sid.startswith("S-1-12-1-") else "local")


def passwordless_mode() -> bool:
    """True when Windows' passwordless device mode is on (DevicePasswordLessBuildVersion = 2)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            v, _ = winreg.QueryValueEx(k, "DevicePasswordLessBuildVersion")
            return int(v) == 2
    except Exception:
        return False


def _logon_user(user: str, domain: str, password: str) -> int:
    """One interactive logon through Negotiate (the lock screen's form). 0 or the Win32 error."""
    import pywintypes      # type: ignore
    import win32security   # type: ignore
    try:
        h = win32security.LogonUser(user, domain or None, password,
                                    _LOGON32_LOGON_INTERACTIVE, _LOGON32_PROVIDER_DEFAULT)
    except pywintypes.error as e:
        return int(e.winerror or -1)
    h.Close()
    return 0


def check_windows_password(user: str, domain: str, password: str, *,
                           logon=_logon_user) -> "tuple[str, int]":
    """``("ok", 0)`` | ``("rejected", err)`` | ``("unverifiable", err)``. Calls ``logon`` ONCE."""
    err = logon(user, domain, password)
    if err == 0:
        return "ok", 0
    if err in _REJECT:
        return "rejected", err
    return "unverifiable", err


def store_password_checked(user: str, password: str, domain: str, *, check=check_windows_password,
                           save=None, load=None) -> "tuple[str, str]":
    """The Save button without Tk: check once, then save and read back. Returns ``(level, text)``,
    level "ok" (green), "warn" (saved but unconfirmed, amber) or "err" (nothing saved, red)."""
    save = save or save_password
    load = load or load_password
    verdict, err = check(user, domain, password)
    if verdict == "rejected":
        return "err", t(_REJECT.get(err, "pwd.err.rejected"), user=user)
    save(user, password, domain)
    rec = load()
    if not rec or rec.get("u") != user:
        raise RuntimeError("stored credential did not read back")
    if verdict == "unverifiable":
        return "warn", t("pwd.status.saved_unverified", err=err)
    return "ok", t("pwd.status.saved")


def _custody_worker(q: "queue.Queue") -> None:
    """Stage 9 (R9, F-99/F-101): a data folder the service could not secure takes no password.
    Asks the service once; an unreachable service does not block (it judges custody at start)."""
    try:
        from .monitor import pipe_call
        st = pipe_call({"cmd": "status"}, timeout_s=3.0) or {}
    except Exception:
        st = {}
    q.put(("custody", bool(st.get("ok")) and st.get("data_dir_secure") is False))


def _check_worker(q: "queue.Queue", user: str, domain: str, password: str) -> None:
    """Worker thread: holds the queue and plain strings only -- never a Tk object."""
    try:
        q.put(("saved", store_password_checked(user, password, domain)))
    except Exception as e:
        log.exception("saving the password failed")
        q.put(("saved", ("err", t("pwd.err.save", err=e))))


class PasswordWindow:
    """The dialog. Never raises out of a handler: a detached windowed process has nowhere to show a
    traceback, so every failure becomes a status line."""

    def __init__(self) -> None:
        from .ui import apply_scaling, bind_standard_keys, px
        self.root = tk.Tk()
        self.root.title(t("pwd.title"))
        self.root.resizable(False, False)
        apply_scaling(self.root)
        self._q: "queue.Queue" = queue.Queue()
        self._busy = False
        self._custody_failed = False

        frm = ttk.Frame(self.root, padding=14)
        frm.grid(row=0, column=0, sticky="nsew")
        wrap = px(self.root, 440)

        ttk.Label(frm, text=t("pwd.intro"), wraplength=wrap, justify="left").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.user, self.domain, kind = account_identity()
        shown = self.user if not self.domain else f"{self.domain}\\{self.user}"
        ttk.Label(frm, text=t("pwd.account")).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Label(frm, text=shown).grid(row=1, column=1, sticky="w", pady=3)
        row = 2
        if kind == "entra-sam":
            ttk.Label(frm, text=t("pwd.note.entra"), wraplength=wrap, justify="left",
                      foreground=AMBER).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)
            row += 1
        if passwordless_mode():
            ttk.Label(frm, text=t("pwd.note.passwordless"), wraplength=wrap, justify="left",
                      foreground=AMBER).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)
            ttk.Button(frm, text=t("pwd.btn.signin_options"), command=self._open_signin_options
                       ).grid(row=row + 1, column=0, columnspan=2, sticky="w", pady=(0, 6))
            row += 2

        self.pw1 = tk.StringVar(master=self.root)
        self.pw2 = tk.StringVar(master=self.root)
        ttk.Label(frm, text=t("pwd.password")).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
        self.pw1_entry = ttk.Entry(frm, textvariable=self.pw1, width=34, show=_MASK)
        self.pw1_entry.grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Label(frm, text=t("pwd.confirm")).grid(row=row + 1, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Entry(frm, textvariable=self.pw2, width=34, show=_MASK).grid(
            row=row + 1, column=1, sticky="ew", pady=3)

        self.status = ttk.Label(frm, text="", wraplength=wrap, justify="left")
        self.status.grid(row=row + 2, column=0, columnspan=2, sticky="w", pady=(12, 8))

        btns = ttk.Frame(frm)
        btns.grid(row=row + 3, column=0, columnspan=2, sticky="e")
        self.save_btn = ttk.Button(btns, text=t("pwd.btn.save"), command=self._save, default="active")
        self.save_btn.grid(row=0, column=0, padx=4)
        self.clear_btn = ttk.Button(btns, text=t("pwd.btn.clear"), command=self._clear)
        self.clear_btn.grid(row=0, column=1, padx=4)
        ttk.Button(btns, text=t("pwd.btn.close"), command=self._close).grid(row=0, column=2, padx=4)

        frm.columnconfigure(1, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        bind_standard_keys(self.root, ok=self._save, cancel=self._close)
        self._show_existing()
        self.pw1_entry.focus_set()
        threading.Thread(target=_custody_worker, args=(self._q,), daemon=True).start()
        self.root.after(50, self._drain)

    # -- helpers ------------------------------------------------------------------------------
    def _set_status(self, text: str, level: str = "ok") -> None:
        self.status.configure(text=text, foreground={"ok": GREEN, "warn": AMBER}.get(level, RED))

    def _show_existing(self) -> None:
        try:
            state, info = password_state()
        except Exception:
            log.exception("reading the stored credential failed")
            state, info = "unreadable", "?"
        if password_rejected() and state == "ok":
            self._set_status(t("pwd.status.rejected_at_lock"), "err")
        elif state == "ok":
            self._set_status(t("pwd.status.existing", user=info))
        elif state == "unreadable":
            log.warning("stored credential unreadable: %s", info)
            self._set_status(t("pwd.status.unreadable"), "warn")
        else:
            self._set_status(t("pwd.status.none"), "warn")

    def _open_signin_options(self) -> None:
        import os
        try:
            os.startfile("ms-settings:signinoptions")  # type: ignore[attr-defined]
        except Exception:
            log.exception("could not open the sign-in options")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "saved":
                    self._on_saved(*payload)
                elif kind == "custody" and payload:
                    self._custody_failed = True
                    self.save_btn.configure(state="disabled")
                    self._set_status(t("pwd.err.custody"), "err")
        except queue.Empty:
            pass
        try:
            self.root.after(50, self._drain)
        except tk.TclError:
            pass

    # -- actions ------------------------------------------------------------------------------
    def _save(self) -> None:
        if self._busy or self._custody_failed:
            return
        pw, pw2 = self.pw1.get(), self.pw2.get()
        if not pw:
            self._set_status(t("pwd.err.empty"), "err")
            return
        if pw != pw2:
            self._set_status(t("pwd.err.mismatch"), "err")
            return
        self._busy = True
        self.save_btn.configure(state="disabled")
        self.clear_btn.configure(state="disabled")
        self._set_status(t("pwd.status.checking"), "warn")
        threading.Thread(target=_check_worker, args=(self._q, self.user, self.domain, pw),
                         name="pwd-check", daemon=True).start()

    def _on_saved(self, level: str, text: str) -> None:
        self._busy = False
        self.save_btn.configure(state="disabled" if self._custody_failed else "normal")
        self.clear_btn.configure(state="normal")
        if level != "err":
            self.pw1.set("")
            self.pw2.set("")
        self._set_status(text, level)

    def _clear(self) -> None:
        if self._busy:
            return
        if not messagebox.askyesno(t("pwd.confirm_clear.title"), t("pwd.confirm_clear.body"),
                                   parent=self.root, icon="warning", default="no"):
            return
        try:
            clear_password()
        except Exception as e:
            log.exception("clearing the stored credential failed")
            self._set_status(t("pwd.err.save", err=e), "err")
            return
        self.pw1.set("")
        self.pw2.set("")
        self._set_status(t("pwd.status.cleared"), "warn")

    def _close(self) -> None:
        if self._busy:
            return                     # the check is one logon; let it finish and report
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    from face_service.config import LOG_PATH
    from face_service.logging_setup import setup_logging
    from .ui import enable_dpi_awareness
    setup_logging(LOG_PATH.with_name("enroll.log"))
    try:
        # i18n state is per-process: this window is its own process (spawned by the tray, or the
        # frozen exe re-execing itself), so the language has to be applied here too.
        set_language(Config.load().language)
        from presence_monitor.instance import first_instance, raise_by_title
        if not first_instance(PASSWORD_MUTEX):
            raise_by_title(t("pwd.title"))
            return 0
        enable_dpi_awareness()
        PasswordWindow().run()
    except Exception:
        log.exception("password dialog crashed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
