from typing import Dict, Iterable, Optional

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor, nn

from .coreml import CoremlEncoder
from timeit import default_timer as timer

def sinusoids(length, channels, max_timescale=10000):
    """Returns sinusoids for positional embedding"""
    assert channels % 2 == 0
    log_timescale_increment = np.log(max_timescale) / (channels // 2 - 1)
    inv_timescales = torch.exp(-log_timescale_increment * torch.arange(channels // 2))
    scaled_time = torch.arange(length)[:, np.newaxis] * inv_timescales[np.newaxis, :]
    return torch.cat([torch.sin(scaled_time), torch.cos(scaled_time)], dim=1)

# large encoder conversion time 48mins -> 18mins
# magic from https://github.com/apple/coremltools/issues/1900
def speedup_conversion_workaround(x: Tensor, n_state: int):
    return torch.cat([x, torch.empty(1, 1, n_state)], dim=1).split(1500, dim=1)[0]

class MultiHeadAttention(nn.Module):
    def __init__(self, n_state: int, n_head: int):
        super().__init__()
        self.n_head = n_head
        self.dim_per_head = (n_state// n_head)
        self.qk_scale = (self.dim_per_head) ** -0.5

        self.query = nn.Linear(n_state, n_state)
        self.key = nn.Linear(n_state, n_state, bias=False)
        self.value = nn.Linear(n_state, n_state)
        self.out = nn.Linear(n_state, n_state)

    def forward(self, x: Tensor):
        q = self.query(x)
        k = self.key(x)
        v = self.value(x)

        #--- magic from https://github.com/apple/ml-ane-transformers/blob/da64000fa56cc85b0859bc17cb16a3d753b8304a/ane_transformers/huggingface/distilbert.py#L151
        # (1, 1500, 384) -> (1, 384, 1, 1500)
        q = q.transpose(1, 2).unsqueeze(2)
        k = k.unsqueeze(2)
        v = v.transpose(1, 2).unsqueeze(2)

        mh_q = q.split(self.dim_per_head, dim=1)
        mh_k = k.split(self.dim_per_head, dim=3)
        mh_v = v.split(self.dim_per_head, dim=1)

        attn_weights = [
            torch.einsum('bchq,bkhc->bkhq', [qi, ki]) * self.qk_scale
            for qi, ki in zip(mh_q, mh_k)
        ]

        attn_weights = [aw.softmax(dim=1) for aw in attn_weights]
        attn = [
            torch.einsum('bkhq,bchk->bchq', wi, vi)
            for wi, vi in zip(attn_weights, mh_v)
        ]

        attn = torch.cat(attn, dim=1)

        # (1, 384, 1, 1500) -> (1, 1500, 384)
        attn = attn.squeeze(2).transpose(1,2)
        #--- end of magic

        attn = self.out(attn)

        return attn

class ResidualAttentionBlock(nn.Module):
    def __init__(self, n_state: int, n_head: int, cross_attention: bool = False):
        super().__init__()

        self.attn = MultiHeadAttention(n_state, n_head)
        self.attn_ln = nn.LayerNorm(n_state)

        n_mlp = n_state * 4
        self.mlp = nn.Sequential(
            nn.Linear(n_state, n_mlp), nn.GELU(), nn.Linear(n_mlp, n_state)
        )
        self.mlp_ln = nn.LayerNorm(n_state)
        self.n_state = n_state

    def forward(self, x: Tensor):
        x = speedup_conversion_workaround(x, self.n_state)
        x = x + self.attn(self.attn_ln(x))
        x = speedup_conversion_workaround(x, self.n_state)
        x = x + self.mlp(self.mlp_ln(x))
        return x

class AudioEncoder(nn.Module):
    def __init__(
        self, n_mels: int, n_ctx: int, n_state: int, n_head: int, n_layer: int
    ):
        super().__init__()
        self.conv1 = nn.Conv1d(n_mels, n_state, kernel_size=3, padding=1)
        self.conv2 = nn.Conv1d(n_state, n_state, kernel_size=3, stride=2, padding=1)
        self.register_buffer("positional_embedding", sinusoids(n_ctx, n_state))

        self.blocks: Iterable[ResidualAttentionBlock] = nn.ModuleList(
            [ResidualAttentionBlock(n_state, n_head) for _ in range(n_layer)]
        )
        self.ln_post = nn.LayerNorm(n_state)
        self.coremlEncoder = None
        self.n_state = n_state

    def forward(self, x: Tensor):
        """
        x : torch.Tensor, shape = (batch_size, n_mels, n_ctx)
            the mel spectrogram of the audio
        """
        ############################
        #if self.coremlEncoder == None:
        #    self.coremlEncoder = CoremlEncoder(self.n_state)
        #return self.coremlEncoder.predictWith(x)
        ###########################3

        x = F.gelu(self.conv1(x))
        x = F.gelu(self.conv2(x))
        x = x.permute(0, 2, 1)

        assert x.shape[1:] == self.positional_embedding.shape, "incorrect audio shape"
        x = (x + self.positional_embedding)

        for block in self.blocks:
            x = block(x)
        x = self.ln_post(x)

        return x
