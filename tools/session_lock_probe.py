"""Read-only probe for presence_monitor.remote_session.session_locked().

Why it exists: the predicate that gates the lockout toast has to be correct exactly
when you cannot see a console -- while the workstation is locked. So this tool has two
modes:

  * one-shot  -- run it from an unlocked console and read the answer directly.
  * --watch   -- run it, lock the screen, unlock, then read the log: every sample is
                 timestamped, so the lock and unlock transitions are visible after the
                 fact.

Read-only in the strict sense: it opens no camera, sends no pipe command, and touches
no service or scheduled task. The only thing it writes is its own log file.

Run:
  python -m tools.session_lock_probe
  python -m tools.session_lock_probe --watch            (Ctrl-C to stop)
  python -m tools.session_lock_probe --watch --interval 1.0
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from face_service.config import APP_DIR
from presence_monitor.remote_session import UNLOCKED_DESKTOP, session_locked

LOG_PATH = APP_DIR / "session_lock_probe.log"


def _desktop_name() -> str:
    """The raw input-desktop name behind the predicate, for the log. Never raises --
    this is diagnostics, and the interesting case (locked) is exactly the one where the
    query is refused."""
    try:
        import win32con  # type: ignore
        import win32service  # type: ignore
    except ImportError:
        return "?(no pywin32)"
    hdesk = None
    try:
        hdesk = win32service.OpenInputDesktop(0, False, win32con.DESKTOP_READOBJECTS)
        return str(win32service.GetUserObjectInformation(hdesk, win32service.UOI_NAME))
    except Exception as e:
        return f"?({type(e).__name__})"
    finally:
        if hdesk is not None:
            try:
                hdesk.CloseDesktop()
            except Exception:
                pass


def _sample() -> str:
    locked = session_locked()
    return (f"{datetime.now().isoformat(timespec='seconds')} "
            f"session_locked={locked!s:5} desktop={_desktop_name()!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Probe session_locked() (read-only).")
    ap.add_argument("--watch", action="store_true",
                    help=f"sample continuously and append to {LOG_PATH}")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between samples in --watch mode (default: 2.0)")
    args = ap.parse_args(argv)

    if not args.watch:
        print(_sample())
        print(f"(unlocked desktop is {UNLOCKED_DESKTOP!r}; anything else means locked/away)")
        return 0

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"watching every {args.interval:g}s -> {LOG_PATH}")
    print("lock the screen, wait a few samples, unlock, then read that file. Ctrl-C to stop.")
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"--- watch started {datetime.now().isoformat(timespec='seconds')} ---\n")
            fh.flush()
            while True:
                line = _sample()
                fh.write(line + "\n")
                fh.flush()   # flush every sample: the interesting ones are written while
                             # the screen is locked and you only read them afterwards
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped. Log: {LOG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
