from timeit import default_timer as timer
################################################
# Coreml Encoder part
from ctypes import cdll, c_int, c_float, c_char_p, c_bool, POINTER
import ctypes
import torch

f32Ptr = POINTER(c_float)
logPredictTime = False

totalLoadTime = 0
totalEncoderTime = 0
totalDecoder1Time = 0
totalDecoder256Time = 0
totalCrossKVTime = 0

class Coreml():
    def __init__(self, n_layer: int, n_state: int, n_head: int, n_vocab: int, modelName):
        self.obj = cdll.LoadLibrary(f'./coreml/{modelName}/coreml.so')
        self.n_layer = n_layer
        self.n_state = n_state
        self.n_head = n_head
        self.n_alignment_head = -1 # for decoder256
        self.bs = -1 # for decoder 1
        self.n_vocab = n_vocab
        self.modelName = modelName
        self.isEncoderLoaded = False
        self.isCrossKVLoaded = False
        self.isDecoder1Loaded = False
        self.isDecoder256Loaded = False

### Encoder #####################################
    def loadEncoder(self):
        global totalLoadTime
        if self.isEncoderLoaded:
            return
        startT = timer()
        self.obj.loadEncoder.argtypes = [c_char_p, c_int, c_int]
        self.obj.loadEncoder.restype = None
        c_string = bytes(f'./coreml/{self.modelName}', 'ascii')
        self.obj.loadEncoder(c_string, self.n_layer, self.n_state)
        self.isEncoderLoaded = True
        # alloc output buffer
        self.output_floats = torch.ones((1, 1500, self.n_state), dtype=torch.float32).contiguous()
        self.output_floats_ptr = ctypes.cast(self.output_floats.data_ptr(), f32Ptr)
        totalLoadTime += timer()-startT

    def encoderPredict(self, melSegment):
        global totalEncoderTime
        if not self.isEncoderLoaded:
            print("⛑️")
            return
        startT = timer()
        self.obj.encoderPredict.argtypes = [f32Ptr, f32Ptr]
        self.obj.encoderPredict.restypes = None

        # force memory continuous, this is very important
        melSegment = melSegment.contiguous()
        melSegmentDataPtr = ctypes.cast(melSegment.data_ptr(), f32Ptr)

        self.obj.encoderPredict(melSegmentDataPtr, self.output_floats_ptr)
        if logPredictTime:
            print(f"\tcoreml encoder {timer()-startT:.3f}")
        totalEncoderTime += timer() - startT
        return self.output_floats

    def closeEncoder(self):
        self.obj.closeEncoder.argtypes = None
        self.obj.closeEncoder.restypes = None
        self.obj.closeEncoder()

### CrossKV #####################################
    def loadCrossKV(self):
        global totalLoadTime
        if self.isCrossKVLoaded:
            return
        startT = timer()
        self.obj.loadCrossKV.argtypes = [c_char_p, c_int, c_int]
        self.obj.loadCrossKV.restype = None
        c_string = bytes(f'./coreml/{self.modelName}/CoremlCrossKV.mlmodelc', 'ascii')
        self.obj.loadCrossKV(c_string, self.n_layer, self.n_state)

        n_state = self.n_state
        n_layer = self.n_layer
        n_head = n_state//64

        dtype1=torch.float32
        # prepare output buffers
        self.out_cross_k_caches = torch.ones((n_layer, n_head, 64, 1500), dtype=dtype1).contiguous()
        self.outCKPtr = ctypes.cast(self.out_cross_k_caches.data_ptr(), f32Ptr)
        self.out_cross_v_caches = torch.ones((n_layer, n_head, 1500, 64), dtype=dtype1).contiguous()
        self.outCVPtr = ctypes.cast(self.out_cross_v_caches.data_ptr(), f32Ptr)
        self.isCrossKVLoaded = True
        totalLoadTime += timer()-startT

    def crossKVPredict(self, xa):
        global totalCrossKVTime
        if not self.isCrossKVLoaded:
            print("⛑️")
            return
        startT = timer()
        self.obj.crossKVPredict.argtypes = [f32Ptr,
                                            f32Ptr, f32Ptr]
        self.obj.crossKVPredict.restypes = None

        xa = xa.contiguous()
        xaPtr = ctypes.cast(xa.data_ptr(), f32Ptr)

        self.obj.crossKVPredict(xaPtr,
                                self.outCKPtr, self.outCVPtr)

        if logPredictTime:
            print(f"\tcoreml crossKV {timer()-startT:.3f}")
        totalCrossKVTime += timer()-startT
        return self.out_cross_k_caches, self.out_cross_v_caches

    def closeCrossKV(self):
        self.obj.closeCrossKV.argtypes = None
        self.obj.closeCrossKV.restypes = None
        self.obj.closeCrossKV()

