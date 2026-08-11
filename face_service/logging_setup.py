"""One logging setup, shared by every long-running entry point.

Stage 7d-B. Four processes -- the service, the presence monitor, the enrollment wizard and the
watchdog -- each had their own copy of the same ``logging.basicConfig`` call, and all four copies
carried the same two defects:

**No rotation.** A plain ``FileHandler`` appends forever. On this machine that had already grown
``service.log`` past 10 MB and ``presence.log`` past 2 MB, with nothing anywhere in the repo, the
installer or the updater that ever truncates or archives them. The audit trail has had a size cap
since Stage 2 (``face_service/audit.py``, ``audit_max_mb``); the four logging-module files never
did. They rotate here instead, at a fixed 5 MB x 2 backups -- deliberately NOT a config knob,
because Config is frozen at 54 fields and a log cap is not something a user should have to tune.

**An unconditional StreamHandler.** All four run under ``pythonw.exe`` (and, once frozen, under a
``console=False`` bundle), where ``sys.stderr`` is None. ``StreamHandler`` binds ``sys.stderr`` at
CONSTRUCTION time, so ``self.stream`` became None and every single record then walked the whole
path -- format the message, call ``stream.write``, raise AttributeError, get caught by ``emit``,
land in ``handleError`` -- only for ``handleError`` to discard it silently because it is itself
gated on ``if raiseExceptions and sys.stderr:``. Nothing was ever lost (the file handler is first
in the list and had already written the record) and nothing crashed; it was pure waste, per record,
in four permanently-running processes. The stream handler is now attached only when there is a
stream to attach it to, which also restores its actual purpose: running any of these by hand from
a console still prints.

The format string is byte-for-byte the one all four sites used, so nothing downstream that reads
these files has to change.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Byte-for-byte the format the four call sites shared before this module existed.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 2


def setup_logging(path: Path, level: int = logging.INFO) -> None:
    """Configure the ROOT logger to write ``path`` with rotation, plus stderr when it exists.

    Call once, first thing in an entry point's ``main()``. ``basicConfig`` is a no-op if the root
    logger already has handlers, which is the desired behaviour for a second accidental call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
        )
    ]
    # Under pythonw.exe and a windowed frozen build sys.stderr is None; a StreamHandler built on
    # it fails on every record and swallows the failure. Attach one only if there is a console.
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers)
