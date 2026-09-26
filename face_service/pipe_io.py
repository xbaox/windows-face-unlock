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

Stage 9 (R1 / R2, F-57 / F-63 / F-64). The server check is no longer a config toggle: every
exchange verifies the server, whatever config.toml says. The server must run as SELF -- the
Python clients (tray, monitor, wizard, watchdog) run as the product's owner, and so does the
service -- and the pipe OBJECT must be owned by SELF as well, so a server judged by a reused PID
cannot pass on the process check alone. The client opens the pipe with SECURITY_SQOS_PRESENT |
SECURITY_IDENTIFICATION: a server can identify the caller, never impersonate it.
No camera, no numpy: safe to import from any process.
"""
from __future__ import annotations

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
# CreateFile flags pywin32 does not export: identification-level impersonation only (F-63).
SECURITY_SQOS_PRESENT = 0x00100000
SECURITY_IDENTIFICATION = 0x00010000
RETRY_SLEEP_S = 0.1        # connect retry cadence inside the budget (not a budget itself)
READ_BUFFER = 65536


class PipeServerIdentityError(RuntimeError):
    """The pipe server is not running as SELF -> a possible squatter owns the name."""


def self_sid_string() -> str:
    from .identity import current_user_sid
    return current_user_sid()


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


def pipe_owner_sid_string(pipe_handle) -> str:
    """Owner SID of the pipe OBJECT behind an open handle (F-64). Unlike the server PID, the
    owner is fixed when the pipe is created, so it cannot be misread through PID reuse."""
    sd = win32security.GetSecurityInfo(pipe_handle, win32security.SE_KERNEL_OBJECT,
                                       win32security.OWNER_SECURITY_INFORMATION)
    return win32security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner())


def server_sid_allowed(server_sid: str, self_sid: str) -> bool:
    """Stage 9 (R1): the pipe server must be SELF -- the owner's own service. SYSTEM is no longer
    accepted: the service never runs as SYSTEM, so a SYSTEM server is not ours."""
    return bool(server_sid) and server_sid == self_sid


def verify_server_identity(pipe_handle) -> None:
    """Raise PipeServerIdentityError unless both the server process and the pipe object belong to
    SELF. An identity that cannot be read is refused too: "could not check" must not mean
    "trusted"."""
    try:
        server_sid = server_sid_string(pipe_handle)
        owner = pipe_owner_sid_string(pipe_handle)
    except Exception as e:
        raise PipeServerIdentityError(f"pipe server identity unreadable ({e!r}) -- refusing to send")
    me = self_sid_string()
    if not server_sid_allowed(server_sid, me):
        raise PipeServerIdentityError(
            f"pipe server SID {server_sid} is not SELF -- refusing to send (possible squatter)")
    if owner != me:
        raise PipeServerIdentityError(
            f"pipe object owner {owner} is not SELF -- refusing to send (possible squatter)")


def exchange(req: dict, timeout_s: float, *,
             pipe_name: str = "") -> "tuple[dict | None, str | None]":
    """Send ``req`` and return ``(reply, None)``, or ``(None, reason)``. Never raises.

    ``timeout_s`` bounds the WHOLE exchange -- connect, identity check, write and read. Reasons:
    ``"busy"`` / ``"no-pipe"`` (connect refused until the budget ran out), ``"untrusted-server"``
    (identity check failed; nothing was written), ``"reply-timeout"``, ``"bad-reply"`` (not a
    JSON object), ``"error: ..."`` (anything else, with the win32 code)."""
    name = pipe_name or PIPE_NAME
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
                0, None, win32file.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, None)
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
