import os
# Prevent TF/Keras from spawning extra worker processes that can steal
# the named pipe and exhaust resources.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "1")
os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
# NOTE (stage 1): the recognition engine moved from tf-keras (CPU) to
# InsightFace on onnxruntime-gpu, so we must NOT hide the GPU anymore.
# TensorFlow (still used only for passive MiniFASNet liveness) stays on CPU
# via TF_* thread limits above and native-Windows TF not seeing CUDA, so
# exposing the GPU here benefits only onnxruntime. Do not re-add
# CUDA_VISIBLE_DEVICES=-1 or the engine silently falls back to CPU.
# (Passive TF liveness is removed in stage 2 with active liveness.)

import multiprocessing

from .service import main

if __name__ == "__main__":
    # On Windows, freeze_support prevents child processes from re-running main
    # when a module uses multiprocessing.Process without the __main__ guard.
    multiprocessing.freeze_support()
    main()
