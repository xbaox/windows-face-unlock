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
"""
from __future__ import annotations

import logging
import os
import tkinter as tk
from tkinter import ttk

from face_service.config import Config
from face_service.credentials import clear_password, load_password, save_password
from face_service.i18n import set_language, t

log = logging.getLogger(__name__)

_MASK = "•"


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
            save_password(user, pw, domain)
            # Same read-back the console tool does: proves DPAPI round-tripped under this user,
            # rather than reporting success on a write nobody has verified.
            check = load_password()
            if not check or check.get("u") != user:
                raise RuntimeError("stored credential did not read back")
        except Exception as e:
            log.exception("saving the password failed")
            self._set_status(t("pwd.err.save").format(err=e), ok=False)
            return
        self.pw1.set("")
        self.pw2.set("")
        self._set_status(t("pwd.status.saved"))

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
