import base64
import gzip
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .decoding import decode as decode_function
from .decoding import detect_language as detect_language_function
from .transcribe import transcribe as transcribe_function
from .coreml import CoremlDecoder, CoremlDecoder256
from timeit import default_timer as timer

class MultiHeadAttention(nn.Module):
    def __init__(self, n_state: int, n_head: int):
        super().__init__()
        self.n_head = n_head
        self.dim_per_head = n_state // n_head
        self.query = nn.Linear(n_state, n_state)
        self.key = nn.Linear(n_state, n_state, bias=False)
        self.value = nn.Linear(n_state, n_state)
        self.out = nn.Linear(n_state, n_state)
        self.qk_scale = (n_state // n_head) ** -0.5

    def forward(
        self,
        x: Tensor,
        qk_mask: Tensor,
        cache_k: Optional[Tensor] = None,
        cache_v: Optional[Tensor] = None,
    ):
        # x_shape
        # decoder1:   (5,    1, 384)
        # decoder256: (1,  256, 384), force bs=1 for speedup conversion

        q = self.query(x) * self.qk_scale
        k = self.key(x)
        v = self.value(x)

        new_k = k
        new_v = v

        if cache_k is not None:
            k = torch.cat([cache_k, k], dim=1)
            v = torch.cat([cache_v, v], dim=1)

        q = q.view(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
        k = k.view(*k.shape[:2], self.n_head, -1).permute(0, 2, 3, 1)
        v = v.view(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)

        qk = q @ k + qk_mask

        w = qk.softmax(dim=-1).to(q.dtype)
        wv = (w @ v).permute(0, 2, 1, 3).flatten(start_dim=2)

        return self.out(wv), new_k, new_v

class CrossMultiHeadAttention(MultiHeadAttention):
    def forward(
        self,
        x: Tensor,
        cache_k: Tensor,
        cache_v: Tensor,
    ):
        q = self.query(x) * self.qk_scale
        k = cache_k
        v = cache_v

        q = q.view(*q.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)
        k = k.view(*k.shape[:2], self.n_head, -1).permute(0, 2, 3, 1)
        v = v.view(*v.shape[:2], self.n_head, -1).permute(0, 2, 1, 3)

        qk = q @ k

        w = qk.softmax(dim=-1).to(q.dtype)
        wv = (w @ v).permute(0, 2, 1, 3).flatten(start_dim=2)

        return self.out(wv), qk

class ResidualAttentionBlock(nn.Module):
    def __init__(self, n_state: int, n_head: int, cross_attention: bool = False):
        super().__init__()

        self.n_state = n_state
        self.attn = MultiHeadAttention(n_state, n_head)
        self.attn_ln = nn.LayerNorm(n_state)

        self.cross_attn = (
            CrossMultiHeadAttention(n_state, n_head) if cross_attention else None
        )
        self.cross_attn_ln = nn.LayerNorm(n_state) if cross_attention else None

        n_mlp = n_state * 4
        self.mlp = nn.Sequential(
            nn.Linear(n_state, n_mlp), nn.GELU(), nn.Linear(n_mlp, n_state)
        )
        self.mlp_ln = nn.LayerNorm(n_state)

    def forward(
        self,
        x: Tensor,
        qk_mask: Optional[Tensor] = None,
        mk: Optional[Tensor] = None,
        mv: Optional[Tensor] = None,
        ck: Optional[Tensor] = None,
        cv: Optional[Tensor] = None,
    ):
        x_out, new_mk, new_mv = self.attn(self.attn_ln(x), qk_mask=qk_mask, cache_k=mk, cache_v=mv)
        x = x + x_out
        cross_qk = new_ck = new_cv = None
        if self.cross_attn:
            x_out, cross_qk = self.cross_attn(self.cross_attn_ln(x), cache_k=ck, cache_v=cv)
            x = x + x_out
        x = x + self.mlp(self.mlp_ln(x))
        return x, cross_qk, new_mk, new_mv

class TextDecoder(nn.Module):
    def __init__(
            self, n_vocab: int, n_ctx: int, n_state: int, n_head: int, n_layer: int, use_coreml: bool, modelName
    ):
        super().__init__()

        self.token_embedding = nn.Embedding(n_vocab, n_state)
        self.positional_embedding = nn.Parameter(torch.empty(n_ctx, n_state))

        self.blocks: Iterable[ResidualAttentionBlock] = nn.ModuleList(
            [
                ResidualAttentionBlock(n_state, n_head, cross_attention=True)
                for _ in range(n_layer)
            ]
        )
        self.ln = nn.LayerNorm(n_state)
        self.n_vocab = n_vocab
        self.n_state = n_state
        self.n_layer = n_layer
        self.n_head = n_head
        mask = torch.empty(n_ctx, n_ctx).fill_(-np.inf).triu_(1)
        self.register_buffer("mask", mask, persistent=False)
        self.coremlDecoder = None
        self.coremlDecoder256 = None
        self.use_coreml = use_coreml
        self.modelName = modelName

        # max token len for first time = max_prefix_len(224) + sot_len(3)
        # not sure why... decoder227 is slower than decoder256
        self.max_n_ctx_for_1st = 256

    def crossKVCaches(self, xa: Tensor):
        cross_kv_caches = []
        for block in self.blocks:
            cross_kv_caches.append(block.cross_attn.key(xa).unsqueeze(0))
            cross_kv_caches.append(block.cross_attn.value(xa).unsqueeze(0))
        return torch.cat(cross_kv_caches, dim=0)

    def forward(self, x: Tensor, xa: Tensor,
                text_offset: Tensor,
                isNewCKV: Tensor,
                masked_kv_caches: Optional[Tensor] = None,
                cross_kv_caches: Optional[Tensor] = None):
        """
        x : torch.LongTensor, shape = (batch_size, <= n_ctx)
            the text tokens
        xa : torch.Tensor, shape = (batch_size, n_mels, n_audio_ctx)
            the encoded audio features to be attended on
        """
        offset = text_offset
        n_batch, n_ctx = x.shape

        x = self.token_embedding(x) + self.positional_embedding[offset : offset + n_ctx]
        x = x.to(xa.dtype)

        if text_offset == 0: # decoder256
            new_cross_kv_caches = self.crossKVCaches(xa) if cross_kv_caches is None else cross_kv_caches
            max_n_ctx = self.max_n_ctx_for_1st
            qk_mask = (torch.ones(max_n_ctx, max_n_ctx) * -np.inf).triu_(1)
            qk_mask[:, n_ctx:] = -np.inf
            ## fix shape by appending zeros to max_n_ctx
            x = torch.cat([x, torch.zeros(n_batch, max_n_ctx-n_ctx, self.n_state)], dim=1)

            # predict beam by beam for reuse decoder256 coreml model for bs=1 and bs=5
            x_bs = x.split(1)

            for bs_idx in range(len(x_bs)):
                # cross_qk only used for word level timestamp, its bs=1
                # cross_kv_caches is the same in all beams
                # TODO: this calculate redundant cross_kv_caches 5 times,
                #       should move that calculating to encoder or independent sub-model
                _x, cross_qks, _new_masked_kv_caches = self.forwardBlocks(x_bs[bs_idx],
                                                                          qk_mask,
                                                                          masked_kv_caches,
                                                                          new_cross_kv_caches,
                                                                          isNewCKV=(bs_idx==0))
                if bs_idx == 0:
                    x = _x
                    new_masked_kv_caches = _new_masked_kv_caches
                else:
                    x = torch.cat([x, _x], dim=0)
                    new_masked_kv_caches = torch.cat([new_masked_kv_caches, _new_masked_kv_caches], dim=1)

            x = x.split(n_ctx, dim=1)[0]
            cross_qks = cross_qks.split(n_ctx, dim=2)[0]
            logits = (
                x @ torch.transpose(self.token_embedding.weight.to(x.dtype), 0, 1)
            ).float()
        else: # decoder1
            qk_mask = torch.cat([torch.zeros((1,text_offset)),
                                 torch.ones((1, 448-text_offset)) * -np.inf,
                                 torch.FloatTensor([[0]])],
                                 dim=1)
            logits, new_masked_kv_caches = self.forwardBlocks(x,
                                                              qk_mask,
                                                              masked_kv_caches,
                                                              cross_kv_caches,
                                                              isNewCKV)
            cross_qks = None
            new_cross_kv_caches = None

        return logits, cross_qks, new_masked_kv_caches, new_cross_kv_caches

    def forwardBlocks(self,
                      x: Tensor,
                      qk_mask: Optional[Tensor] = None,
                      masked_kv_caches: Optional[Tensor] = None,
                      cross_kv_caches: Optional[Tensor] = None,
                      isNewCKV: Optional[Tensor] = None, # only for coremlDecoder1
                      ):

        ############################
        # Coreml Decoder part
        if self.use_coreml:
            if masked_kv_caches is not None and x.shape[1] == 1:
                if self.coremlDecoder == None:
                    self.coremlDecoder = CoremlDecoder(self.n_layer, self.n_state, self.n_head, self.n_vocab, self.modelName)
                return self.coremlDecoder.predictWith(x, qk_mask, masked_kv_caches, cross_kv_caches, isNewCKV)

            else:
                if self.coremlDecoder256 == None:
                    self.coremlDecoder256 = CoremlDecoder256(self.n_layer, self.n_state, self.n_head, self.modelName)
                return self.coremlDecoder256.predictWith(x, qk_mask, cross_kv_caches, isNewCKV)
        ############################

        cross_qks = []
        new_masked_kv_caches = []

        if masked_kv_caches is not None:
            mkv_caches = masked_kv_caches.split(16) # 8 block * 2 = 16 (k,v)
        if cross_kv_caches is not None:
            #print("cross_kv_caches", cross_kv_caches.shape)
            ckv_caches = cross_kv_caches.split(16)

        layer_offset = 0
        for block8_idx in range(len(ckv_caches)):
            if masked_kv_caches is not None:
                block8_mkv_caches = mkv_caches[block8_idx].split(1)
                #print("block8_mkv_caches", len(block8_mkv_caches))
            if cross_kv_caches is not None:
                block8_ckv_caches = ckv_caches[block8_idx].split(1)
                #print("block8_ckv_caches", len(block8_ckv_caches))

            for layer_idx in range(layer_offset, min(layer_offset+8, self.n_layer)):
                #if layer_idx >= 16:
                #    break
                block = self.blocks[layer_idx]
                # mk = masked_key_cache, ck = cross_key_cache
                mk = mv = ck = cv = None
                if masked_kv_caches is not None:
                    mk = block8_mkv_caches[(layer_idx*2)%16].squeeze(0)
                    mv = block8_mkv_caches[(layer_idx*2 + 1)%16].squeeze(0)

                if cross_kv_caches is not None:
                    ck = block8_ckv_caches[(layer_idx*2)%16].squeeze(0)
                    cv = block8_ckv_caches[(layer_idx*2 + 1)%16].squeeze(0)

                x, cross_qk, new_mk, new_mv= block(x, qk_mask, mk, mv, ck, cv)

                cross_qks.append(cross_qk)
                new_masked_kv_caches.append(new_mk)
                new_masked_kv_caches.append(new_mv)
            layer_offset += 8

        cross_qks = torch.cat(cross_qks)
        new_masked_kv_caches = torch.stack(new_masked_kv_caches)

        x = self.ln(x)

        if qk_mask.shape[0] == 1: # decoder1
            splits = self.token_embedding.weight.split(self.n_vocab//5, dim=0)
            x = x.view(*x.shape[:2], self.n_state)
            logits = torch.cat([ x @ split.transpose(0,1) for split in splits], dim=2)

            return logits, new_masked_kv_caches
        else: # decoder256 and decoder call from add timestamp
            return x, cross_qks, new_masked_kv_caches
