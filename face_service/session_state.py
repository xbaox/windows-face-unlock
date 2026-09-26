"""Is this session locked? -- asked of the session itself (moved here in Stage 9).

The presence monitor gates its ticks on this, and since Stage 9 (act 9b R10) the service uses it
too: it warms the camera when the session locks and releases it on unlock, so the unlock runs on a
warm camera while the device is free the rest of the time.
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)


# --- "is this session locked" (7c-7) ---------------------------------------------------------
#
# remote_session.session_locked infers the answer from the INPUT DESKTOP: ACCESS_DENIED on
# OpenInputDesktop, or a desktop name other than "Default", means locked. On this hardware it does
# not work -- %USERPROFILE%\.face-unlock\session_lock_probe.log (written by tools.diag.session_lock_probe) holds 609 samples reading desktop='Default' and NOT ONE
# reading 'Winlogon', with a single one-sample True that is flanked by False two seconds either
# side (a blip, not a lock). The live consequence is in presence.log: after the 17:32:47 lock the
# probe kept running at 17:33:47 while the machine was on the lock screen, earned a strike, and
# that strike survived the unlock and locked the machine again ~40s later. The same shape is
# visible on 2026-07-18, two weeks before the gate existed, so this is not a 7c-6 regression --
# the gate has simply never fired here.
#
# So ask the session about itself instead of inferring from a desktop handle:
# WTSQuerySessionInformation(WTSSessionInfoEx) returns WTSINFOEX_LEVEL1.SessionFlags, which IS the
# lock state. It is a plain poll (no window and no message loop, which a tray thread cannot host
# without restructuring), needs no privilege from a medium-IL process, and reports our own session.
#
# NOTE the historical wart: on Windows 7 / Server 2008 R2 the LOCK and UNLOCK values are swapped.
# We target Windows 11, and only the two documented values are trusted -- anything else, including
# WTS_SESSIONSTATE_UNKNOWN, returns None and lets the caller fall back.
_WTS_CURRENT_SERVER_HANDLE = 0
_WTS_CURRENT_SESSION = -1
_WTS_SESSION_INFO_EX = 25        # WTS_INFO_CLASS.WTSSessionInfoEx
_WTS_SESSIONSTATE_LOCK = 0
_WTS_SESSIONSTATE_UNLOCK = 1


class _WTSINFOEX_LEVEL1(ctypes.Structure):
    """WTSINFOEX_LEVEL1_W. Declared IN FULL on purpose: the trailing LARGE_INTEGERs give the
    struct 8-byte alignment, and that alignment is what puts this union at offset 8 inside
    WTSINFOEXW. Declaring only the first three fields would silently read from offset 4."""
    _fields_ = [
        ("SessionId", ctypes.c_ulong),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", ctypes.c_long),
        ("WinStationName", ctypes.c_wchar * 33),
        ("UserName", ctypes.c_wchar * 21),
        ("DomainName", ctypes.c_wchar * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", ctypes.c_ulong),
        ("OutgoingBytes", ctypes.c_ulong),
        ("IncomingFrames", ctypes.c_ulong),
        ("OutgoingFrames", ctypes.c_ulong),
        ("IncomingCompressedBytes", ctypes.c_ulong),
        ("OutgoingCompressedBytes", ctypes.c_ulong),
    ]


class _WTSINFOEX(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_ulong), ("Data", _WTSINFOEX_LEVEL1)]


def session_locked_wts() -> "bool | None":
    """True/False from the session's own lock flag, or None when it cannot be determined.

    None is not "unlocked": it means this probe has no opinion, and the caller falls back. Every
    failure mode -- no wtsapi32, a failed call, a short buffer, an unexpected Level, a SessionFlags
    value outside the two documented ones -- lands there rather than inventing an answer.
    """
    buf = ctypes.c_void_p()
    size = ctypes.c_ulong(0)
    try:
        wts = ctypes.windll.wtsapi32
        ok = wts.WTSQuerySessionInformationW(
            ctypes.c_void_p(_WTS_CURRENT_SERVER_HANDLE), ctypes.c_int(_WTS_CURRENT_SESSION),
            ctypes.c_int(_WTS_SESSION_INFO_EX), ctypes.byref(buf), ctypes.byref(size))
    except Exception as e:
        log.debug("WTSQuerySessionInformation unavailable: %s", e)
        return None
    if not ok or not buf or size.value < ctypes.sizeof(_WTSINFOEX):
        log.debug("WTSQuerySessionInformation failed (ok=%s size=%s)", ok, size.value)
        if buf:
            try:
                ctypes.windll.wtsapi32.WTSFreeMemory(buf)
            except Exception:
                pass
        return None
    try:
        info = ctypes.cast(buf, ctypes.POINTER(_WTSINFOEX)).contents
        if info.Level != 1:
            log.debug("WTSINFOEX Level=%d, expected 1", info.Level)
            return None
        flags = info.Data.SessionFlags
    finally:
        try:
            ctypes.windll.wtsapi32.WTSFreeMemory(buf)
        except Exception:
            pass
    if flags == _WTS_SESSIONSTATE_LOCK:
        return True
    if flags == _WTS_SESSIONSTATE_UNLOCK:
        return False
    log.debug("WTSINFOEX SessionFlags=%r is neither LOCK nor UNLOCK", flags)
    return None
