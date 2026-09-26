"""Detect a session that is being remotely CONTROLLED, so the presence monitor can skip auto-lock.

Stage 9 (act 9b R10; F-142, F-148). Only a session that someone is actually driving counts:

1. RDP: ``GetSystemMetrics(SM_REMOTESESSION)`` -- this session is itself a remote session -- or
   ``SM_REMOTECONTROL`` -- the console session is being remotely controlled (RDP shadowing, Remote
   Assistance over RDP).
2. Third-party tools: a PER-CONNECTION process running in THIS session -- one the tool starts
   only while a remote side is connected (``SESSION_MARKERS``).

What no longer counts: a remote tool's tray or service process holding an ESTABLISHED TCP
connection. Idle tools keep keep-alive connections to their relays all day, and that switched the
walk-away lock off for hours (F-142). Tools with no known per-connection marker are simply not
detected; presence then treats the session as local.

Also here, unrelated to remoting but the same kind of ambient session probe: whether the
workstation is currently locked by its input desktop (``session_locked``, the fallback of
face_service.session_state).
"""
from __future__ import annotations
import logging

try:
    import psutil  # optional but recommended
except ImportError:
    psutil = None  # type: ignore

import win32api  # type: ignore

try:
    import pywintypes  # type: ignore
    import win32con  # type: ignore
    import win32service  # type: ignore
    import winerror  # type: ignore
except ImportError:  # pragma: no cover - pywin32 is present in every real install
    win32service = None  # type: ignore

log = logging.getLogger(__name__)

# Processes that exist only while a remote side is connected (per-connection helpers). Seen in THIS
# session, they mean the session is being driven remotely.
SESSION_MARKERS = {
    "teamviewer_desktop.exe",      # TeamViewer: spawned into the session on connect
    "remoting_desktop.exe",        # Chrome Remote Desktop: per-connection desktop agent
    "quickassist.exe",             # Microsoft Quick Assist
    "msra.exe",                    # Windows Remote Assistance
}
# Kept as a public alias for older callers.
ALWAYS_REMOTE_PROCS = SESSION_MARKERS

SM_REMOTESESSION = 0x1000
SM_REMOTECONTROL = 0x2001

# Name of the interactive desktop of a normal, unlocked session. The lock screen
# and the secure-desktop (UAC) prompt run on "Winlogon", the screen saver on
# "Screen-saver" -- so anything other than "Default" means the user is not
# looking at their own desktop right now.
UNLOCKED_DESKTOP = "Default"


def is_rdp_session() -> bool:
    """This session is an RDP session, or the console is being remotely controlled (F-148)."""
    for metric in (SM_REMOTESESSION, SM_REMOTECONTROL):
        try:
            if win32api.GetSystemMetrics(metric):
                return True
        except Exception as e:
            log.debug("GetSystemMetrics(0x%X) failed: %s", metric, e)
    return False


def _session_of(pid: int) -> "int | None":
    import ctypes
    sid = ctypes.c_ulong(0)
    try:
        if ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.c_ulong(pid), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return None


def session_locked() -> bool:
    """True when the interactive session is NOT on its normal desktop -- i.e. the
    lock screen (or the secure desktop / screen saver) is up.

    Read the INPUT desktop (the one currently receiving user input) and compare its
    object name against ``UNLOCKED_DESKTOP``. Cheap by construction: one
    OpenInputDesktop plus one name query, no sleep and no retry, so it is safe to
    call from the presence tick.
    """
    if win32service is None:
        # No pywin32 at all -> we cannot tell. Degrade to "not locked" so the caller
        # keeps its previous behaviour (raise the notification) instead of silently
        # suppressing it. Failing this way loses nothing that worked before.
        log.debug("session_locked: win32service unavailable")
        return False
    hdesk = None
    try:
        hdesk = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
        name = win32service.GetUserObjectInformation(hdesk, win32service.UOI_NAME)
    except pywintypes.error as e:
        if e.winerror == winerror.ERROR_ACCESS_DENIED:
            # EXPECTED while locked: the input desktop is Winlogon, which an ordinary
            # process in the user session is not allowed to open. The refusal IS the
            # signal, so report locked rather than treating it as an error.
            return True
        # Any OTHER win32 failure leaves us genuinely unsure. Degrade to "not locked"
        # (status quo: the notification still fires) rather than swallowing it.
        log.debug("session_locked: desktop probe failed: %s", e)
        return False
    except Exception as e:
        # Same reasoning as above, for a non-win32 failure.
        log.debug("session_locked: unexpected failure: %s", e)
        return False
    finally:
        if hdesk is not None:
            try:
                hdesk.CloseDesktop()
            except Exception:
                pass
    return name != UNLOCKED_DESKTOP


def active_remote_tools() -> list[str]:
    """The per-connection remote-tool processes running in THIS session."""
    if psutil is None:
        return []
    import os
    mine = _session_of(os.getpid())
    found: list[str] = []
    for p in psutil.process_iter(attrs=["name", "pid"]):
        try:
            name = (p.info.get("name") or "").lower()
        except Exception:
            continue
        if name not in SESSION_MARKERS:
            continue
        if mine is not None and _session_of(int(p.info.get("pid") or 0)) not in (mine, None):
            continue            # another user's session is being driven, not this one
        found.append(name)
    return found


def is_remote_context() -> tuple[bool, str]:
    if is_rdp_session():
        return True, "rdp"
    tools = active_remote_tools()
    if tools:
        return True, "remote-tool:" + ",".join(sorted(set(tools)))
    return False, ""
