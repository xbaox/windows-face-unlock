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

from face_service.config import PIPE_NAME
# Stage 8b (F-20): the SID helpers and the exchange itself now live in face_service.pipe_io, the one
# client every Python caller shares. The old names stay importable from here (the selftests and
# measure_threshold use them); the behaviour is the same rule, now with a read deadline.
from face_service.pipe_io import (PipeServerIdentityError, SYSTEM_SID_STRING, exchange,  # noqa: F401
                                  self_sid_string as _self_sid_string,
                                  server_sid_allowed as _server_sid_allowed,
                                  server_sid_string as _server_sid_string,
                                  verify_server_identity as _verify_server_identity)

# Whole-exchange budget. Generous: this is the dev tool and the installed graceful-shutdown path,
# and a gesture round can hold the sequential server for ~11 s before it gets to us.
SEND_TIMEOUT_S = 30.0


def send(req: dict, connect_timeout_s: float = SEND_TIMEOUT_S) -> dict:
    """One request/reply through face_service.pipe_io.exchange (server SID checked BEFORE the write
    when pipe_first_instance is on; the whole exchange bounded by ``connect_timeout_s``). Raises
    PipeServerIdentityError on an untrusted server and SystemExit when nothing usable came back."""
    resp, why = exchange(req, connect_timeout_s)
    if why == "untrusted-server":
        raise PipeServerIdentityError("pipe server is not SELF/SYSTEM -- refused to send")
    if resp is None:
        raise SystemExit(f"no reply from {PIPE_NAME}: {why} (is the service running?)")
    return resp


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
