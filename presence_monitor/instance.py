"""Single-instance guards for the tray's processes (tray, wizard, password dialog).

Named ``Local\\`` mutexes, one per role, held for the process lifetime (the OS releases them at
exit). A second instance does not start: it brings the running one's window to the front instead
(Stage 9, act 9b R12 / F-173, F-189), because "nothing happens" on a second click reads as broken.
"""
from __future__ import annotations

import ctypes

_held: list = []


def first_instance(name: str) -> bool:
    """True if this process now owns the named mutex; False if another instance holds it. A mutex
    that cannot be created at all fails OPEN (True): the guard must never stop the tool itself."""
    try:
        import win32api    # type: ignore
        import win32event  # type: ignore
        import winerror    # type: ignore
        handle = win32event.CreateMutex(None, False, name)
        if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
            return False
        _held.append(handle)
    except Exception:
        return True
    return True


def raise_by_title(title: str) -> bool:
    """Restore and bring to the front the top-level window titled ``title``. Never raises."""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, ctypes.c_wchar_p(title))
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)              # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False
