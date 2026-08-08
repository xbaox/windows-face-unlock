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
    # Every import below is ABSOLUTE, and that is load-bearing rather than a style
    # choice. This file is an entry point in both layouts, and the two layouts
    # disagree about what package it belongs to. `python -m presence_monitor`
    # imports it as presence_monitor.__main__, where a leading dot resolves.
    # PyInstaller freezes it as the top-level script `__main__` -- PKG-01.toc
    # records it as ('__main__', ..., 'PYSOURCE') -- and a top-level script has no
    # package context, so `from .monitor import main` raises "ImportError:
    # attempted relative import with no known parent package". That is exactly how
    # face_unlock_tray.exe died on every start: main() ran, the router reached the
    # no-flag branch, and the import under it blew up. All three branches carried
    # the same defect, so --enroll and --set-password were equally dead.
    #
    # Naming presence_monitor.monitor in the spec's hiddenimports (9c0123b) put the
    # module IN the bundle, which is necessary and was not sufficient: it fixed what
    # modulegraph collected, not what these lines do at runtime. The absolute form
    # works in both layouts, because either way the package is importable by name.
    args = list(sys.argv[1:] if argv is None else argv)
    flag = args[0] if args else ""

    if flag == "--enroll":
        from presence_monitor.enroll_gui import main as enroll_main
        return int(enroll_main() or 0)

    if flag == "--set-password":
        from presence_monitor.password_gui import main as password_main
        return int(password_main() or 0)

    if flag == "--pipe-shutdown":
        # Same request the dev path sends, through the same client, so the SID checks and the
        # server-identity verification are identical in both layouts.
        from tools.pipe_client import main as pipe_main
        return int(pipe_main(["shutdown"]))

    # No flag (or an unrecognised one): the tray, exactly as before.
    from presence_monitor.monitor import main as monitor_main
    monitor_main()
    return 0


if __name__ == "__main__":
    # On Windows, freeze_support prevents child processes from re-running main when a module uses
    # multiprocessing.Process without the __main__ guard. face_service/__main__.py has carried
    # this since Stage 1; the tray entry point needs it for the same reason once it is frozen.
    multiprocessing.freeze_support()
    raise SystemExit(main())
