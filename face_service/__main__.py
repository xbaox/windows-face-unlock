import os
# Cap native thread pools before anything heavy is imported. An unbounded pool
# competes with the pipe server thread and makes the first verify slower.
os.environ.setdefault("OMP_NUM_THREADS", "1")
# Recognition is InsightFace on onnxruntime-gpu (stage 1). Do NOT re-add
# CUDA_VISIBLE_DEVICES=-1: that variable HIDES every GPU, and the engine then
# falls back to CPU silently. Removing it is what made GPU inference work.

import multiprocessing
import sys

# Stage 8b-2: the hidden custody self-check the build gate runs out of the FROZEN exe (see
# face_service/selfcheck.py). Routed here, BEFORE face_service.service is imported, so the mode
# never loads the engine, the camera or the pipe code, and never reads the config.
if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck-custody":
    from face_service.selfcheck import selfcheck_custody_main
    raise SystemExit(selfcheck_custody_main(sys.argv[1:]))

# ABSOLUTE, and it must stay absolute. This file is an entry point in both
# layouts, and the two layouts disagree about what package it belongs to.
# `python -m face_service` imports it as face_service.__main__, where a leading
# dot resolves. PyInstaller freezes it as the top-level script `__main__` --
# PKG-00.toc records it as ('__main__', ..., 'PYSOURCE') -- and a top-level
# script has no package context at all, so `from .service import main` raises
# ImportError: attempted relative import with no known parent package before a
# single line of the service runs. Naming face_service.service in the spec's
# hiddenimports (9c0123b) put the module IN the bundle, which is necessary and
# was not sufficient: it fixed what modulegraph collected, not what this line
# does at runtime. The absolute form works in both layouts because either way
# the package is importable by name.
from face_service.service import main

if __name__ == "__main__":
    # On Windows, freeze_support prevents child processes from re-running main
    # when a module uses multiprocessing.Process without the __main__ guard.
    multiprocessing.freeze_support()
    main()
