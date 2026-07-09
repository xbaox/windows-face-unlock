"""Minimal named-pipe client for the face-unlock service (dev / camera testing).

Usage (from the repo root, with the service running):
    python tools/pipe_client.py ping
    python tools/pipe_client.py status
    python tools/pipe_client.py verify
    python tools/pipe_client.py unlock
    python tools/pipe_client.py presence
    python tools/pipe_client.py challenge            # random gesture
    python tools/pipe_client.py challenge nod         # forced kind: blink|turn_left|turn_right|nod
    python tools/pipe_client.py reload_config
    python tools/pipe_client.py pause_camera 30

Sends one JSON command line and prints the JSON reply plus the round-trip time.
The plaintext password from `unlock` is masked before printing.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so face_service imports

import pywintypes    # type: ignore
import win32api      # type: ignore
import win32con      # type: ignore
import win32file     # type: ignore
import win32pipe     # type: ignore
import win32security  # type: ignore

from face_service.config import Config

PIPE_NAME = r"\\.\pipe\FaceUnlock"
SYSTEM_SID_STRING = "S-1-5-18"


class PipeServerIdentityError(RuntimeError):
    """The pipe server is not running as SELF or SYSTEM -> a possible squatter owns the name."""


def _self_sid_string() -> str:
    th = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(th, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(th)
    return win32security.ConvertSidToStringSid(sid)


def _server_sid_string(pipe_handle) -> str:
    """SID of the process on the SERVER end of an open pipe handle:
    GetNamedPipeServerProcessId -> OpenProcess(QUERY_LIMITED) -> token -> SID."""
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


def _server_sid_allowed(server_sid: str, self_sid: str) -> bool:
    """The pipe server must be SELF (our user-session service) or SYSTEM (the lockscreen CP)."""
    return server_sid == self_sid or server_sid == SYSTEM_SID_STRING


def _verify_server_identity(pipe_handle) -> None:
    """Emulate the Stage-5 C++ CP check: verify the pipe server runs as SELF or SYSTEM BEFORE
    sending the request. A mismatch means a squatter owns the pipe name -> refuse (do not send).
    The real CP does the equivalent in C++ (GetNamedPipeServerProcessId + token SID)."""
    server_sid = _server_sid_string(pipe_handle)
    if not _server_sid_allowed(server_sid, _self_sid_string()):
        raise PipeServerIdentityError(
            f"pipe server SID {server_sid} is not SELF/SYSTEM -- refusing to send (possible squatter)")


def send(req: dict, connect_timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + connect_timeout_s
    handle = None
    while handle is None:
        try:
            handle = win32file.CreateFile(
                PIPE_NAME,
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None,
            )
        except pywintypes.error:
            if time.monotonic() >= deadline:
                raise SystemExit(f"could not open {PIPE_NAME} (is the service running?)")
            time.sleep(0.1)
    try:
        # Anti-squatting (Stage 4 Step 4): confirm the server is SELF/SYSTEM BEFORE sending anything.
        # Gated by cfg.pipe_first_instance (default on); best-effort config read -> default to checking.
        try:
            _check = Config.load().pipe_first_instance
        except Exception:
            _check = True
        if _check:
            _verify_server_identity(handle)
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, json.dumps(req).encode("utf-8"))
        _hr, data = win32file.ReadFile(handle, 65536)
        return json.loads(data.decode("utf-8"))
    finally:
        win32file.CloseHandle(handle)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    req: dict = {"cmd": cmd}
    if cmd == "challenge" and len(argv) > 1:
        req["kind"] = argv[1]
    elif cmd == "pause_camera" and len(argv) > 1:
        req["seconds"] = float(argv[1])

    t0 = time.time()
    try:
        resp = send(req)
    except PipeServerIdentityError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    dt_ms = (time.time() - t0) * 1000.0

    if isinstance(resp, dict) and resp.get("password"):
        resp["password"] = "***"
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    print(f"[{dt_ms:.0f} ms]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
