import os
# Cap native thread pools before anything heavy is imported. An unbounded pool
# competes with the pipe server thread and makes the first verify slower.
os.environ.setdefault("OMP_NUM_THREADS", "1")
# The two TF_* variables are vestigial: TensorFlow left the project in stage 2
# together with the passive MiniFASNet liveness that needed it, and nothing
# here imports it any more. They are harmless no-ops on an interpreter that
# never loads TF. Deleting them is a code change rather than a comment fix, so
# it is deferred rather than smuggled into a docs-only commit.
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "1")
os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
# Recognition is InsightFace on onnxruntime-gpu (stage 1). Do NOT re-add
# CUDA_VISIBLE_DEVICES=-1: that variable HIDES every GPU, and the engine then
# falls back to CPU silently. Removing it is what made GPU inference work.

import multiprocessing

from .service import main

if __name__ == "__main__":
    # On Windows, freeze_support prevents child processes from re-running main
    # when a module uses multiprocessing.Process without the __main__ guard.
    multiprocessing.freeze_support()
    main()
