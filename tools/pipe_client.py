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

import pywintypes  # type: ignore
import win32file   # type: ignore
import win32pipe   # type: ignore

PIPE_NAME = r"\\.\pipe\FaceUnlock"


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
    resp = send(req)
    dt_ms = (time.time() - t0) * 1000.0

    if isinstance(resp, dict) and resp.get("password"):
        resp["password"] = "***"
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    print(f"[{dt_ms:.0f} ms]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