### Decoder256 #####################################
    def loadDecoder256(self):
        global totalLoadTime
        if self.isDecoder256Loaded:
            return
        startT = timer()

        self.obj.loadDecoder256.argtypes = [c_char_p, c_int, c_int, c_int, c_int]
        self.obj.loadDecoder256.restype = None
        c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder256.mlmodelc', 'ascii')
        self.obj.loadDecoder256(c_string, self.n_layer, self.n_state, self.n_head, self.n_alignment_head)

        bs = 1 # beam_size
        n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
        n_state = self.n_state
        n_layer = self.n_layer
        n_alignment_head = self.n_alignment_head
        max_n_ctx = 256

        dtype1=torch.float32
        # prepare output buffers
        self.out_x256 = torch.ones((bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
        self.out_cross_head_weights256 = torch.ones((n_alignment_head, max_n_ctx, 1500), dtype=dtype1).contiguous()
        self.new_masked_kv_caches256 = torch.ones((n_layer * 2, bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
        self.outXPtr256 = ctypes.cast(self.out_x256.data_ptr(), f32Ptr)
        self.outCHWPtr256 = ctypes.cast(self.out_cross_head_weights256.data_ptr(), f32Ptr)
        self.outMKVPtr256 = ctypes.cast(self.new_masked_kv_caches256.data_ptr(), f32Ptr)
        self.isDecoder256Loaded = True

        totalLoadTime += timer()-startT

    def decoder256Predict(self, x, qk_mask, cross_k_caches, cross_v_caches, isNewCKV):
        global totalDecoder256Time
        if not self.isDecoder256Loaded:
            print("⛑️")
            return
        startT = timer()
        self.obj.decoder256Predict.argtypes = [f32Ptr, f32Ptr, f32Ptr, f32Ptr,
                                               c_bool,
                                               f32Ptr, f32Ptr, f32Ptr]
        self.obj.decoder256Predict.restypes = None

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
        self.obj.decoder256Predict(xPtr, qkMaskPtr, ckPtr, cvPtr,
                                   isNewCKV,
                                   self.outXPtr256, self.outCHWPtr256, self.outMKVPtr256)
        if logPredictTime:
            print(f"\tcoreml decoder256 {timer()-startT:.3f}")

        totalDecoder256Time += timer()-startT
        return self.out_x256, self.out_cross_head_weights256, self.new_masked_kv_caches256

    def closeDecoder256(self):
        self.obj.closeDecoder256.argtypes = None
        self.obj.closeDecoder256.restypes = None
        self.obj.closeDecoder256()

### Decoder1 #####################################
    def loadDecoder1(self):
        global totalLoadTime
        if self.isDecoder1Loaded:
            return
        startT = timer()
        self.obj.loadDecoder1.argtypes = [c_char_p, c_int, c_int, c_int, c_int, c_int]
        self.obj.loadDecoder1.restype = None
        c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder.mlmodelc', 'ascii')
        bs = self.bs # beam_size
        n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
        n_state = self.n_state
        n_layer = self.n_layer
        n_vocab = self.n_vocab
        self.obj.loadDecoder1(c_string, n_layer, n_state, n_head, n_vocab, bs)

        dtype1=torch.float32
        # prepare output buffers
        self.out_x1 = torch.ones((bs, 1, self.n_vocab), dtype=dtype1).contiguous()
        self.new_masked_kv_caches1 = torch.ones((n_layer * 2, bs, 1, n_state), dtype=dtype1).contiguous()
        self.outXPtr1 = ctypes.cast(self.out_x1.data_ptr(), f32Ptr)
        self.outMKVPtr1 = ctypes.cast(self.new_masked_kv_caches1.data_ptr(), f32Ptr)
        self.isDecoder1Loaded = True
        totalLoadTime += timer()-startT

    def rearrange_mkv(self, indices, text_offset):
        global totalDecoder1Time
        #if logPredictTime:
        #    startT = timer()
        self.obj.rearrange_mkv.argtypes = [POINTER(c_int), c_int]
        self.obj.rearrange_mkv.restypes = None
        indices = indices.to(torch.int32).contiguous()
        indicesPtr = ctypes.cast(indices.data_ptr(), POINTER(c_int))

        # predict
        self.obj.rearrange_mkv(indicesPtr,
                               text_offset)
        #if logPredictTime:
        #    print(f"\tcoreml decoder1 rearrange_mkv {timer()-startT:.3f}")

    def decoder1Predict(self, x, qk_mask, masked_kv_caches, cross_k_caches, cross_v_caches, text_offset, isNewCKV):
        global totalDecoder1Time
        if not self.isDecoder1Loaded:
            print("⛑️")
            return
        startT = timer()
        self.obj.decoder1Predict.argtypes = [f32Ptr, f32Ptr, f32Ptr, f32Ptr, f32Ptr,
                                             c_int, c_bool,
                                             f32Ptr, f32Ptr]
        self.obj.decoder1Predict.restypes = None

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
        self.obj.decoder1Predict(xPtr, qkMaskPtr, mkvPtr, ckPtr, cvPtr,
                                 text_offset, isNewCKV,
                                 self.outXPtr1, self.outMKVPtr1)
        if logPredictTime:
            print(f"\tcoreml decoder1 {timer()-startT:.3f}")

        totalDecoder1Time += timer() - startT
        return self.out_x1, self.new_masked_kv_caches1

    def closeDecoder1(self):
        self.obj.closeDecoder1.argtypes = None
        self.obj.closeDecoder1.restypes = None
        self.obj.closeDecoder1()


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

