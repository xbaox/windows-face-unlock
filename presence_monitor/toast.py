"""Windows toast notifications with the product's AppUserModelID (Stage 9, act 9b R14; 9d A-5).

The tray used pystray's legacy ``NIF_INFO`` balloon. On Windows 11 those did not appear at all for
this process (F-197: three update checks, no balloon, nothing in the notification centre), and they
could never be routed or allowed per app. A modern toast needs an AppUserModelID that Windows
knows: the installer creates the Start-menu shortcut with ``AppUserModelID = APP_ID`` and the tray
process sets the same ID on itself (``set_process_app_id``).

9d (A-5, V-37): the toast goes through WinRT's ``ToastNotificationManager`` NATIVELY, via pywinrt
(MIT: ``winrt-runtime``, ``winrt-Windows.UI.Notifications``, ``winrt-Windows.Data.Xml.Dom``,
``winrt-Windows.Foundation``). Stage 9 started one hidden Windows PowerShell per toast; no
executable of the product starts ``powershell.exe`` while it runs any more.

A toast is best-effort by nature (Focus assist, notifications off for the app, a developer checkout
without the Start-menu shortcut). The fallback channel is the "Recent events" list in the Status
window, which gets every event whether or not a toast was shown. Without WinRT (the bindings
missing or failing to load) one WARNING is logged and every later toast is skipped quietly.
"""
from __future__ import annotations

import ctypes
import logging
import threading
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

APP_ID = "WindowsFaceUnlock.Tray"      # must equal AppUserModelID in installer.iss [Icons]

_lock = threading.Lock()
_api = None                            # the three WinRT classes, loaded once
_unavailable_warned = False


def set_process_app_id(app_id: str = APP_ID) -> bool:
    """Give this process the product's AppUserModelID (taskbar grouping, toast attribution)."""
    try:
        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        return hr == 0
    except Exception:
        log.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)
        return False


def toast_xml(title: str, message: str) -> str:
    """The toast payload: a ToastGeneric binding with a title line and a body line."""
    return ("<toast><visual><binding template=\"ToastGeneric\">"
            f"<text>{escape(title)}</text><text>{escape(message)}</text>"
            "</binding></visual></toast>")


class _WinRt:
    """The three WinRT classes a toast needs."""

    def __init__(self):
        from winrt.windows.data.xml.dom import XmlDocument
        from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager
        self.XmlDocument = XmlDocument
        self.ToastNotification = ToastNotification
        self.ToastNotificationManager = ToastNotificationManager


def _load_api():
    """The WinRT bindings, or None when they cannot be loaded (one WARNING for the process)."""
    global _api, _unavailable_warned
    with _lock:
        if _api is not None:
            return _api
        try:
            _api = _WinRt()
            return _api
        except Exception as e:
            if not _unavailable_warned:
                _unavailable_warned = True
                log.warning("toasts are unavailable (WinRT bindings: %r); events stay in the "
                            "Status window's Recent events", e)
            return None


def show_toast(title: str, message: str, app_id: str = APP_ID, *, _api_for_test=None) -> bool:
    """Show one toast. Returns whether Windows accepted it; never raises."""
    api = _api_for_test if _api_for_test is not None else _load_api()
    if api is None:
        return False
    try:
        doc = api.XmlDocument()
        doc.load_xml(toast_xml(title, message))
        notifier = api.ToastNotificationManager.create_toast_notifier_with_id(app_id)
        notifier.show(api.ToastNotification(doc))
        return True
    except Exception as e:
        log.warning("toast could not be shown: %r", e)
        return False
