from timeit import default_timer as timer
################################################
# Coreml Encoder part
from ctypes import cdll, c_int, c_float, c_char_p, c_void_p, c_bool, POINTER
import ctypes
import torch

f32Ptr = POINTER(c_float)
logPredictTime = False

totalLoadTime = 0
totalEncoderTime = 0
totalDecoder1Time = 0
totalDecoder256Time = 0
totalCrossKVTime = 0

class CoremlEncoder():
    def __init__(self, n_layer: int, n_state: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.modelName = modelName
        self.encoderObj = None

    def loadModel(self):
        global totalLoadTime
        startT = timer()
        if self.encoderObj == None:
            self.encoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/encoder.so')
            self.encoderObj.loadModel.argtypes = [c_char_p, c_int, c_int]
            self.encoderObj.loadModel.restype = None
            c_string = bytes(f'./coreml/{self.modelName}', 'ascii')
            self.encoderObj.loadModel(c_string, self.n_layer, self.n_state)
        totalLoadTime += timer()-startT

    def predictWith(self, melSegment):
        global totalEncoderTime
        if self.encoderObj == None:
            self.loadModel()
        startT = timer()
        self.encoderObj.predictWith.argtypes = [f32Ptr, f32Ptr]
        self.encoderObj.predictWith.restypes = None

        # force memory continuous, this is very important
        melSegment = melSegment.contiguous()
        melSegmentDataPtr = ctypes.cast(melSegment.data_ptr(), f32Ptr)

        # alloc output buffer
        output_floats = torch.ones((1, 1500, self.n_state), dtype=torch.float32).contiguous()
        output_floats_ptr = ctypes.cast(output_floats.data_ptr(), f32Ptr)
        self.encoderObj.predictWith(melSegmentDataPtr, output_floats_ptr)
        if logPredictTime:
            print(f"\tcoreml encoder {timer()-startT:.3f}")
        totalEncoderTime += timer() - startT
        return output_floats

    def closeModel(self):
        if self.encoderObj != None:
            self.encoderObj.closeModel.argtypes = None
            self.encoderObj.closeModel.restypes = None
            self.encoderObj.closeModel()
            self.encoderObj = None

########################################
class CoremlDecoder256():
    def __init__(self, n_layer: int, n_state: int, n_head: int, n_alignment_head: int, modelName: str):
        self.n_layer = n_layer
        self.n_state = n_state
        self.n_head = n_head
        self.n_alignment_head = n_alignment_head
        self.modelName = modelName
        self.decoderObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        global totalLoadTime
        startT = timer()
        if self.mlmodel_handle == None:
            self.decoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/decoder256.so')
            self.decoderObj.loadModel.argtypes = [c_char_p, c_int, c_int, c_int, c_int]
            self.decoderObj.loadModel.restype = c_void_p
            c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder256.mlmodelc', 'ascii')
            self.mlmodel_handle = self.decoderObj.loadModel(c_string, self.n_layer, self.n_state, self.n_head, self.n_alignment_head)

            bs = 1 # beam_size
            n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
            n_state = self.n_state
            n_layer = self.n_layer
            n_alignment_head = self.n_alignment_head
            max_n_ctx = 256

            dtype1=torch.float32
            # prepare output buffers
            self.out_x = torch.ones((bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
            self.out_cross_head_weights = torch.ones((n_alignment_head, max_n_ctx, 1500), dtype=dtype1).contiguous()
            self.new_masked_kv_caches = torch.ones((n_layer * 2, bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
            self.outXPtr = ctypes.cast(self.out_x.data_ptr(), f32Ptr)
            self.outCHWPtr = ctypes.cast(self.out_cross_head_weights.data_ptr(), f32Ptr)
            self.outMKVPtr = ctypes.cast(self.new_masked_kv_caches.data_ptr(), f32Ptr)
        totalLoadTime += timer()-startT

    def predictWith(self, x, qk_mask, cross_k_caches, cross_v_caches, isNewCKV):
        global totalDecoder256Time
        if self.mlmodel_handle == None:
            self.loadModel()
        startT = timer()
        self.decoderObj.predictWith.argtypes = [c_void_p,
                                                f32Ptr, f32Ptr, f32Ptr, f32Ptr,
                                                c_bool,
                                                f32Ptr, f32Ptr, f32Ptr]
        self.decoderObj.predictWith.restypes = None

        # prepare inputs
        x = x.contiguous()
        xPtr = ctypes.cast(x.data_ptr(), f32Ptr)
        qk_mask = qk_mask.contiguous()
        qkMaskPtr = ctypes.cast(qk_mask.data_ptr(), f32Ptr)
        cross_k_caches = cross_k_caches.contiguous()
        ckPtr = ctypes.cast(cross_k_caches.data_ptr(), f32Ptr)
        cross_v_caches = cross_v_caches.contiguous()
        cvPtr = ctypes.cast(cross_v_caches.data_ptr(), f32Ptr)

        # predict
        self.decoderObj.predictWith(self.mlmodel_handle,
                                    xPtr, qkMaskPtr, ckPtr, cvPtr,
                                    isNewCKV,
                                    self.outXPtr, self.outCHWPtr, self.outMKVPtr)
        if logPredictTime:
            print(f"\tcoreml decoder256 {timer()-startT:.3f}")

        totalDecoder256Time += timer()-startT
        return self.out_x, self.out_cross_head_weights, self.new_masked_kv_caches

    def closeModel(self):
        if self.mlmodel_handle != None:
            self.decoderObj.closeModel.argtypes = [c_void_p]
            self.decoderObj.closeModel.restypes = None
            self.decoderObj.closeModel(self.mlmodel_handle)
            self.decoderObj = None
            self.mlmodel_handle = None

########################################
class CoremlDecoder():
    def __init__(self, n_layer: int, n_state: int, n_head: int, n_vocab: int, bs: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.n_head = n_head
        self.n_vocab = n_vocab
        self.bs = bs
        self.modelName = modelName
        self.decoderObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        global totalLoadTime
        startT = timer()
        if self.mlmodel_handle == None:
            self.decoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/decoder.so')
            self.decoderObj.loadModel.argtypes = [c_char_p, c_int, c_int, c_int, c_int, c_int]
            self.decoderObj.loadModel.restype = c_void_p
            c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder.mlmodelc', 'ascii')
            bs = self.bs # beam_size
            n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
            n_state = self.n_state
            n_layer = self.n_layer
            n_vocab = self.n_vocab
            self.mlmodel_handle = self.decoderObj.loadModel(c_string, n_layer, n_state, n_head, n_vocab, bs)


            dtype1=torch.float32
            # prepare output buffers
            self.out_x = torch.ones((bs, 1, self.n_vocab), dtype=dtype1).contiguous()
            self.new_masked_kv_caches = torch.ones((n_layer * 2, bs, 1, n_state), dtype=dtype1).contiguous()
            self.outXPtr = ctypes.cast(self.out_x.data_ptr(), f32Ptr)
            self.outMKVPtr = ctypes.cast(self.new_masked_kv_caches.data_ptr(), f32Ptr)
        totalLoadTime += timer()-startT

    def rearrange_mkv(self, indices, text_offset):
        global totalDecoder1Time
        if self.mlmodel_handle == None:
            self.loadModel()
        #if logPredictTime:
        #    startT = timer()
        self.decoderObj.rearrange_mkv.argtypes = [POINTER(c_int), c_int]
        self.decoderObj.rearrange_mkv.restypes = None
        indices = indices.to(torch.int32).contiguous()
        indicesPtr = ctypes.cast(indices.data_ptr(), POINTER(c_int))

        # predict
        self.decoderObj.rearrange_mkv(indicesPtr,
                                      text_offset)
        #if logPredictTime:
        #    print(f"\tcoreml decoder1 rearrange_mkv {timer()-startT:.3f}")

    def predictWith(self, x, qk_mask, masked_kv_caches, cross_k_caches, cross_v_caches, text_offset, isNewCKV):
        global totalDecoder1Time
        if self.mlmodel_handle == None:
            self.loadModel()
        startT = timer()
        self.decoderObj.predictWith.argtypes = [c_void_p,
                                                f32Ptr, f32Ptr, f32Ptr, f32Ptr, f32Ptr,
                                                c_int, c_bool,
                                                f32Ptr, f32Ptr]
        self.decoderObj.predictWith.restypes = None

        # prepare inputs
        x = x.contiguous()
        xPtr = ctypes.cast(x.data_ptr(), f32Ptr)
        qk_mask = qk_mask.contiguous()
        qkMaskPtr = ctypes.cast(qk_mask.data_ptr(), f32Ptr)
        masked_kv_caches = masked_kv_caches.contiguous()
        mkvPtr = ctypes.cast(masked_kv_caches.data_ptr(), f32Ptr)
        cross_k_caches = cross_k_caches.contiguous()
        ckPtr = ctypes.cast(cross_k_caches.data_ptr(), f32Ptr)
        cross_v_caches = cross_v_caches.contiguous()
        cvPtr = ctypes.cast(cross_v_caches.data_ptr(), f32Ptr)

        # predict
        self.decoderObj.predictWith(self.mlmodel_handle,
                                    xPtr, qkMaskPtr, mkvPtr, ckPtr, cvPtr,
                                    text_offset, isNewCKV,
                                    self.outXPtr, self.outMKVPtr)
        if logPredictTime:
            print(f"\tcoreml decoder1 {timer()-startT:.3f}")

        totalDecoder1Time += timer() - startT
        return self.out_x, self.new_masked_kv_caches

    def closeModel(self):
        if self.mlmodel_handle != None:
            self.decoderObj.closeModel.argtypes = [c_void_p]
            self.decoderObj.closeModel.restypes = None
            self.decoderObj.closeModel(self.mlmodel_handle)
            self.decoderObj = None
            self.mlmodel_handle = None

########################################
class CoremlCrossKV():
    def __init__(self, n_layer: int, n_state: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.modelName = modelName
        self.crossKVObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        global totalLoadTime
        startT = timer()
        if self.mlmodel_handle == None:
            self.crossKVObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/crossKV.so')
            self.crossKVObj.loadModel.argtypes = [c_char_p, c_int, c_int]
            self.crossKVObj.loadModel.restype = c_void_p
            c_string = bytes(f'./coreml/{self.modelName}/CoremlCrossKV.mlmodelc', 'ascii')
            self.mlmodel_handle = self.crossKVObj.loadModel(c_string, self.n_layer, self.n_state)

            n_state = self.n_state
            n_layer = self.n_layer
            n_head = n_state//64

            dtype1=torch.float32
            # prepare output buffers
            self.out_cross_k_caches = torch.ones((n_layer, n_head, 64, 1500), dtype=dtype1).contiguous()
            self.outCKPtr = ctypes.cast(self.out_cross_k_caches.data_ptr(), f32Ptr)
            self.out_cross_v_caches = torch.ones((n_layer, n_head, 1500, 64), dtype=dtype1).contiguous()
            self.outCVPtr = ctypes.cast(self.out_cross_v_caches.data_ptr(), f32Ptr)
        totalLoadTime += timer()-startT

    def predictWith(self, xa):
        global totalCrossKVTime
        if self.mlmodel_handle == None:
            self.loadModel()
        startT = timer()
        self.crossKVObj.predictWith.argtypes = [c_void_p,
                                                f32Ptr,
                                                f32Ptr, f32Ptr]
        self.crossKVObj.predictWith.restypes = None

        # prepare inputs
        xa = xa.contiguous()
        xaPtr = ctypes.cast(xa.data_ptr(), f32Ptr)

        # predict
        self.crossKVObj.predictWith(self.mlmodel_handle,
                                    xaPtr,
                                    self.outCKPtr, self.outCVPtr)

        if logPredictTime:
            print(f"\tcoreml crossKV {timer()-startT:.3f}")
        totalCrossKVTime += timer()-startT
        return self.out_cross_k_caches, self.out_cross_v_caches


    def closeModel(self):
        if self.mlmodel_handle != None:
            self.crossKVObj.closeModel.argtypes = [c_void_p]
            self.crossKVObj.closeModel.restypes = None
            self.crossKVObj.closeModel(self.mlmodel_handle)
            self.crossKVObj = None
            self.mlmodel_handle = None

########################################

def showCoremlPredictTime():
    global totalLoadTime
    global totalEncoderTime
    global totalDecoder1Time
    global totalDecoder256Time
    global totalCrossKVTime
    print("--- coreml load -----------------")
    print(f"\ttotal load time    {totalLoadTime:.3f}s")
    print("--- coreml predict --------------")
    print(f"\ttotalEncoder       {totalEncoderTime:.3f}s")
    print(f"\ttotalCrossKV       {totalCrossKVTime:.3f}s")
    print(f"\ttotalDecoder256    {totalDecoder256Time:.3f}s")
    print(f"\ttotalDecoder1      {totalDecoder1Time:.3f}s")
    print(f"\t---")
    print(f"\ttotal predict time {totalEncoderTime+totalCrossKVTime+totalDecoder1Time+totalDecoder256Time:.3f}s")
    print("---------------------------------")

