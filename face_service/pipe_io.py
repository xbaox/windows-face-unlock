"""The one Python client for the FaceUnlock pipe (Stage 8b, F-20).

Defect -> consequence -> fix:

* Defect. Four Python clients talked to the pipe, each its own way. The tray / presence monitor /
  enrollment wizard (presence_monitor.monitor.pipe_call) and the watchdog ping never checked WHO
  owned the pipe, and pipe_call's ReadFile had no deadline; only the dev tool tools/pipe_client.py
  checked the server SID, and it too read without a bound.
* Consequence. Whoever held the pipe name could be sent the tray's requests (status, presence,
  shutdown, clear_enrollment ...) and could stall any of these clients forever by accepting the
  connection and never answering -- the residual Stage 4 left as "sanctioned scope".
* Fix. Every Python client goes through ``exchange`` below: connect (retrying only the two
  refusals that mean "the sequential server is between instances"), confirm the server process
  runs as SELF or SYSTEM BEFORE anything is written (the same rule the Credential Provider and
  tools/pipe_client.py apply), then write and read with overlapped I/O bounded by ONE deadline for
  the whole exchange. On expiry the pending operation is cancelled and DRAINED before the buffer
  and handle are released (the kernel owns them until the cancelled op completes) -- the pattern
  tools/watchdog.py's ping has used since 7c-2, now shared.

The server-SID check is governed by ``pipe_first_instance`` like before (config.py documents both
halves under that one toggle); the service ratchets that key, so a reload cannot switch it off.
No camera, no numpy: safe to import from any process.
"""
from __future__ import annotations

import functools
import json
import logging
import time

import pywintypes      # type: ignore
import win32api        # type: ignore
import win32con        # type: ignore
import win32event      # type: ignore
import win32file       # type: ignore
import win32pipe       # type: ignore
import win32security   # type: ignore
import winerror        # type: ignore

from .config import PIPE_NAME

log = logging.getLogger(__name__)

SYSTEM_SID_STRING = "S-1-5-18"
RETRY_SLEEP_S = 0.1        # connect retry cadence inside the budget (not a budget itself)
READ_BUFFER = 65536


class PipeServerIdentityError(RuntimeError):
    """The pipe server is not running as SELF or SYSTEM -> a possible squatter owns the name."""


def self_sid_string() -> str:
    from .config import _current_user_sid
    return _current_user_sid()


def server_sid_string(pipe_handle) -> str:
    """SID of the process on the SERVER end of an open pipe handle:
    GetNamedPipeServerProcessId -> OpenProcess(QUERY_LIMITED) -> token -> SID. Raises on failure."""
    pid = win32pipe.GetNamedPipeServerProcessId(pipe_handle)
    ph = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        th = win32security.OpenProcessToken(ph, win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(th)
    finally:
        win32api.CloseHandle(ph)
    return win32security.ConvertSidToStringSid(sid)


def server_sid_allowed(server_sid: str, self_sid: str) -> bool:
    """The pipe server must be SELF (our user-session service) or SYSTEM (the lockscreen CP)."""
    return server_sid == self_sid or server_sid == SYSTEM_SID_STRING


def verify_server_identity(pipe_handle) -> None:
    """Raise PipeServerIdentityError unless the server is SELF or SYSTEM. A server whose identity
    cannot be read at all is refused too: "could not check" must not mean "trusted"."""
    try:
        server_sid = server_sid_string(pipe_handle)
    except Exception as e:
        raise PipeServerIdentityError(f"pipe server identity unreadable ({e!r}) -- refusing to send")
    if not server_sid_allowed(server_sid, self_sid_string()):
        raise PipeServerIdentityError(
            f"pipe server SID {server_sid} is not SELF/SYSTEM -- refusing to send (possible squatter)")


@functools.lru_cache(maxsize=1)
def _verify_by_default() -> bool:
    """pipe_first_instance, read once per process; any failure -> True (check)."""
    try:
        from .config import Config
        return bool(Config.load().pipe_first_instance)
    except Exception:
        return True


def exchange(req: dict, timeout_s: float, *, pipe_name: str = "",
             verify_server: "bool | None" = None) -> "tuple[dict | None, str | None]":
    """Send ``req`` and return ``(reply, None)``, or ``(None, reason)``. Never raises.

    ``timeout_s`` bounds the WHOLE exchange -- connect, identity check, write and read. Reasons:
    ``"busy"`` / ``"no-pipe"`` (connect refused until the budget ran out), ``"untrusted-server"``
    (identity check failed; nothing was written), ``"reply-timeout"``, ``"bad-reply"`` (not a
    JSON object), ``"error: ..."`` (anything else, with the win32 code)."""
    name = pipe_name or PIPE_NAME
    check = _verify_by_default() if verify_server is None else bool(verify_server)
    deadline = time.monotonic() + float(timeout_s)

    def _shut(h) -> None:
        try:
            win32file.CloseHandle(h)
        except Exception:
            pass

    def _sleep_within() -> bool:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(RETRY_SLEEP_S, left))
        return True

    h = None
    stalled = "no-pipe"
    while True:
        try:
            h = win32file.CreateFile(
                name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, win32con.FILE_FLAG_OVERLAPPED, None)
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_PIPE_BUSY:
                stalled = "busy"
            elif e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                stalled = "no-pipe"
            else:
                return None, f"error: connect winerror={e.winerror}"
            if not _sleep_within():
                return None, stalled
            continue
        try:
            win32pipe.SetNamedPipeHandleState(h, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        except pywintypes.error:
            # The server tore this instance down between CreateFile and here: same race as busy.
            _shut(h)
            h = None
            stalled = "busy"
            if not _sleep_within():
                return None, stalled
            continue
        break

    if check:
        try:
            verify_server_identity(h)
        except PipeServerIdentityError as e:
            log.warning("%s", e)
            _shut(h)
            return None, "untrusted-server"

    ov = pywintypes.OVERLAPPED()
    ov.hEvent = win32event.CreateEvent(None, True, False, None)

    def _await():
        ms = max(0, int((deadline - time.monotonic()) * 1000))
        if win32event.WaitForSingleObject(ov.hEvent, ms) == win32event.WAIT_OBJECT_0:
            return win32file.GetOverlappedResult(h, ov, False)
        win32file.CancelIo(h)
        try:
            win32file.GetOverlappedResult(h, ov, True)   # MUST finish before anything is freed
        except Exception:
            pass
        return None

    try:
        win32event.ResetEvent(ov.hEvent)
        win32file.WriteFile(h, (json.dumps(req) + "\n").encode("utf-8"), ov)
        if _await() is None:
            return None, "reply-timeout"
        buf = win32file.AllocateReadBuffer(READ_BUFFER)
        win32event.ResetEvent(ov.hEvent)
        win32file.ReadFile(h, buf, ov)
        n = _await()
        if n is None:
            return None, "reply-timeout"
        try:
            resp = json.loads(bytes(buf[:n]).decode("utf-8").strip())
        except Exception:
            return None, "bad-reply"
        if not isinstance(resp, dict):
            return None, "bad-reply"
        return resp, None
    except pywintypes.error as e:
        return None, f"error: exchange winerror={e.winerror}"
    except Exception as e:
        return None, f"error: {e!r}"
    finally:
        _shut(ov.hEvent)
        _shut(h)
