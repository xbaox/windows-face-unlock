"""Entry point for the tray/presence process -- and, when frozen, for the tools it re-execs.

A frozen build has no ``-m`` entry point. The bootloader runs the one script the EXE was built
from and hands it the remaining argv, so ``face_unlock_tray.exe -m presence_monitor.enroll_gui``
does not start the wizard: it starts a SECOND TRAY and passes the rest of the command line to it
as sys.argv, which the tray then ignores. Nothing in presence_monitor holds a single-instance
mutex, so that duplicate really would run and start competing for the camera.

The fix has two halves. This module is the first: a flag router, so the one bundled executable can
be asked for any of the things the dev layout reached with ``-m``. The second is in tray.py, which
picks the flag form when ``sys.frozen`` is set and the ``-m`` form when it is not.

    face_unlock_tray.exe                    -> tray + presence monitor (the default, unchanged)
    face_unlock_tray.exe --enroll           -> the enrollment wizard, in its own process
    face_unlock_tray.exe --set-password     -> the DPAPI password dialog
    face_unlock_tray.exe --pipe-shutdown    -> ask the service to stop, then exit

``--pipe-shutdown`` exists for tools/register_tasks.ps1. Its graceful-shutdown step shells out to
``python -m tools.pipe_client shutdown``, which cannot work on an installed machine: there is no
tools/ tree under {app} and no interpreter to run it with, so every stop there degraded into the
hard kill that the graceful path was added to avoid.
"""
from __future__ import annotations

import multiprocessing
import sys


def main(argv: "list[str] | None" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    flag = args[0] if args else ""

    if flag == "--enroll":
        from .enroll_gui import main as enroll_main
        return int(enroll_main() or 0)

    if flag == "--set-password":
        from .password_gui import main as password_main
        return int(password_main() or 0)

    if flag == "--pipe-shutdown":
        # Same request the dev path sends, through the same client, so the SID checks and the
        # server-identity verification are identical in both layouts.
        from tools.pipe_client import main as pipe_main
        return int(pipe_main(["shutdown"]))

    # No flag (or an unrecognised one): the tray, exactly as before.
    from .monitor import main as monitor_main
    monitor_main()
    return 0


if __name__ == "__main__":
    # On Windows, freeze_support prevents child processes from re-running main when a module uses
    # multiprocessing.Process without the __main__ guard. face_service/__main__.py has carried
    # this since Stage 1; the tray entry point needs it for the same reason once it is frozen.
    multiprocessing.freeze_support()
    raise SystemExit(main())
