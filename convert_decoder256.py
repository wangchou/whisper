import whisper
import torch
import coremltools as ct
import os
import numpy as np
from timeit import default_timer as timer

startT = timer()

# model setting
modelSize = "tiny"
model = whisper.load_model(modelSize).cpu()
n_state = { 'tiny': 384, 'base': 512, 'small': 768, 'medium': 1024, 'large': 1280}[modelSize]
n_layer = { 'tiny': 4, 'base': 6, 'small': 12, 'medium': 24, 'large': 32}[modelSize]
n_head = 6

decoder = model.decoder
decoder.eval()

inType=np.float16
# coreml has some issue when output type = fp16 when using ane or gpu
# https://github.com/apple/coremltools/issues/1893
outType=np.float16

bs = 5 # beam_size

# input data for trace

# max token len for first time = max_prefix_len(224) + sot_len(3)
max_n_ctx = decoder.max_n_ctx_for_1st
x = torch.ones((bs, max_n_ctx, n_state))
xa = torch.ones((bs, 1500, n_state))
qk_mask = torch.zeros((max_n_ctx, max_n_ctx))

traced_decoder = torch.jit.trace_module(decoder,
                                        {'forwardBlocks': (x, xa, qk_mask)})
# ct.convert only look forward func
traced_decoder.forward = traced_decoder.forwardBlocks

# input types for convert
input1 = ct.TensorType("x", x.shape, dtype=inType)
input2 = ct.TensorType("xa", xa.shape, dtype=inType)
input3 = ct.TensorType("qk_mask", qk_mask.shape, dtype=inType)
inputs = [input1, input2, input3]

outputs = [ct.TensorType("out_x", dtype=outType),
           ct.TensorType("out_cross_qks", dtype=outType),
           ct.TensorType("out_new_masked_kv_caches", dtype=outType),
           ct.TensorType("out_new_cross_kv_caches", dtype=outType)]

decoder = ct.convert(
    traced_decoder,
    convert_to="mlprogram",
    inputs=inputs,
    outputs=outputs,
    compute_units=ct.ComputeUnit.CPU_AND_NE,
    minimum_deployment_target=ct.target.iOS16, # make fp16 input and output available
)
print(f"converted {timer()-startT:.3f}")

folder_path = f"coreml/{modelSize}"
if not os.path.exists(folder_path):
    os.mkdir(folder_path)
decoder.save(f"{folder_path}/CoremlDecoder256.mlpackage")
print(f"saved {timer()-startT:.3f}")

## test accuracy
torch_output = traced_decoder.forward(x, xa, qk_mask)[0]
print("torch model output:", torch_output[:,0,:2], torch_output[:,max_n_ctx-1,n_state-1])
print(f"torch predicted {timer()-startT:.3f}")

coreml_output = torch.from_numpy(
        decoder.predict({'x': x,
                         'xa': xa,
                         'qk_mask': qk_mask})['out_x']
)
print(f"coreml predicted {timer()-startT:.3f}")
print(f"coreml {modelSize} model output:", coreml_output[:,0,:2], coreml_output[:,max_n_ctx-1,n_state-1])
diff = torch.abs(torch_output - coreml_output).detach()
print("diff avg,max:", torch.mean(diff), torch.max(diff))
