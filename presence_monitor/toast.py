"""Windows toast notifications with the product's AppUserModelID (Stage 9, act 9b R14).

The tray used pystray's legacy ``NIF_INFO`` balloon. On Windows 11 those did not appear at all for
this process (F-197: three update checks, no balloon, nothing in the notification centre), and they
could never be routed or allowed per app. A modern toast needs an AppUserModelID that Windows
knows: the installer creates the Start-menu shortcut with ``AppUserModelID = APP_ID`` and the tray
process sets the same ID on itself (``set_process_app_id``).

The toast is shown through the WinRT ``ToastNotificationManager`` of Windows PowerShell 5.1, which
ships with every Windows 10/11 -- no third-party library, nothing to license (R14 allows only
MIT/BSD/Apache libraries; this uses none). One short-lived hidden PowerShell per toast
(CREATE_NO_WINDOW); the text travels as base64 in the command line, never interpolated into the
script, so no message text can inject code.

A toast is best-effort by nature (Focus assist, notifications off for the app, a developer checkout
without the Start-menu shortcut). The fallback channel is the "Recent events" list in the Status
window, which gets every event whether or not a toast was shown.
"""
from __future__ import annotations

import base64
import ctypes
import logging
import subprocess
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

APP_ID = "WindowsFaceUnlock.Tray"      # must equal AppUserModelID in installer.iss [Icons]

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:FU_TOAST_XML))
$appId = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:FU_TOAST_APPID))
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


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


def show_toast(title: str, message: str, app_id: str = APP_ID, *, _run=subprocess.Popen) -> bool:
    """Show one toast. Returns whether the PowerShell helper was started; never raises."""
    import os
    env = dict(os.environ)
    env["FU_TOAST_XML"] = base64.b64encode(toast_xml(title, message).encode("utf-8")).decode("ascii")
    env["FU_TOAST_APPID"] = base64.b64encode(app_id.encode("utf-8")).decode("ascii")
    try:
        _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
              "-Command", _SCRIPT], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
             stderr=subprocess.DEVNULL,
             creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
        return True
    except Exception:
        log.warning("toast could not be shown", exc_info=True)
        return False
