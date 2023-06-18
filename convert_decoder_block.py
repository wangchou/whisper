import whisper
import torch
import coremltools as ct
from coremltools.models.neural_network import quantization_utils
import os
import numpy as np

# model setting
modelSize = "tiny"
model = whisper.load_model(modelSize).cpu()
n_state = 384 # tiny=384, base=512, small=768, medium=1024, large=1280

# trace model by torch.jit
decoder = model.decoder
decoder.eval()
#decoder block input
# x               torch.Size([5, 1, 384]),
# text_offset 18
# xa              torch.Size([5, 1500, 384])
# mask            torch.Size([448, 448])
# masked_kv_cache torch.Size([2, 5, 448, 384])
# cross_kv_cache  torch.Size([2, 5, 1500, 384])
bs = 5 # beam_size
n_ctx = 448
x = torch.ones((bs, 1, n_state))
text_offset = torch.ones(1, dtype=torch.int32)
xa = torch.ones((bs, 1500, n_state))
mask = torch.ones((n_ctx, n_ctx))
masked_kv_cache = torch.ones((2, bs, n_ctx, n_state))
cross_kv_cache = torch.ones((2, bs, 1500, n_state))

# convert to coreml model
#input1 = ct.TensorType(name="x", shape=ct.Shape(shape=(5,
#                                                       ct.RangeDim(lower_bound=1, upper_bound=5, default=1),
#                                                       n_state)))
input1 = ct.TensorType(name="x", shape=x.shape)
input2 = ct.TensorType(name="text_offset", shape=text_offset.shape, dtype=np.int32)
input3 = ct.TensorType(name="xa", shape=xa.shape)
input4 = ct.TensorType(name="mask", shape=mask.shape)
input5 = ct.TensorType(name="masked_kv_cache", shape=masked_kv_cache.shape)
input6 = ct.TensorType(name="cross_kv_cache", shape=cross_kv_cache.shape)

#decoder block output
#x                   torch.Size([5, 1, 384])
#cross_qk            torch.Size([5, 6, 1, 1500])
#new_masked_kv_cache torch.Size([2, 5, 448, 384])
#new_cross_kv_cache  torch.Size([2, 5, 1500, 384])
head_count = 6
cross_qk = torch.ones((bs, head_count, 1, 1500))
output1 = ct.TensorType(name="x")
output2 = ct.TensorType(name="cross_qk")
output3 = ct.TensorType(name="new_masked_kv_cache")
output4 = ct.TensorType(name="new_cross_kv_cache")

traced_decoder_block = torch.jit.trace(decoder.blocks[0], (x, text_offset, xa, mask, masked_kv_cache, cross_kv_cache))
decoder_block = ct.convert(
    traced_decoder_block,
    convert_to="mlprogram",
    inputs=[input1, input2, input3, input4, input5, input6],
    compute_units=ct.ComputeUnit.ALL,
)

folder_path = f"coreml/{modelSize}"
if not os.path.exists(folder_path):
    os.mkdir(folder_path)
decoder_block.save(f"{folder_path}/DecoderBlock.mlpackage")

# test accuracy
#torch_output = traced_decoder_block.forward([input1, input2, input3, input4, input5, input6])
#print("torch model output:", torch_output)
#melSegment = melSegment.cpu().detach().numpy()
#coreml_output = torch.from_numpy(
#  list(encoder_fp16.predict({'melSegment': melSegment}).values())[0]
#)
#print(f"coreml {modelSize} model output:", coreml_output)
#diff = torch.abs(torch_output - coreml_output).detach()
#print("diff avg,max:", torch.mean(diff), torch.max(diff))

# note
# convertion time on Macbook M1 Air 16GB
# tiny:       28s
# small:   5 mins
# medium: 40 mins (29GB)
# large:  crashed, use 60+GB memory after 23mins
