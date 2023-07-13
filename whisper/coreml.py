from timeit import default_timer as timer
################################################
# Coreml Encoder part
from ctypes import cdll, c_int, c_float, c_char_p, c_void_p, c_bool, POINTER
import ctypes
import torch

class CoremlEncoder():
    def __init__(self, n_layer: int, n_state: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.modelName = modelName
        self.encoderObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        if self.mlmodel_handle == None:
            self.encoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/encoderWrapper.so')
            self.encoderObj.loadModel.argtypes = [c_char_p, c_int, c_int]
            self.encoderObj.loadModel.restype = None
            c_string = bytes(f'./coreml/{self.modelName}', 'ascii')
            self.encoderObj.loadModel(c_string, self.n_layer, self.n_state)

    def predictWith(self, melSegment):
        if self.mlmodel_handle == None:
            self.loadModel()
        self.encoderObj.predictWith.argtypes = [POINTER(c_float), POINTER(c_float)]
        self.encoderObj.predictWith.restypes = None

        # force memory continuous, this is very important
        melSegment = melSegment.contiguous()
        melSegmentDataPtr = ctypes.cast(melSegment.data_ptr(), POINTER(c_float))

        # alloc output buffer
        output_floats = torch.ones((1, 1500, self.n_state), dtype=torch.float32).contiguous()
        output_floats_ptr = ctypes.cast(output_floats.data_ptr(), POINTER(c_float))
        self.encoderObj.predictWith(melSegmentDataPtr, output_floats_ptr)
        return output_floats

    def closeModel(self):
        if self.mlmodel_handle != None:
            self.encoderObj.closeModel.argtypes = None
            self.encoderObj.closeModel.restypes = None
            self.encoderObj.closeModel()

    def getModelSize(self, n_state: int):
        if n_state == 384:
            return "tiny"
        elif n_state == 512:
            return "base"
        elif n_state == 768:
            return "small"
        elif n_state == 1024:
            return "medium"
        elif n_state == 1280:
            return "large"
        else:
            return "unknown_model_size"
########################################
class CoremlDecoder256():
    def __init__(self, n_layer: int, n_state: int, n_head: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.n_head = n_head
        self.modelName = modelName
        self.decoderObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        if self.mlmodel_handle == None:
            self.decoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/decoder256Wrapper.so')
            self.decoderObj.loadModel.argtypes = [c_char_p, c_int, c_int, c_int]
            self.decoderObj.loadModel.restype = c_void_p
            c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder256.mlmodelc', 'ascii')
            self.mlmodel_handle = self.decoderObj.loadModel(c_string, self.n_layer, self.n_state, self.n_head)

            bs = 1 # beam_size
            n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
            n_state = self.n_state
            n_layer = self.n_layer
            max_n_ctx = 256

            dtype1=torch.float32
            # prepare output buffers
            self.out_x = torch.ones((bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
            self.out_cross_qks = torch.ones((n_layer * bs, n_head, max_n_ctx, 1500), dtype=dtype1).contiguous()
            self.new_masked_kv_caches = torch.ones((n_layer * 2, bs, max_n_ctx, n_state), dtype=dtype1).contiguous()
            self.new_cross_kv_caches = torch.ones((n_layer * 2, 1, 1500, n_state), dtype=dtype1).contiguous()
            self.outXPtr = ctypes.cast(self.out_x.data_ptr(), POINTER(c_float))
            self.outCQKPtr = ctypes.cast(self.out_cross_qks.data_ptr(), POINTER(c_float))
            self.outMKVPtr = ctypes.cast(self.new_masked_kv_caches.data_ptr(), POINTER(c_float))
            self.outCKVPtr = ctypes.cast(self.new_cross_kv_caches.data_ptr(), POINTER(c_float))

    def predictWith(self, x, xa, qk_mask):
        if self.mlmodel_handle == None:
            self.loadModel()
        self.decoderObj.predictWith.argtypes = [c_void_p,
                                                POINTER(c_float), POINTER(c_float), POINTER(c_float),
                                                c_int, c_int, c_int,
                                                POINTER(c_float), POINTER(c_float), POINTER(c_float), POINTER(c_float)]
        self.decoderObj.predictWith.restypes = None

        # prepare inputs
        x = x.contiguous()
        xPtr = ctypes.cast(x.data_ptr(), POINTER(c_float))
        xa = xa.contiguous()
        xaPtr = ctypes.cast(xa.data_ptr(), POINTER(c_float))
        qk_mask = qk_mask.contiguous()
        qkMaskPtr = ctypes.cast(qk_mask.data_ptr(), POINTER(c_float))

        # predict
        #startT = timer()
        self.decoderObj.predictWith(self.mlmodel_handle,
                                    xPtr, xaPtr, qkMaskPtr,
                                    self.n_layer, self.n_state, self.n_head,
                                    self.outXPtr, self.outCQKPtr, self.outMKVPtr, self.outCKVPtr)
        #print(f"\tpredictWit took {timer() - startT:.3f}")

        return self.out_x, self.out_cross_qks, self.new_masked_kv_caches, self.new_cross_kv_caches

    def closeModel(self):
        if self.mlmodel_handle != None:
            self.decoderObj.closeModel.argtypes = [c_void_p]
            self.decoderObj.closeModel.restypes = None
            self.decoderObj.closeModel(self.mlmodel_handle)

########################################
class CoremlDecoder():
    def __init__(self, n_layer: int, n_state: int, n_head: int, n_vocab: int, modelName):
        self.n_layer = n_layer
        self.n_state = n_state
        self.n_head = n_head
        self.n_vocab = n_vocab
        self.modelName = modelName
        self.decoderObj = None
        self.mlmodel_handle = None

    def loadModel(self):
        if self.mlmodel_handle == None:
            self.decoderObj = cdll.LoadLibrary(f'./coreml/{self.modelName}/decoderWrapper.so')
            self.decoderObj.loadModel.argtypes = [c_char_p, c_int, c_int, c_int, c_int]
            self.decoderObj.loadModel.restype = c_void_p
            c_string = bytes(f'./coreml/{self.modelName}/CoremlDecoder.mlmodelc', 'ascii')
            self.mlmodel_handle = self.decoderObj.loadModel(c_string, self.n_layer, self.n_state, self.n_head, self.n_vocab)

            bs = 5 # beam_size
            n_head = self.n_head # tiny=6, base=8, small=12, medium=16, large=20
            n_state = self.n_state
            n_layer = self.n_layer

            dtype1=torch.float32
            # prepare output buffers
            self.out_x = torch.ones((bs, 1, self.n_vocab), dtype=dtype1).contiguous()
            self.new_masked_kv_caches = torch.ones((n_layer * 2, bs, 1, n_state), dtype=dtype1).contiguous()
            self.outXPtr = ctypes.cast(self.out_x.data_ptr(), POINTER(c_float))
            self.outMKVPtr = ctypes.cast(self.new_masked_kv_caches.data_ptr(), POINTER(c_float))

    def predictWith(self, x, xa, qk_mask, masked_kv_caches, cross_kv_caches, isNewCKV):
        if self.mlmodel_handle == None:
            self.loadModel()
        self.decoderObj.predictWith.argtypes = [c_void_p,
                                                POINTER(c_float), POINTER(c_float), POINTER(c_float), POINTER(c_float), POINTER(c_float),
                                                c_int, c_int, c_int, c_int, c_bool,
                                                POINTER(c_float), POINTER(c_float)]
        self.decoderObj.predictWith.restypes = None

        # prepare inputs
        x = x.contiguous()
        xPtr = ctypes.cast(x.data_ptr(), POINTER(c_float))
        xa = xa.contiguous()
        xaPtr = ctypes.cast(xa.data_ptr(), POINTER(c_float))
        qk_mask = qk_mask.contiguous()
        qkMaskPtr = ctypes.cast(qk_mask.data_ptr(), POINTER(c_float))
        masked_kv_caches = masked_kv_caches.contiguous()
        mkvPtr = ctypes.cast(masked_kv_caches.data_ptr(), POINTER(c_float))
        cross_kv_caches = cross_kv_caches.contiguous()
        ckvPtr = ctypes.cast(cross_kv_caches.data_ptr(), POINTER(c_float))

        # predict
        startT = timer()
        self.decoderObj.predictWith(self.mlmodel_handle,
                                    xPtr, xaPtr, qkMaskPtr, mkvPtr, ckvPtr,
                                    self.n_layer, self.n_state, self.n_head, self.n_vocab, isNewCKV,
                                    self.outXPtr, self.outMKVPtr)
        #print(f"\tpredictWit took {timer() - startT:.3f}")

        return self.out_x, self.new_masked_kv_caches

    def closeModel(self):
        if self.mlmodel_handle != None:
            self.decoderObj.closeModel.argtypes = [c_void_p]
            self.decoderObj.closeModel.restypes = None
            self.decoderObj.closeModel(self.mlmodel_handle)

########################################
