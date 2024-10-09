"""
Copyright (c) 2023 by FlashInfer team.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import torch
import math
from enum import Enum
from typing import Optional, Tuple, Union, Dict


class PosEncodingMode(Enum):
    NONE = 0
    ROPE_LLAMA = 1
    ALIBI = 2


class MaskMode(Enum):
    NON_CAUSAL = 0
    CAUSAL = 1
    CUSTOM = 2


class TensorLayout(Enum):
    NHD = 0
    HND = 1


log2e = 1.44269504088896340736


def _expand_5d(x: torch.Tensor, kv_layout: str) -> torch.Tensor:
    if not x.ndim in [4, 5]:
        raise ValueError("x must be 4D or 5D")
    if x.ndim == 4:
        # page_size == 1
        if kv_layout == "NHD":
            # (num_pages, 2, num_heads, head_dim) -> (num_pages, 2, page_size=1, num_heads, head_dim)
            # expand to 5D on the 3nd last dimension
            return x.unsqueeze(-3)
        elif kv_layout == "HND":
            # (num_pages, 2, num_heads, head_dim) -> (num_pages, 2, num_heads, page_size=1, head_dim)
            # expand to 5D on the 2nd last dimension
            return x.unsqueeze(-2)
        else:
            raise KeyError("Invalid kv_layout {}".format(kv_layout))
    return x


def _expand_4d(x: torch.Tensor, kv_layout: str) -> torch.Tensor:
    if not x.ndim in [3, 4]:
        raise ValueError("x must be 3D or 4D")
    if x.ndim == 3:
        # page_size == 1
        if kv_layout == "NHD":
            # (num_pages, num_heads, head_dim) -> (num_pages, page_size=1, num_heads, head_dim)
            # expand to 4D on the 3nd last dimension
            return x.unsqueeze(-3)
        elif kv_layout == "HND":
            # (num_pages, num_heads, head_dim) -> (num_pages, num_heads, page_size=1, head_dim)
            # expand to 5D on the 2nd last dimension
            return x.unsqueeze(-2)
        else:
            raise KeyError("Invalid kv_layout {}".format(kv_layout))
    return x


def _check_pos_encoding_mode(pos_encoding_mode: str) -> None:
    if not hasattr(PosEncodingMode, pos_encoding_mode):
        raise KeyError("Invalid pos_encoding_mode {}".format(pos_encoding_mode))


def _check_kv_layout(kv_layout: str) -> None:
    if not hasattr(TensorLayout, kv_layout):
        raise KeyError("Invalide kv_layout {}".format(kv_layout))


def is_float8(x: torch.Tensor) -> bool:
    return x.dtype in [torch.float8_e4m3fn, torch.float8_e5m2]


def get_indptr(x: torch.Tensor) -> torch.Tensor:
    x = x.to(torch.int64)
    ret = torch.zeros(x.shape[0] + 1, dtype=x.dtype, device=x.device)
    ret[1:] = x.cumsum(0)
    return ret


def _unpack_paged_kv_cache(
    paged_kv_cache: Union[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]],
    kv_layout: str,
) -> Tuple[torch.Tensor, torch.Tensor]:
    if isinstance(paged_kv_cache, tuple):
        paged_k_cache, paged_v_cache = paged_kv_cache
        return (
            _expand_4d(paged_k_cache, kv_layout),
            _expand_4d(paged_v_cache, kv_layout),
        )
    elif torch.is_tensor(paged_kv_cache):
        # NOTE(Zihao): split on the second dimension
        paged_kv_cache = _expand_5d(paged_kv_cache, kv_layout)
        paged_k_cache, paged_v_cache = paged_kv_cache.unbind(dim=1)
        return paged_k_cache, paged_v_cache
    else:
        raise KeyError(
            "Unrecongized paged_kv_cache type {}, expect a single tensor or a tuple of tensor.".format(
                type(paged_kv_cache)
            )
        )


def get_alibi_slopes(n_heads: int) -> torch.Tensor:
    n = 2 ** math.floor(math.log2(n_heads))
    m_0 = 2.0 ** (-8.0 / n)
    m = torch.pow(m_0, torch.arange(1, 1 + n))
    if n < n_heads:
        m_hat_0 = 2.0 ** (-4.0 / n)
        m_hat = torch.pow(m_hat_0, torch.arange(1, 1 + 2 * (n_heads - n), 2))
        m = torch.cat([m, m_hat])
    return m.float()


_cache_buf: Dict[Tuple[str, torch.device], torch.Tensor] = {}


def _get_cache_buf(name: str, bytes: int, device: torch.device) -> torch.Tensor:
    key = (name, device)
    buf = _cache_buf.get(key)
    if buf is None:
        buf = torch.empty(bytes, dtype=torch.uint8, device=device)
        _cache_buf[key] = buf
    return buf


# find the least power of 2 that is greater than or equal to x
def _ceil_pow2(x: int) -> int:
    return 1 << (x - 1).bit_length()


def _get_range_buf(seq_len: int, device: torch.device) -> torch.Tensor:
    seq_len_pow2 = _ceil_pow2(seq_len)
    key = (f"range_{seq_len_pow2}", device)
    buf = _cache_buf.get(key)
    if buf is None:
        buf = torch.arange(seq_len_pow2, device=device, dtype=torch.int32)
        _cache_buf[key] = buf
    return buf[:seq_len]


def _get_cache_alibi_slopes_buf(
    num_qo_heads: int, device: torch.device
) -> torch.Tensor:
    key = (f"alibi_slopes_{num_qo_heads}", device)
    buf = _cache_buf.get(key)
    if buf is None:
        buf = (get_alibi_slopes(num_qo_heads) * log2e).to(device)
        _cache_buf[key] = buf
    return buf


def canonicalize_torch_dtype(dtype: Union[torch.dtype, str]) -> torch.dtype:
    if isinstance(dtype, str):
        return getattr(torch, dtype)
    elif isinstance(dtype, torch.dtype):
        return dtype
    else:
        raise TypeError(
            "dtype must be a string or torch.dtype, got {}".format(type(dtype))
        )


def get_compute_capability(device: torch.device) -> Tuple[int, int]:
    if device.type != "cuda":
        raise ValueError("device must be a cuda device")
    return torch.cuda.get_device_capability(device.index)
