print(">>> PRELOAD PROBE v2 <<<")
import numpy as np
import onnxruntime as ort

ort.preload_dlls()                 # грузит CUDA/cuDNN/cublas DLL из site-packages/nvidia/*
print("=== ORT debug info ===")
try:
    ort.print_debug_info()
except Exception as e:
    print("print_debug_info недоступен:", e)

from onnx import helper, TensorProto
X = helper.make_tensor_value_info("X", TensorProto.FLOAT, [1])
Y = helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1])
g = helper.make_graph([helper.make_node("Add", ["X","X"], ["Y"])], "g", [X], [Y])
m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 18)])
sess = ort.InferenceSession(m.SerializeToString(),
                            providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
print("session providers:", sess.get_providers())
print("run ok:", sess.run(None, {"X": np.array([21.0], dtype=np.float32)})[0])
print(">>> CUDA OK <<<" if "CUDAExecutionProvider" in sess.get_providers()
      else ">>> всё ещё CPU — смотри debug info / E: / W: выше <<<")
