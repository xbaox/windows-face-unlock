"""Detect remote-access contexts so the presence monitor can skip auto-lock.

Two tiers of detection:

1. ALWAYS_REMOTE_PROCS — processes that only exist while a remote session is
   *currently connected* (e.g. TeamViewer_Desktop.exe, Quick Assist).
   Seeing them is enough to treat as remote.

2. CONNECTION_CHECKED_PROCS — processes that run persistently (tray / service)
   and only mean "active remote" when they hold an ESTABLISHED TCP connection
   to a non-loopback peer (UltraViewer, AnyDesk, RustDesk, Parsec, ...).

Also: RDP session via GetSystemMetrics(SM_REMOTESESSION), and -- unrelated to
remoting, but the same kind of ambient session probe -- whether the workstation
is currently locked (see ``session_locked``).
"""
from __future__ import annotations
import ipaddress
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

# Always indicate an active remote connection on sight.
ALWAYS_REMOTE_PROCS = {
    "teamviewer_desktop.exe",      # TeamViewer spawns this on connect
    "remoting_desktop.exe",        # Chrome Remote Desktop (per-connection)
    "quickassist.exe",             # Microsoft Quick Assist
    "msra.exe",                    # Windows Remote Assistance
}

# Tray/service processes that run all the time; only treat as active when
# they hold an ESTABLISHED external TCP connection.
CONNECTION_CHECKED_PROCS = {
    "ultraviewer_desktop.exe",
    "ultraviewer_service.exe",
    "anydesk.exe",
    "rustdesk.exe",
    "remoting_host.exe",           # Chrome Remote Desktop host
    "parsecd.exe",
    "sunshine.exe",
    "srserver.exe",                # Splashtop
    "teamviewer.exe",              # full idle tray (checked via connection)
}

SM_REMOTESESSION = 0x1000

# Name of the interactive desktop of a normal, unlocked session. The lock screen
# and the secure-desktop (UAC) prompt run on "Winlogon", the screen saver on
# "Screen-saver" -- so anything other than "Default" means the user is not
# looking at their own desktop right now.
UNLOCKED_DESKTOP = "Default"


def _is_external(addr: str) -> bool:
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_link_local)


def is_rdp_session() -> bool:
    try:
        return bool(win32api.GetSystemMetrics(SM_REMOTESESSION))
    except Exception as e:
        log.debug("GetSystemMetrics failed: %s", e)
        return False


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


def _proc_has_external_established(proc: "psutil.Process") -> bool:
    try:
        for c in proc.net_connections(kind="tcp"):
            if c.status == psutil.CONN_ESTABLISHED and c.raddr and _is_external(c.raddr.ip):
                return True
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False
    except Exception as e:
        log.debug("net_connections failed: %s", e)
    return False


def active_remote_tools() -> list[str]:
    if psutil is None:
        return []
    found: list[str] = []
    for p in psutil.process_iter(attrs=["name"]):
        try:
            name = (p.info.get("name") or "").lower()
        except Exception:
            continue
        if name in ALWAYS_REMOTE_PROCS:
            found.append(name)
        elif name in CONNECTION_CHECKED_PROCS:
            if _proc_has_external_established(p):
                found.append(f"{name}(connected)")
    return found


def is_remote_context() -> tuple[bool, str]:
    if is_rdp_session():
        return True, "rdp"
    tools = active_remote_tools()
    if tools:
        return True, "remote-tool:" + ",".join(sorted(set(tools)))
    return False, ""
