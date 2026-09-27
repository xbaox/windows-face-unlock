"""The tray icon and its menu (Stage 9, act 9b R12 / R14 / R15).

* **State at a glance** (F-172, R9, R13): a transparent icon drawn per state -- green (ready),
  amber (needs attention: paused, no face enrolled, the stored password was rejected at the lock
  screen), grey (off: the service is not running or refuses, the camera cannot see) -- and the same
  state as the first, disabled line of the menu and in the tooltip. It follows the monitor's ticks,
  not only menu clicks. While the service refuses face functions, "Set up face" and "Check
  presence now" are disabled.
* **Notifications** (R14): WinRT toasts under the product's AppUserModelID (presence_monitor.toast);
  pystray balloons are not used. Every event also goes to the Status window's "Recent events".
* **Quit asks first** (F-157): it switches face sign-in and walk-away lock off until the next
  sign-in to Windows or until Face Unlock is started again from Start (9d, A-9: a pause bound to
  the logon session, no TTL; the watchdog honours it, a tray start clears it).
* **Windows** run on the one Tk thread (presence_monitor.ui); a second click raises the open window
  (F-173). The password dialog and the wizard are their own processes, each with its own mutex;
  the dev layout opens the same password dialog as the installed one (F-162).
* **Updates** (R15): the background check runs at most once a day, only with update_check on, and
  stays silent unless a newer release exists (then: a toast, an event, and an "Open releases page"
  menu item). The menu item always checks and answers in a dialog (F-197), with a separate text for
  each failure (F-183). One check at a time.
* **Languages** (R16): English and Russian.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

from face_service.config import Config
from face_service.i18n import Msg, get_language, set_language, shown_languages, t

from .gui import open_help, open_log_folder, open_settings, open_status, probe_text
from .monitor import PresenceMonitor, pipe_call
from .toast import set_process_app_id, show_toast
from .ui import UiThread, enable_dpi_awareness
from .updater import (RELEASES_PAGE_URL, check_latest_status, current_version, due_for_auto_check,
                      record_auto_check)

log = logging.getLogger(__name__)

ENROLL_CMD = ["-m", "presence_monitor.enroll_gui"]
PASSWORD_CMD = ["-m", "presence_monitor.password_gui"]

# A frozen bundle has no ``-m`` entry point: presence_monitor/__main__.py routes these flags, and
# the launchers below re-exec sys.executable (face_unlock_tray.exe when frozen) with them.
FROZEN = bool(getattr(sys, "frozen", False))
ENROLL_FLAG = "--enroll"
SET_PASSWORD_FLAG = "--set-password"

AUTO_UPDATE_DELAY_S = 120.0

# The live child processes, kept so Quit can take them down (a list: rebound without `global`).
_children: dict[str, subprocess.Popen] = {}


# ---- icon ------------------------------------------------------------------------------------

_COLOURS = {"ok": (46, 160, 67, 255), "attention": (214, 150, 20, 255), "off": (140, 140, 140, 255)}


def icon_image(level: str) -> Image.Image:
    """A transparent 64x64 face in the state colour (F-172: no white square on a dark taskbar).
    9d (V-48): drawn by presence_monitor.ui, which also gives it to every window."""
    from .ui import face_icon_image
    return face_icon_image(level if level in _COLOURS else "off")


def tray_state(status: "dict | None", snap: dict, reachable_known: bool = True) -> "tuple[str, str]":
    """(level, state line) from the last status reply and the monitor snapshot. Pure."""
    if status is None:
        return "off", t("tray.state.no_service") if reachable_known else t("tray.state.starting")
    if status.get("state") == "refusing":
        why = str(status.get("why") or "")
        return "off", t("tray.state.refusing", why=t(f"why.{why}") if why else "?")
    if status.get("password_rejected"):
        return "attention", t("tray.state.password_rejected")
    if not status.get("enrollment"):
        return "attention", t("tray.state.no_enrollment")
    if snap.get("paused"):
        return "attention", t("tray.state.paused")
    if snap.get("last_result") == "unknown":
        from .gui import camera_why_text
        return "off", t("tray.state.camera_unknown", why=camera_why_text(snap.get("last_why")))
    return "ok", (t("tray.state.ready_autolock") if snap.get("auto_lock") else t("tray.state.ready"))


def update_result_text(release, status: str, current: str) -> "tuple[str, str]":
    """What a MANUAL update check says, in a dialog (F-197: never only a toast), with one text per
    outcome (F-183). ("ask", text) when a newer release exists, else ("info", text). Pure."""
    if release is not None and release.is_newer_than(current):
        return "ask", t("update.available", latest=release.tag, current=current)
    if release is not None:
        return "info", t("update.up_to_date", v=current)
    if status.startswith("no-asset:"):
        return "info", t("update.no_asset", tag=status.split(":", 1)[1])
    key = {"no-release": "update.no_releases", "rate-limited": "update.rate_limited",
           "proxy-auth": "update.proxy_auth", "network": "update.network",
           "bad-response": "update.bad_response"}.get(status, "update.http_error")
    return "info", t(key, err=status)


# ---- child processes --------------------------------------------------------------------------

def _spawn(role: str, dev_args: list, flag: str) -> None:
    """Start the wizard / password dialog as its own windowed process and KEEP the handle, so Quit
    can take it down. The child holds its own mutex and raises its window on a duplicate."""
    proc = _children.get(role)
    running = proc is not None and proc.poll() is None
    if running:
        # Still started: the new process finds the mutex taken, brings the open window to the
        # front and exits. The handle of the running one is kept.
        log.info("%s already running (pid=%s) -- raising it", role, proc.pid)
    if FROZEN:
        argv = [sys.executable, flag]
        cwd = None
    else:
        repo = Path(__file__).resolve().parent.parent
        pyw = repo / ".venv" / "Scripts" / "pythonw.exe"
        argv = [str(pyw) if pyw.exists() else sys.executable, *dev_args]
        cwd = str(repo)
    try:
        new = subprocess.Popen(argv, cwd=cwd,
                               creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        if not running:
            _children[role] = new
            log.info("%s started (pid=%s)", role, new.pid)
    except Exception:
        log.exception("failed to start the %s", role)


def _terminate_children() -> None:
    for role, proc in list(_children.items()):
        if proc.poll() is not None:
            continue
        try:
            log.info("terminating the %s (pid=%s)", role, proc.pid)
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            log.exception("terminating the %s failed", role)


def _save_language(code: str) -> None:
    """Persist the language choice -- ONLY that key (Stage 9, R9)."""
    try:
        cfg = Config.load()
        cfg.language = code
        cfg.validate()
        cfg.save(keys=["language"])
    except Exception:
        log.exception("failed to persist language=%s", code)


# ---- the tray ---------------------------------------------------------------------------------

def resume_after_quit(*, _start=None) -> bool:
    """9d (A-9): a tray start ends a Quit of this sign-in -- the marker goes, and the service is
    started again (it was stopped by Quit and the watchdog left it alone). Returns whether a Quit
    pause was active. Never raises."""
    try:
        from face_service.config import QUIT_PAUSE_PATH
        from face_service.watchdog import clear_pause, logon_id, quit_active
        was = quit_active(QUIT_PAUSE_PATH, logon_id())
        clear_pause(QUIT_PAUSE_PATH)
    except Exception:
        log.exception("could not check the Quit pause")
        return False
    if was:
        log.info("started after a Quit in this sign-in: the pause is cleared, starting the service")
        try:
            if _start is not None:
                _start()
            else:
                from face_service.taskreg import Scheduler
                Scheduler().run("FaceUnlock-Service")
        except Exception:
            log.warning("could not start the service task; the watchdog starts it", exc_info=True)
    return was


def run_with_tray(cfg: Config) -> None:
    set_language(cfg.language)
    threading.Thread(target=resume_after_quit, name="resume-after-quit", daemon=True).start()
    set_process_app_id()
    log.info("DPI awareness: %s", enable_dpi_awareness())
    ui = UiThread()

    monitor = PresenceMonitor(cfg)
    icon_ref: list[pystray.Icon] = []
    newer: dict = {}                       # {"tag": "v0.2.1"} once a newer release is known
    update_busy = threading.Lock()

    def refresh_icon() -> None:
        if not icon_ref:
            return
        icon = icon_ref[0]
        level, line = tray_state(monitor.last_status(), monitor.snapshot(),
                                 reachable_known=monitor._svc_reachable is not None)
        icon.icon = icon_image(level)
        icon.title = f"{t('tray.title')} -- {line}"[:127]     # NOTIFYICONDATA szTip limit
        icon.update_menu()

    def on_event(gate: str, message: str) -> None:
        """Monitor events: a toast when its gate is on (the monitor's config, not a file read per
        toast -- F-154), and a state refresh either way."""
        if bool(getattr(monitor.cfg, gate, True)):
            show_toast(t("tray.title"), str(message))
        refresh_icon()

    monitor.on_event = on_event
    monitor.on_update = refresh_icon
    threading.Thread(target=monitor.run, name="presence-loop", daemon=True).start()

    def refusing() -> bool:
        st = monitor.last_status()
        return bool(st and st.get("state") == "refusing")

    # ---- actions ----
    def on_toggle(icon, item):
        if monitor.is_paused():
            monitor.resume()
        else:
            monitor.pause()
        refresh_icon()

    def on_status(icon, item):
        open_status(ui, monitor)

    def on_settings(icon, item):
        open_settings(ui, monitor, on_saved=lambda _c: refresh_icon())

    def on_help(icon, item):
        open_help(ui)

    def on_enroll(icon, item):
        _spawn("wizard", ENROLL_CMD, ENROLL_FLAG)

    def on_set_password(icon, item):
        _spawn("password dialog", PASSWORD_CMD, SET_PASSWORD_FLAG)

    def on_open_log(icon, item):
        open_log_folder()

    def on_probe_now(icon, item):
        key = ui.new_key("tray-probe")

        def show(resp):
            ui.off(key)
            from tkinter import messagebox
            text = probe_text(resp)
            # 9d (V-48): rendered in the language of the moment it is shown
            monitor.record_event(Msg("event.probe", result=lambda: probe_text(resp)))
            ui.dialog(lambda p: messagebox.showinfo(t("tray.probe").rstrip("…"), text, parent=p))
        ui.post(ui.on, key, show)
        ui.run_bg(pipe_call, {"cmd": "presence"}, 20.0, reply=key)

    # 9d (V-47): dialogs of the hidden root get a throw-away topmost owner through ui.dialog(),
    # destroyed when the dialog returns -- not after a fixed 60 s under a dialog still open.

    # ---- updates ----
    def check_updates(interactive: bool) -> None:
        if not update_busy.acquire(blocking=False):
            return                                   # F-173: one check at a time
        try:
            release, status = check_latest_status(timeout=8.0)
        finally:
            update_busy.release()
        current = current_version()
        if release is not None and release.is_newer_than(current):
            newer["tag"] = release.tag
            monitor._notify("notify_update", Msg("notify.update_available", latest=release.tag))
            refresh_icon()
        if interactive:
            ui.post(_show_update_result, release, status, current)

    def _show_update_result(release, status, current) -> None:
        from tkinter import messagebox
        import webbrowser
        kind, text = update_result_text(release, status, current)
        if kind == "ask":
            if ui.dialog(lambda p: messagebox.askyesno(t("update.title"), text, parent=p)):
                webbrowser.open(RELEASES_PAGE_URL)
        else:
            ui.dialog(lambda p: messagebox.showinfo(t("update.title"), text, parent=p))

    def on_check_update(icon, item):
        threading.Thread(target=check_updates, args=(True,), name="update-check", daemon=True).start()

    def on_open_releases(icon, item):
        import webbrowser
        webbrowser.open(RELEASES_PAGE_URL)

    def _auto_update_loop():
        # R15: at most once per 24 h, never in the first minutes after logon, only when enabled.
        time.sleep(AUTO_UPDATE_DELAY_S)
        while True:
            if monitor.cfg.update_check and due_for_auto_check():
                record_auto_check()
                check_updates(False)
            time.sleep(3600)

    threading.Thread(target=_auto_update_loop, name="update-auto", daemon=True).start()

    # ---- quit ----
    def _stop_service_process() -> None:
        try:
            resp = pipe_call({"cmd": "shutdown"}, timeout_s=3.0)
            log.info("service shutdown on Quit: %s", "sent" if resp and resp.get("ok")
                     else "no answer (service not running?)")
        except Exception:
            log.exception("service shutdown on Quit failed")

    def _do_quit() -> None:
        log.info("Quit confirmed")
        # 9d (A-9): first the pause, so the watchdog never sees a gap in which to restart.
        try:
            from face_service.config import QUIT_PAUSE_PATH
            from face_service.watchdog import logon_id, write_quit
            write_quit(QUIT_PAUSE_PATH, logon_id(), time.time())
        except Exception:
            log.exception("the Quit pause could not be written; the watchdog may restart the service")
        monitor.stop()
        _terminate_children()               # the wizard's camera lease first
        _stop_service_process()
        # 9d (V-47): Tk is torn down on its own thread, and Quit waits for it (bounded) before the
        # icon's loop ends and the process exits.
        if not ui.stop(wait_s=5.0):
            log.warning("the UI thread did not finish within 5 s")
        if icon_ref:
            icon_ref[0].stop()

    def _confirm_quit() -> None:
        from tkinter import messagebox
        if ui.dialog(lambda p: messagebox.askyesno(t("quit.confirm.title"), t("quit.confirm.body"),
                                                   parent=p, icon="warning", default="no")):
            threading.Thread(target=_do_quit, name="quit", daemon=True).start()

    def on_quit(icon, item):
        log.info("Quit requested from tray")
        ui.post(_confirm_quit)

    # ---- language ----
    def make_language_handler(code: str):
        def _handler(icon, item):
            set_language(code)
            _save_language(code)
            threading.Thread(target=lambda: pipe_call({"cmd": "reload_config"}, timeout_s=3.0),
                             daemon=True).start()
            refresh_icon()
        return _handler

    # ---- menu ----
    def lbl(key: str):
        return lambda item: t(key)

    def state_line(item):
        return tray_state(monitor.last_status(), monitor.snapshot(),
                          reachable_known=monitor._svc_reachable is not None)[1]

    def pause_text(item):
        return t("tray.resume") if monitor.is_paused() else t("tray.pause")

    language_submenu = pystray.Menu(*(
        pystray.MenuItem(native, make_language_handler(code),
                         checked=(lambda c: (lambda item: get_language() == c))(code), radio=True)
        for code, native in shown_languages()))

    menu = pystray.Menu(
        pystray.MenuItem(state_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lbl("tray.status"), on_status, default=True),
        pystray.MenuItem(lbl("tray.settings"), on_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lbl("tray.probe"), on_probe_now,
                         enabled=lambda item: bool(monitor.cfg.auto_lock) and not refusing()),
        pystray.MenuItem(pause_text, on_toggle, visible=lambda item: bool(monitor.cfg.auto_lock)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lbl("tray.enroll"), on_enroll, enabled=lambda item: not refusing()),
        pystray.MenuItem(lbl("tray.set_password"), on_set_password),
        pystray.MenuItem(lbl("tray.open_log"), on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lbl("tray.language"), language_submenu),
        pystray.MenuItem(lbl("tray.check_update"), on_check_update),
        pystray.MenuItem(lambda item: t("tray.open_releases", tag=newer.get("tag", "")),
                         on_open_releases, visible=lambda item: bool(newer)),
        pystray.MenuItem(lbl("tray.help"), on_help),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lbl("tray.quit"), on_quit),
    )

    icon = pystray.Icon("face-unlock-presence", icon_image("off"), t("tray.title"), menu)
    icon_ref.append(icon)
    refresh_icon()
    try:
        icon.run()
    finally:
        icon_ref.clear()
