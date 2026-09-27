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

9d-build: pywinrt 3.2.1 (the newest) ships its OWN msvcp140.dll, version 14.29 (VS 2019). Code built
with MSVC 14.40+ (onnx, and any newer C++ extension) crashes when that older runtime is the one the
process loaded first (the std::mutex ABI change of VS 2022 17.10) -- the build's isolated import
of every package died exactly so. Therefore: the process's own, newer msvcp140.dll is loaded
BEFORE winrt (the bundle carries it in _internal; a checkout finds System32's) and must be 14.40 or
newer, else toasts stay off; the bundle does not ship winrt's copy; and winrt is imported by name
at run time (importlib) so PyInstaller never imports it during the build.

A toast is best-effort by nature (Focus assist, notifications off for the app, a developer checkout
without the Start-menu shortcut). The fallback channel is the "Recent events" list in the Status
window, which gets every event whether or not a toast was shown. Without WinRT (the bindings
missing or failing to load) one WARNING is logged and every later toast is skipped quietly.
"""
from __future__ import annotations

import ctypes
import importlib
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


MSVCP_MIN = (14, 40)


def _file_version(path: str) -> "tuple[int, ...]":
    import win32api  # type: ignore
    info = win32api.GetFileVersionInfo(path, "\\")
    ms, ls = info["FileVersionMS"] & 0xFFFFFFFF, info["FileVersionLS"] & 0xFFFFFFFF
    return (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)


def msvcp_runtime() -> "tuple[str, tuple[int, ...]]":
    """Load the process's msvcp140.dll by name -- in the bundle _internal's copy, in a checkout
    System32's -- BEFORE winrt can load its older one; return (path, version)."""
    h = ctypes.WinDLL("msvcp140.dll")
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetModuleFileNameW(ctypes.c_void_p(h._handle), buf, 1024)
    return buf.value, _file_version(buf.value)


class _WinRt:
    """The three WinRT classes a toast needs. Imported by NAME (see the module docstring)."""

    def __init__(self):
        path, ver = msvcp_runtime()
        if ver[:2] < MSVCP_MIN:
            raise RuntimeError(f"the C++ runtime {path} is {'.'.join(map(str, ver))}; winrt needs a "
                               f"process runtime >= {MSVCP_MIN[0]}.{MSVCP_MIN[1]} loaded first")
        self.msvcp = (path, ver)
        dom = importlib.import_module("winrt.windows.data.xml.dom")
        notif = importlib.import_module("winrt.windows.ui.notifications")
        self.XmlDocument = dom.XmlDocument
        self.ToastNotification = notif.ToastNotification
        self.ToastNotificationManager = notif.ToastNotificationManager


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


def selfcheck_main(argv) -> int:
    """face_unlock_tray.exe --selfcheck-toast --out <file.json> (build gate only, FU_BUILD_GATE=1):
    load the bindings as the tray would, build a document, a toast and a notifier -- never shown.
    Exit 0 when all of it worked on a runtime >= 14.40."""
    import json
    try:
        out = argv[argv.index("--out") + 1]
    except (ValueError, IndexError):
        return 2
    res: dict = {"frozen": bool(getattr(__import__("sys"), "frozen", False))}
    rc = 1
    try:
        api = _WinRt()
        res["msvcp140"] = {"path": api.msvcp[0], "version": ".".join(map(str, api.msvcp[1]))}
        doc = api.XmlDocument()
        doc.load_xml(toast_xml("Face Unlock", "build gate"))
        api.ToastNotification(doc)
        api.ToastNotificationManager.create_toast_notifier_with_id(APP_ID)
        mod = __import__("sys").modules.get("winrt")     # no static import: see the docstring
        res["winrt_file"] = getattr(mod, "__file__", None)
        res["ok"] = True
        rc = 0
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{e.__class__.__name__}: {e}"
    res["rc"] = rc
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    return rc
