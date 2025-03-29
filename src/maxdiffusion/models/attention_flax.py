# Copyright 2023 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import math

import flax.linen as nn
import jax
import jax.numpy as jnp
from jax.experimental import shard_map
from jax.experimental.pallas.ops.tpu.splash_attention import splash_attention_mask
from jax.experimental.pallas.ops.tpu.splash_attention import splash_attention_kernel
from einops import rearrange
from .. import common_types, max_logging

from . import quantizations


Array = common_types.Array
Mesh = common_types.Mesh
DType = common_types.DType
BlockSizes = common_types.BlockSizes


AxisNames = common_types.AxisNames
BATCH = common_types.BATCH
LENGTH = common_types.LENGTH
HEAD = common_types.HEAD
D_KV = common_types.D_KV
EMBED = common_types.EMBED
Quant = quantizations.AqtQuantization


Quant = quantizations.AqtQuantization
import numpy as np
DEFAULT_MASK_VALUE = -0.7 * float(np.finfo(np.dtype("float32")).max)

def _maybe_aqt_einsum(quant: Quant):
  return jnp.einsum if quant is None else quant.einsum()


class AttentionOp(nn.Module):
  mesh: Mesh
  attention_kernel: str
  scale: int
  heads: int
  dim_head: int
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  float32_qk_product: bool = True
  flash_axis_names: AxisNames = (BATCH, HEAD, LENGTH, D_KV)
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  dtype: DType = jnp.float32
  quant: Quant = None

  def setup(self):
    if self.attention_kernel == "cudnn_flash_te":
      from transformer_engine.jax.flax.transformer import DotProductAttention  # pytype: disable=import-error

      self.dpa_layer = DotProductAttention(
          head_dim=self.dim_head,
          num_attention_heads=self.heads,
          num_gqa_groups=self.heads,
          attn_mask_type="no_mask",  # 'no_mask', 'padding', 'causal', or 'padding_causal'
          attn_bias_type="NO_BIAS",  # 'no_bias', 'pre_scale_bias' or 'post_scale_bias'
          # attention_dropout=self.dropout_rate,
          dropout_rng_name="aqt",
          dtype=self.dtype,
          # float32_logits=self.float32_logits,
          qkv_layout="BSHD_BSHD_BSHD",  # 'BS3HD', 'BSHD_BS2HD' or 'BSHD_BSHD_BSHD'
          scale_factor=self.scale,
          transpose_batch_sequence=False,
      )

  def check_attention_inputs(self, query: Array, key: Array, value: Array) -> None:
    """Check attention inputs."""

    assert key.ndim == value.ndim, "k, v must have same rank."
    assert query.shape[:-3] == key.shape[:-3] == value.shape[:-3], "q, k, v batch dims must match."
    assert key.shape[-2] == value.shape[-2], "k, v num_kv_heads must match."
    assert key.shape[-3] == value.shape[-3], "k, v lengths must match."
    assert query.shape[-1] == key.shape[-1], "q, k depths must match."

  def apply_attention(self, query: Array, key: Array, value: Array,decoder_segment_ids=None):
    """Routes to different attention kernels."""
    self.check_attention_inputs(query, key, value)

    if self.attention_kernel == "flash":
      can_use_flash_attention = (
          query.shape[1] >= self.flash_min_seq_length
          and key.shape[1] >= self.flash_min_seq_length
          and value.shape[1] >= self.flash_min_seq_length
      )
    else:
      can_use_flash_attention = True

    if self.attention_kernel == "dot_product" or self.use_memory_efficient_attention or not can_use_flash_attention:
      return self.apply_attention_dot(query, key, value,decoder_segment_ids)
    elif self.attention_kernel == "flash":
      return self.tpu_flash_attention(query, key * self.scale, value,decoder_segment_ids)
    elif self.attention_kernel == "cudnn_flash_te":
      return self.cudnn_flash_attention(query, key, value)
    else:
      raise ValueError(f"Unexpected attention kernel {self.attention_kernel=}.")

  def tpu_flash_attention(self, query: jax.Array, key: jax.Array, value: jax.Array, decoder_segment_ids=None) -> jax.Array:
    """TPU Flash Attention"""

    query, kv_size = self.reshape_data_for_flash(query)
    key, _ = self.reshape_data_for_flash(key)
    value, _ = self.reshape_data_for_flash(value)
    axis_names = nn.logical_to_mesh_axes(self.flash_axis_names)
    segment_axis_names = nn.logical_to_mesh_axes((BATCH, "activation_length_no_heads"))
    if decoder_segment_ids is not None:
      decoder_segment_ids = splash_attention_kernel.SegmentIds(decoder_segment_ids, decoder_segment_ids)

    @functools.partial(
        shard_map.shard_map,
        mesh=self.mesh,
        in_specs=(
            axis_names,
            axis_names,
            axis_names,
            segment_axis_names,
        ),
        out_specs=axis_names,
        check_rep=False,
    )
    def wrap_flash_attention(query, key, value,decoder_segment_ids):
      if self.flash_block_sizes:
        block_sizes = self.flash_block_sizes
      else:
        block_sizes = splash_attention_kernel.BlockSizes(
            block_q=min(512, query.shape[2]),
            block_kv_compute=min(512, key.shape[2]),
            block_kv=min(512, key.shape[2]),
            block_q_dkv=min(512, query.shape[2]),
            block_kv_dkv=min(512, key.shape[2]),
            block_kv_dkv_compute=min(512, query.shape[2]),
            block_q_dq=min(512, query.shape[2]),
            block_kv_dq=min(512, query.shape[2]),
        )
      masks = [splash_attention_mask.FullMask(_shape=(query.shape[2], query.shape[2])) for _ in range(query.shape[1])]
      multi_head_mask = splash_attention_mask.MultiHeadMask(masks=masks)
      splash_kernel = splash_attention_kernel.make_splash_mha(
          mask=multi_head_mask, head_shards=1, q_seq_shards=1, block_sizes=block_sizes
      )
      return jax.vmap(splash_kernel)(query, key, value,segment_ids = decoder_segment_ids)

    devices_in_data_fsdp = self.mesh.shape["data"] * self.mesh.shape["fsdp"]
    # This warning might show up when doing model eval for example, when calculating model flops
    # and that is expected.
    if not (query.shape[0] / devices_in_data_fsdp).is_integer():
      max_logging.log(
          "Warning, batch dimension should be shardable among the devices in data and fsdp"
          f" axis, batch dimension: {query.shape[0]}, devices_in_data_fsdp: {devices_in_data_fsdp}"
      )
    x = wrap_flash_attention(query, key, value,decoder_segment_ids)
    x = x[:, :, :, :kv_size]
    x = self.reshape_heads_to_head_dim(x)

    return x

  def cudnn_flash_attention(
      self,
      query: Array,
      key: Array,
      value: Array,
  ) -> Array:
    """CUDNN Flash Attention with Transformer Engine.
    1. Stable API, supports GQA
    2. Supports head_dim till 128; head_dim=256 support will be added soon
    """
    # These imports are only meant to work in a GPU build.
    # copied from tpu_flash_attention
    query = self.reshape_data_for_cudnn_flash(query)
    key = self.reshape_data_for_cudnn_flash(key)
    value = self.reshape_data_for_cudnn_flash(value)

    cudnn_flash_axis_names = (BATCH, LENGTH, HEAD, D_KV)
    axis_names = nn.logical_to_mesh_axes(cudnn_flash_axis_names)

    query = nn.with_logical_constraint(query, axis_names)
    key = nn.with_logical_constraint(key, axis_names)
    value = nn.with_logical_constraint(value, axis_names)

    @functools.partial(
        shard_map.shard_map,
        mesh=self.mesh,
        in_specs=(axis_names, axis_names, axis_names),
        out_specs=axis_names,
        check_rep=False,
    )
    def wrap_flash_attention(query, key, value):
      return jax.vmap(self.dpa_layer)(query, key, value, mask=None)

    out = wrap_flash_attention(query, key, value)
    return self.reshape_data_from_cudnn_flash(out)

  def apply_attention_dot(self, query: Array, key: Array, value: Array,decoder_segment_ids=None):
    """Apply Attention."""
    if self.split_head_dim:
      b = key.shape[0]
      query_states = jnp.reshape(query, (b, -1, self.heads, self.dim_head))
      key_states = jnp.reshape(key, (b, -1, self.heads, self.dim_head))
      value_states = jnp.reshape(value, (b, -1, self.heads, self.dim_head))
    else:
      query_states = self.reshape_heads_to_batch_dim(query)
      key_states = self.reshape_heads_to_batch_dim(key)
      value_states = self.reshape_heads_to_batch_dim(value)

    if self.float32_qk_product:
      query_states = query_states.astype(jnp.float32)
      key_states = key_states.astype(jnp.float32)

    if self.split_head_dim:
      attention_scores = jnp.einsum("b t n h, b f n h -> b n f t", key_states, query_states)
    else:
      attention_scores = jnp.einsum("b i d, b j d->b i j", query_states, key_states)

    attention_scores = attention_scores * self.scale

    if decoder_segment_ids is not None:
        # 假设 seq_len_query == seq_len_key == seq_len
        mask = decoder_segment_ids[:, :, None] == decoder_segment_ids[:, None, :]  # (batch_size, seq_len, seq_len)
        if self.split_head_dim:
            mask = mask[:, None, :, :]  # (batch_size, 1, seq_len, seq_len)
        else:
            mask = jnp.repeat(mask, self.heads, axis=0)  # (batch_size * heads, seq_len, seq_len)
        attention_scores = jnp.where(mask, attention_scores, -1e9)

    attention_probs = nn.softmax(attention_scores, axis=-1 if self.split_head_dim else 2)

    attention_probs = attention_probs.astype(self.dtype)

    # attend to values
    if self.split_head_dim:
      hidden_states = jnp.einsum("b n f t, b t n h -> b f n h", attention_probs, value_states)
      b = hidden_states.shape[0]
      hidden_states = jnp.reshape(hidden_states, (b, -1, self.heads * self.dim_head))
    else:
      hidden_states = jnp.einsum("b i j, b j d -> b i d", attention_probs, value_states)
      hidden_states = self.reshape_batch_dim_to_heads(hidden_states)

    return hidden_states

  def reshape_heads_to_batch_dim(self, tensor):
    batch_size, seq_len, dim = tensor.shape
    head_size = self.heads
    tensor = tensor.reshape(batch_size, seq_len, head_size, dim // head_size)
    tensor = jnp.transpose(tensor, (0, 2, 1, 3))
    tensor = tensor.reshape(batch_size * head_size, seq_len, dim // head_size)
    return tensor

  def reshape_batch_dim_to_heads(self, tensor):
    batch_size, seq_len, dim = tensor.shape
    head_size = self.heads
    tensor = tensor.reshape(batch_size // head_size, head_size, seq_len, dim)
    tensor = jnp.transpose(tensor, (0, 2, 1, 3))
    tensor = tensor.reshape(batch_size // head_size, seq_len, dim * head_size)
    return tensor

  def reshape_data_for_cudnn_flash(self, tensor):
    # reshapes from [b, s, h * d] to [b, s, h, d] (input format to flash format)
    batch, seq, heads_and_dim_head = tensor.shape
    tensor = tensor.reshape(batch, seq, self.heads, heads_and_dim_head // self.heads)
    return tensor

  def reshape_data_from_cudnn_flash(self, tensor):
    # reshapes from [b, s, h, d] back to [b, s, h * d]
    return tensor.reshape(tensor.shape[0], tensor.shape[1], -1)

  def reshape_data_for_flash(self, tensor):
    # reshapes from [b, s, h * d] to [b, h, s, d] (input format to flash format)
    batch, seq, heads_and_dim_head = tensor.shape
    tensor = tensor.reshape(batch, seq, self.heads, heads_and_dim_head // self.heads)
    # Transpose to ('batch', 'heads', 'length', 'kv')
    tensor = jnp.transpose(tensor, (0, 2, 1, 3))
    kv_size = tensor.shape[-1]
    if kv_size < 128:
      npad = ((0, 0), (0, 0), (0, 0), (0, 128 - kv_size))
      tensor = jnp.pad(tensor, npad)
    return tensor, kv_size

  def reshape_heads_to_head_dim(self, tensor):
    # takes a tensor of shape [b, h, s, d] and reshapes to [b, s, h * d]
    # This is used to transform the output of flash attention back into the format of other attention outputs
    b, h, s, d = tensor.shape
    tensor = jnp.transpose(tensor, axes=[0, 2, 1, 3])
    return jnp.reshape(tensor, (b, -1, h * d))


def _query_chunk_attention(query, key, value, precision, key_chunk_size: int = 4096):
  """Multi-head dot product attention with a limited number of queries."""
  num_kv, num_heads, k_features = key.shape[-3:]
  v_features = value.shape[-1]
  key_chunk_size = min(key_chunk_size, num_kv)
  query = query / jnp.sqrt(k_features)

  @functools.partial(jax.checkpoint, prevent_cse=False)
  def summarize_chunk(query, key, value):
    attn_weights = jnp.einsum("...qhd,...khd->...qhk", query, key, precision=precision)

    max_score = jnp.max(attn_weights, axis=-1, keepdims=True)
    max_score = jax.lax.stop_gradient(max_score)
    exp_weights = jnp.exp(attn_weights - max_score)

    exp_values = jnp.einsum("...vhf,...qhv->...qhf", value, exp_weights, precision=precision)
    max_score = jnp.einsum("...qhk->...qh", max_score)

    return (exp_values, exp_weights.sum(axis=-1), max_score)

  def chunk_scanner(chunk_idx):
    # julienne key array
    key_chunk = jax.lax.dynamic_slice(
        operand=key,
        start_indices=[0] * (key.ndim - 3) + [chunk_idx, 0, 0],  # [...,k,h,d]
        slice_sizes=list(key.shape[:-3]) + [key_chunk_size, num_heads, k_features],  # [...,k,h,d]
    )

    # julienne value array
    value_chunk = jax.lax.dynamic_slice(
        operand=value,
        start_indices=[0] * (value.ndim - 3) + [chunk_idx, 0, 0],  # [...,v,h,d]
        slice_sizes=list(value.shape[:-3]) + [key_chunk_size, num_heads, v_features],  # [...,v,h,d]
    )

    return summarize_chunk(query, key_chunk, value_chunk)

  chunk_values, chunk_weights, chunk_max = jax.lax.map(f=chunk_scanner, xs=jnp.arange(0, num_kv, key_chunk_size))

  global_max = jnp.max(chunk_max, axis=0, keepdims=True)
  max_diffs = jnp.exp(chunk_max - global_max)

  chunk_values *= jnp.expand_dims(max_diffs, axis=-1)
  chunk_weights *= max_diffs

  all_values = chunk_values.sum(axis=0)
  all_weights = jnp.expand_dims(chunk_weights, -1).sum(axis=0)

  return all_values / all_weights


def jax_memory_efficient_attention(
    query, key, value, precision=jax.lax.Precision.HIGHEST, query_chunk_size: int = 1024, key_chunk_size: int = 4096
):
  r"""
  Flax Memory-efficient multi-head dot product attention. https://arxiv.org/abs/2112.05682v2
  https://github.com/AminRezaei0x443/memory-efficient-attention

  Args:
      query (`jnp.ndarray`): (batch..., query_length, head, query_key_depth_per_head)
      key (`jnp.ndarray`): (batch..., key_value_length, head, query_key_depth_per_head)
      value (`jnp.ndarray`): (batch..., key_value_length, head, value_depth_per_head)
      precision (`jax.lax.Precision`, *optional*, defaults to `jax.lax.Precision.HIGHEST`):
          numerical precision for computation
      query_chunk_size (`int`, *optional*, defaults to 1024):
          chunk size to divide query array value must divide query_length equally without remainder
      key_chunk_size (`int`, *optional*, defaults to 4096):
          chunk size to divide key and value array value must divide key_value_length equally without remainder

  Returns:
      (`jnp.ndarray`) with shape of (batch..., query_length, head, value_depth_per_head)
  """
  num_q, num_heads, q_features = query.shape[-3:]

  def chunk_scanner(chunk_idx, _):
    # julienne query array
    query_chunk = jax.lax.dynamic_slice(
        operand=query,
        start_indices=([0] * (query.ndim - 3)) + [chunk_idx, 0, 0],  # [...,q,h,d]
        slice_sizes=list(query.shape[:-3]) + [min(query_chunk_size, num_q), num_heads, q_features],  # [...,q,h,d]
    )

    return (
        chunk_idx + query_chunk_size,  # unused ignore it
        _query_chunk_attention(query=query_chunk, key=key, value=value, precision=precision, key_chunk_size=key_chunk_size),
    )

  _, res = jax.lax.scan(
      f=chunk_scanner, init=0, xs=None, length=math.ceil(num_q / query_chunk_size)  # start counter  # stop counter
  )

  return jnp.concatenate(res, axis=-3)  # fuse the chunked result back


class FlaxFluxAttention(nn.Module):
  query_dim: int
  heads: int = 8
  dim_head: int = 64
  dropout: float = 0.0
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  attention_kernel: str = "dot_product"
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  mesh: jax.sharding.Mesh = None
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  query_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  key_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  value_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  out_axis_names: AxisNames = (BATCH, LENGTH, EMBED)
  precision: jax.lax.Precision = None
  qkv_bias: bool = False

  def setup(self):
    if self.attention_kernel in {"flash", "cudnn_flash_te"} and self.mesh is None:
      raise ValueError(f"The flash attention kernel requires a value for mesh, but mesh is {self.mesh}")
    inner_dim = self.dim_head * self.heads
    scale = self.dim_head**-0.5

    self.attention_op = AttentionOp(
        mesh=self.mesh,
        attention_kernel=self.attention_kernel,
        scale=scale,
        heads=self.heads,
        dim_head=self.dim_head,
        flash_min_seq_length=self.flash_min_seq_length,
        use_memory_efficient_attention=self.use_memory_efficient_attention,
        split_head_dim=self.split_head_dim,
        flash_block_sizes=self.flash_block_sizes,
        dtype=self.dtype,
        float32_qk_product=False,
    )

    kernel_axes = ("embed", "heads")
    qkv_init_kernel = nn.with_logical_partitioning(nn.initializers.lecun_normal(), kernel_axes)

    self.qkv = nn.Dense(
        inner_dim * 3,
        kernel_init=qkv_init_kernel,
        use_bias=self.qkv_bias,
        bias_init=nn.with_logical_partitioning(nn.initializers.zeros, ("heads",)),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="i_qkv",
        precision=self.precision,
    )

    self.encoder_qkv = nn.Dense(
        inner_dim * 3,
        kernel_init=qkv_init_kernel,
        use_bias=self.qkv_bias,
        bias_init=nn.with_logical_partitioning(nn.initializers.zeros, ("heads",)),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="e_qkv",
        precision=self.precision,
    )

    self.proj_attn = nn.Dense(
        self.query_dim,
        kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), kernel_axes),
        use_bias=True,
        bias_init=nn.with_logical_partitioning(nn.initializers.zeros, ("heads",)),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="i_proj",
        precision=self.precision,
    )

    self.encoder_proj_attn = nn.Dense(
        self.query_dim,
        kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), kernel_axes),
        use_bias=True,
        bias_init=nn.with_logical_partitioning(nn.initializers.zeros, ("heads",)),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="e_proj",
        precision=self.precision,
    )

    self.query_norm = nn.RMSNorm(
        dtype=self.dtype,
        scale_init=nn.with_logical_partitioning(nn.initializers.ones, ("heads",)),
        param_dtype=self.weights_dtype,
    )
    self.key_norm = nn.RMSNorm(
        dtype=self.dtype,
        scale_init=nn.with_logical_partitioning(nn.initializers.ones, ("heads",)),
        param_dtype=self.weights_dtype,
    )

    self.encoder_query_norm = nn.RMSNorm(
        dtype=self.dtype,
        scale_init=nn.with_logical_partitioning(nn.initializers.ones, ("heads",)),
        param_dtype=self.weights_dtype,
    )
    self.encoder_key_norm = nn.RMSNorm(
        dtype=self.dtype,
        scale_init=nn.with_logical_partitioning(nn.initializers.ones, ("heads",)),
        param_dtype=self.weights_dtype,
    )

  def apply_rope(self, xq: Array, xk: Array, freqs_cis: Array) -> tuple[Array, Array]:
    xq_ = xq.reshape(*xq.shape[:-1], -1, 1, 2)
    xk_ = xk.reshape(*xk.shape[:-1], -1, 1, 2)

    xq_out = freqs_cis[..., 0] * xq_[..., 0] + freqs_cis[..., 1] * xq_[..., 1]
    xk_out = freqs_cis[..., 0] * xk_[..., 0] + freqs_cis[..., 1] * xk_[..., 1]

    return xq_out.reshape(*xq.shape).astype(xq.dtype), xk_out.reshape(*xk.shape).astype(xk.dtype)

  def __call__(self, hidden_states, encoder_hidden_states=None, attention_mask=None, image_rotary_emb=None):

    qkv_proj = self.qkv(hidden_states)
    B, L = hidden_states.shape[:2]
    H, D, K = self.heads, qkv_proj.shape[-1] // (self.heads * 3), 3
    qkv_proj = qkv_proj.reshape(B, L, K, H, D).transpose(2, 0, 3, 1, 4)
    query_proj, key_proj, value_proj = qkv_proj

    query_proj = self.query_norm(query_proj)

    key_proj = self.key_norm(key_proj)

    if encoder_hidden_states is not None:

      encoder_qkv_proj = self.encoder_qkv(encoder_hidden_states)
      B, L = encoder_hidden_states.shape[:2]
      H, D, K = self.heads, encoder_qkv_proj.shape[-1] // (self.heads * 3), 3
      encoder_qkv_proj = encoder_qkv_proj.reshape(B, L, K, H, D).transpose(2, 0, 3, 1, 4)
      encoder_query_proj, encoder_key_proj, encoder_value_proj = encoder_qkv_proj

      encoder_query_proj = self.encoder_query_norm(encoder_query_proj)

      encoder_key_proj = self.encoder_key_norm(encoder_key_proj)

      query_proj = jnp.concatenate((encoder_query_proj, query_proj), axis=2)
      key_proj = jnp.concatenate((encoder_key_proj, key_proj), axis=2)
      value_proj = jnp.concatenate((encoder_value_proj, value_proj), axis=2)

      query_proj = nn.with_logical_constraint(query_proj, self.query_axis_names)
      key_proj = nn.with_logical_constraint(key_proj, self.key_axis_names)
      value_proj = nn.with_logical_constraint(value_proj, self.value_axis_names)

    image_rotary_emb = rearrange(image_rotary_emb, "n d (i j) -> n d i j", i=2, j=2)
    query_proj, key_proj = self.apply_rope(query_proj, key_proj, image_rotary_emb)

    query_proj = query_proj.transpose(0, 2, 1, 3).reshape(query_proj.shape[0], query_proj.shape[2], -1)
    key_proj = key_proj.transpose(0, 2, 1, 3).reshape(key_proj.shape[0], key_proj.shape[2], -1)
    value_proj = value_proj.transpose(0, 2, 1, 3).reshape(value_proj.shape[0], value_proj.shape[2], -1)

    attn_output = self.attention_op.apply_attention(query_proj, key_proj, value_proj)
    context_attn_output = None

    if encoder_hidden_states is not None:
      context_attn_output, attn_output = (
          attn_output[:, : encoder_hidden_states.shape[1]],
          attn_output[:, encoder_hidden_states.shape[1] :],
      )

      attn_output = self.proj_attn(attn_output)

      context_attn_output = self.encoder_proj_attn(context_attn_output)

    return attn_output, context_attn_output


class FlaxAttention(nn.Module):
  r"""
  A Flax multi-head attention module as described in: https://arxiv.org/abs/1706.03762

  Parameters:
      query_dim (:obj:`int`):
          Input hidden states dimension
      heads (:obj:`int`, *optional*, defaults to 8):
          Number of heads
      dim_head (:obj:`int`, *optional*, defaults to 64):
          Hidden states dimension inside each head
      dropout (:obj:`float`, *optional*, defaults to 0.0):
          Dropout rate
      use_memory_efficient_attention (`bool`, *optional*, defaults to `False`):
          enable memory efficient attention https://arxiv.org/abs/2112.05682
      split_head_dim (`bool`, *optional*, defaults to `False`):
          Whether to split the head dimension into a new axis for the self-attention computation. In most cases,
          enabling this flag should speed up the computation for Stable Diffusion 2.x and Stable Diffusion XL.
      attention_kernel (`str`, *optional*, defaults to `dot_product`)
          Attention mechanism to be used.
      flash_min_seq_length (`int`, *optional*, defaults to 4096)
          Minimum seq length required to apply flash attention.
      flash_block_sizes (`BlockSizes`, *optional*, defaults to None)
          Overrides default block sizes for flash attention.
      mesh (`jax.sharding.mesh`, *optional*, defaults to `None`):
          jax mesh is required if attention is set to flash.
      dtype (:obj:`jnp.dtype`, *optional*, defaults to jnp.float32):
          Parameters `dtype`
      quant (`AqtQuantization`, *optional*, defaults to None)

  """

  query_dim: int
  heads: int = 8
  dim_head: int = 64
  dropout: float = 0.0
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  attention_kernel: str = "dot_product"
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  mesh: jax.sharding.Mesh = None
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  query_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  key_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  value_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  out_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  precision: jax.lax.Precision = None
  quant: Quant = None

  def setup(self):

    if self.attention_kernel == "flash" and self.mesh is None:
      raise ValueError(f"The flash attention kernel requires a value for mesh, but mesh is {self.mesh}")
    inner_dim = self.dim_head * self.heads
    scale = self.dim_head**-0.5

    self.attention_op = AttentionOp(
        mesh=self.mesh,
        attention_kernel=self.attention_kernel,
        scale=scale,
        heads=self.heads,
        dim_head=self.dim_head,
        flash_min_seq_length=self.flash_min_seq_length,
        use_memory_efficient_attention=self.use_memory_efficient_attention,
        split_head_dim=self.split_head_dim,
        flash_block_sizes=self.flash_block_sizes,
        dtype=self.dtype,
        quant=self.quant,
    )

    qkv_init_kernel = nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("embed", "heads"))
    dot_general_cls = None
    if self.quant:
      dot_general_cls = self.quant.dot_general_cls()
    self.query = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=True,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_q",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.key = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=True,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_k",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.value = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=True,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_v",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.proj_attn = nn.Dense(
        self.query_dim,
        kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("heads", "embed")),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_out_0",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )
    self.dropout_layer = nn.Dropout(rate=self.dropout)

  def __call__(self, hidden_states, context=None, deterministic=True, cross_attention_kwargs=None):
    context = hidden_states if context is None else context
    query_proj = self.query(hidden_states)
    key_proj = self.key(context)
    value_proj = self.value(context)

    query_proj = nn.with_logical_constraint(query_proj, self.query_axis_names)
    key_proj = nn.with_logical_constraint(key_proj, self.key_axis_names)
    value_proj = nn.with_logical_constraint(value_proj, self.value_axis_names)

    hidden_states = self.attention_op.apply_attention(query_proj, key_proj, value_proj)

    hidden_states = self.proj_attn(hidden_states)
    hidden_states = nn.with_logical_constraint(hidden_states, (BATCH, LENGTH, HEAD))
    return self.dropout_layer(hidden_states, deterministic=deterministic)


class FlaxBasicTransformerBlock(nn.Module):
  r"""
  A Flax transformer block layer with `GLU` (Gated Linear Unit) activation function as described in:
  https://arxiv.org/abs/1706.03762


  Parameters:
      dim (:obj:`int`):
          Inner hidden states dimension
      n_heads (:obj:`int`):
          Number of heads
      d_head (:obj:`int`):
          Hidden states dimension inside each head
      dropout (:obj:`float`, *optional*, defaults to 0.0):
          Dropout rate
      only_cross_attention (`bool`, defaults to `False`):
          Whether to only apply cross attention.
      dtype (:obj:`jnp.dtype`, *optional*, defaults to jnp.float32):
          Parameters `dtype`
      use_memory_efficient_attention (`bool`, *optional*, defaults to `False`):
          enable memory efficient attention https://arxiv.org/abs/2112.05682
      split_head_dim (`bool`, *optional*, defaults to `False`):
          Whether to split the head dimension into a new axis for the self-attention computation. In most cases,
          enabling this flag should speed up the computation for Stable Diffusion 2.x and Stable Diffusion XL.
      attention_kernel (`str`, *optional*, defaults to `dot_product`)
          Attention mechanism to be used.
      flash_min_seq_length (`int`, *optional*, defaults to 4096)
          Minimum seq length required to apply flash attention.
      flash_block_sizes (`BlockSizes`, *optional*, defaults to None)
          Overrides default block sizes for flash attention.
      mesh (`jax.sharding.mesh`, *optional*, defaults to `None`):
          jax mesh is required if attention is set to flash.
      quant (`AqtQuantization`, *optional*, defaults to None)
  """

  dim: int
  n_heads: int
  d_head: int
  dropout: float = 0.0
  only_cross_attention: bool = False
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  attention_kernel: str = "dot_product"
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  mesh: jax.sharding.Mesh = None
  precision: jax.lax.Precision = None
  quant: Quant = None

  def setup(self):
    # self attention (or cross_attention if only_cross_attention is True)
    self.attn1 = FlaxAttention(
        self.dim,
        self.n_heads,
        self.d_head,
        self.dropout,
        self.use_memory_efficient_attention,
        self.split_head_dim,
        attention_kernel=self.attention_kernel,
        flash_min_seq_length=self.flash_min_seq_length,
        flash_block_sizes=self.flash_block_sizes,
        mesh=self.mesh,
        dtype=self.dtype,
        weights_dtype=self.weights_dtype,
        precision=self.precision,
        quant=self.quant,
    )
    # cross attention
    self.attn2 = FlaxAttention(
        self.dim,
        self.n_heads,
        self.d_head,
        self.dropout,
        self.use_memory_efficient_attention,
        self.split_head_dim,
        attention_kernel=self.attention_kernel,
        flash_min_seq_length=self.flash_min_seq_length,
        flash_block_sizes=self.flash_block_sizes,
        mesh=self.mesh,
        dtype=self.dtype,
        weights_dtype=self.weights_dtype,
        precision=self.precision,
        quant=self.quant,
    )
    self.ff = FlaxFeedForward(
        dim=self.dim, dropout=self.dropout, dtype=self.dtype, weights_dtype=self.weights_dtype, precision=self.precision
    )
    self.norm1 = nn.LayerNorm(epsilon=1e-5, dtype=self.dtype, param_dtype=self.weights_dtype)
    self.norm2 = nn.LayerNorm(epsilon=1e-5, dtype=self.dtype, param_dtype=self.weights_dtype)
    self.norm3 = nn.LayerNorm(epsilon=1e-5, dtype=self.dtype, param_dtype=self.weights_dtype)
    self.dropout_layer = nn.Dropout(rate=self.dropout)

  def __call__(self, hidden_states, context, deterministic=True, cross_attention_kwargs=None):
    # self attention
    residual = hidden_states
    if self.only_cross_attention:
      hidden_states = self.attn1(
          self.norm1(hidden_states), context, deterministic=deterministic, cross_attention_kwargs=cross_attention_kwargs
      )
    else:
      hidden_states = self.attn1(
          self.norm1(hidden_states), deterministic=deterministic, cross_attention_kwargs=cross_attention_kwargs
      )

    hidden_states = hidden_states + residual

    # cross attention
    residual = hidden_states
    hidden_states = self.attn2(
        self.norm2(hidden_states), context, deterministic=deterministic, cross_attention_kwargs=cross_attention_kwargs
    )
    hidden_states = hidden_states + residual

    # feed forward
    residual = hidden_states
    hidden_states = self.ff(self.norm3(hidden_states), deterministic=deterministic)
    hidden_states = hidden_states + residual

    return self.dropout_layer(hidden_states, deterministic=deterministic)


class FlaxTransformer2DModel(nn.Module):
  r"""
  A Spatial Transformer layer with Gated Linear Unit (GLU) activation function as described in:
  https://arxiv.org/pdf/1506.02025.pdf


  Parameters:
      in_channels (:obj:`int`):
          Input number of channels
      n_heads (:obj:`int`):
          Number of heads
      d_head (:obj:`int`):
          Hidden states dimension inside each head
      depth (:obj:`int`, *optional*, defaults to 1):
          Number of transformers block
      dropout (:obj:`float`, *optional*, defaults to 0.0):
          Dropout rate
      use_linear_projection (`bool`, defaults to `False`): tbd
      only_cross_attention (`bool`, defaults to `False`): tbd
      dtype (:obj:`jnp.dtype`, *optional*, defaults to jnp.float32):
          Parameters `dtype`
      use_memory_efficient_attention (`bool`, *optional*, defaults to `False`):
          enable memory efficient attention https://arxiv.org/abs/2112.05682
      split_head_dim (`bool`, *optional*, defaults to `False`):
          Whether to split the head dimension into a new axis for the self-attention computation. In most cases,
          enabling this flag should speed up the computation for Stable Diffusion 2.x and Stable Diffusion XL.
      attention_kernel (`str`, *optional*, defaults to `dot_product`)
          Attention mechanism to be used.
      flash_min_seq_length (`int`, *optional*, defaults to 4096)
          Minimum seq length required to apply flash attention.
      flash_block_sizes (`BlockSizes`, *optional*, defaults to None)
          Overrides default block sizes for flash attention.
      mesh (`jax.sharding.mesh`, *optional*, defaults to `None`):
          jax mesh is required if attention is set to flash.
      quant (`AqtQuantization`, *optional*, defaults to None)
            Configures AQT quantization github.com/google/aqt.
  """

  in_channels: int
  n_heads: int
  d_head: int
  depth: int = 1
  dropout: float = 0.0
  use_linear_projection: bool = False
  only_cross_attention: bool = False
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  attention_kernel: str = "dot_product"
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  mesh: jax.sharding.Mesh = None
  norm_num_groups: int = 32
  precision: jax.lax.Precision = None
  hidden_state_axis_names: AxisNames = (BATCH, LENGTH, D_KV)
  quant: Quant = (None,)

  def setup(self):
    self.norm = nn.GroupNorm(num_groups=self.norm_num_groups, epsilon=1e-5, dtype=self.dtype, param_dtype=self.weights_dtype)

    conv_kernel_init = nn.with_logical_partitioning(
        nn.initializers.lecun_normal(), ("keep_1", "keep_2", "conv_in", "conv_out")
    )

    inner_dim = self.n_heads * self.d_head
    if self.use_linear_projection:
      self.proj_in = nn.Dense(
          inner_dim,
          kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("embed", "hidden")),
          dtype=self.dtype,
          param_dtype=self.weights_dtype,
          precision=self.precision,
      )
    else:
      self.proj_in = nn.Conv(
          inner_dim,
          kernel_init=conv_kernel_init,
          kernel_size=(1, 1),
          strides=(1, 1),
          padding="VALID",
          dtype=self.dtype,
          param_dtype=self.weights_dtype,
          precision=self.precision,
      )

    self.transformer_blocks = [
        FlaxBasicTransformerBlock(
            inner_dim,
            self.n_heads,
            self.d_head,
            dropout=self.dropout,
            only_cross_attention=self.only_cross_attention,
            dtype=self.dtype,
            weights_dtype=self.weights_dtype,
            use_memory_efficient_attention=self.use_memory_efficient_attention,
            split_head_dim=self.split_head_dim,
            attention_kernel=self.attention_kernel,
            flash_min_seq_length=self.flash_min_seq_length,
            flash_block_sizes=self.flash_block_sizes,
            mesh=self.mesh,
            precision=self.precision,
            quant=self.quant,
        )
        for _ in range(self.depth)
    ]

    if self.use_linear_projection:
      self.proj_out = nn.Dense(
          inner_dim,
          kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("hidden", "embed")),
          dtype=self.dtype,
          param_dtype=self.weights_dtype,
          precision=self.precision,
      )
    else:
      self.proj_out = nn.Conv(
          inner_dim,
          kernel_init=conv_kernel_init,
          kernel_size=(1, 1),
          strides=(1, 1),
          padding="VALID",
          dtype=self.dtype,
          param_dtype=self.weights_dtype,
          precision=self.precision,
      )

    self.dropout_layer = nn.Dropout(rate=self.dropout)

  def __call__(self, hidden_states, context, deterministic=True, cross_attention_kwargs=None):
    batch, height, width, channels = hidden_states.shape
    residual = hidden_states
    hidden_states = self.norm(hidden_states)
    if self.use_linear_projection:
      hidden_states = hidden_states.reshape(batch, height * width, channels)
      hidden_states = self.proj_in(hidden_states)
    else:
      hidden_states = self.proj_in(hidden_states)
      hidden_states = hidden_states.reshape(batch, height * width, channels)

    for transformer_block in self.transformer_blocks:
      hidden_states = transformer_block(
          hidden_states, context, deterministic=deterministic, cross_attention_kwargs=cross_attention_kwargs
      )

    if self.use_linear_projection:
      hidden_states = self.proj_out(hidden_states)
      hidden_states = hidden_states.reshape(batch, height, width, channels)
    else:
      hidden_states = hidden_states.reshape(batch, height, width, channels)
      hidden_states = self.proj_out(hidden_states)

    hidden_states = nn.with_logical_constraint(hidden_states, self.hidden_state_axis_names)

    hidden_states = hidden_states + residual
    return self.dropout_layer(hidden_states, deterministic=deterministic)


class FlaxFeedForward(nn.Module):
  r"""
  Flax module that encapsulates two Linear layers separated by a non-linearity. It is the counterpart of PyTorch's
  [`FeedForward`] class, with the following simplifications:
  - The activation function is currently hardcoded to a gated linear unit from:
  https://arxiv.org/abs/2002.05202
  - `dim_out` is equal to `dim`.
  - The number of hidden dimensions is hardcoded to `dim * 4` in [`FlaxGELU`].

  Parameters:
      dim (:obj:`int`):
          Inner hidden states dimension
      dropout (:obj:`float`, *optional*, defaults to 0.0):
          Dropout rate
      dtype (:obj:`jnp.dtype`, *optional*, defaults to jnp.float32):
          Parameters `dtype`
  """

  dim: int
  dropout: float = 0.0
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  precision: jax.lax.Precision = None

  def setup(self):
    # The second linear layer needs to be called
    # net_2 for now to match the index of the Sequential layer
    self.net_0 = FlaxGEGLU(self.dim, self.dropout, self.dtype, self.weights_dtype, precision=self.precision)
    self.net_2 = nn.Dense(self.dim, dtype=self.dtype, param_dtype=self.weights_dtype, precision=self.precision)

  def __call__(self, hidden_states, deterministic=True):
    hidden_states = self.net_0(hidden_states, deterministic=deterministic)
    hidden_states = self.net_2(hidden_states)
    return hidden_states


class FlaxGEGLU(nn.Module):
  r"""
  Flax implementation of a Linear layer followed by the variant of the gated linear unit activation function from
  https://arxiv.org/abs/2002.05202.

  Parameters:
      dim (:obj:`int`):
          Input hidden states dimension
      dropout (:obj:`float`, *optional*, defaults to 0.0):
          Dropout rate
      dtype (:obj:`jnp.dtype`, *optional*, defaults to jnp.float32):
          Parameters `dtype`
  """

  dim: int
  dropout: float = 0.0
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  precision: jax.lax.Precision = None

  def setup(self):
    inner_dim = self.dim * 4
    self.proj = nn.Dense(inner_dim * 2, dtype=self.dtype, param_dtype=self.weights_dtype, precision=self.precision)
    self.dropout_layer = nn.Dropout(rate=self.dropout)

  def __call__(self, hidden_states, deterministic=True):
    hidden_states = self.proj(hidden_states)
    hidden_linear, hidden_gelu = jnp.split(hidden_states, 2, axis=2)
    return self.dropout_layer(hidden_linear * nn.gelu(hidden_gelu), deterministic=deterministic)


class FlaxF5Attention(nn.Module):
  query_dim: int
  heads: int = 8
  dim_head: int = 64
  dropout: float = 0.0
  use_memory_efficient_attention: bool = False
  split_head_dim: bool = False
  attention_kernel: str = "dot_product"
  flash_min_seq_length: int = 4096
  flash_block_sizes: BlockSizes = None
  mesh: jax.sharding.Mesh = None
  dtype: jnp.dtype = jnp.float32
  weights_dtype: jnp.dtype = jnp.float32
  query_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  key_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  value_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  out_axis_names: AxisNames = (BATCH, LENGTH, HEAD)
  precision: jax.lax.Precision = None
  qkv_bias : bool = False
  quant: Quant = None

  def setup(self):

    if self.attention_kernel == "flash" and self.mesh is None:
      raise ValueError(f"The flash attention kernel requires a value for mesh, but mesh is {self.mesh}")
    inner_dim = self.dim_head * self.heads
    scale = self.dim_head**-0.5

    self.attention_op = AttentionOp(
        mesh=self.mesh,
        attention_kernel=self.attention_kernel,
        scale=scale,
        heads=self.heads,
        dim_head=self.dim_head,
        flash_min_seq_length=self.flash_min_seq_length,
        use_memory_efficient_attention=self.use_memory_efficient_attention,
        split_head_dim=self.split_head_dim,
        flash_block_sizes=self.flash_block_sizes,
        dtype=self.dtype,
        quant=self.quant,
    )

    qkv_init_kernel = nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("embed", "heads"))
    dot_general_cls = None
    if self.quant:
      dot_general_cls = self.quant.dot_general_cls()

    self.query = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=self.qkv_bias,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_q",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.key = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=self.qkv_bias,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_k",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.value = nn.Dense(
        inner_dim,
        kernel_init=qkv_init_kernel,
        use_bias=self.qkv_bias,
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_v",
        precision=self.precision,
        dot_general_cls=dot_general_cls,
    )

    self.proj_attn = nn.Dense(
        self.query_dim,
        kernel_init=nn.with_logical_partitioning(nn.initializers.lecun_normal(), ("heads", "embed")),
        dtype=self.dtype,
        param_dtype=self.weights_dtype,
        name="to_out_0",
        precision=self.precision,
    )
    self.dropout_layer = nn.Dropout(rate=self.dropout)
  def rotate_half(self,x):
    x = rearrange(x, '... (d r) -> ... d r', r = 2)
    x1, x2 = jnp.split(x,indices_or_sections=2,axis = -1)
    x = jnp.concatenate((-x2, x1), axis = -1)
    return rearrange(x, '... d r -> ... (d r)')

  def apply_rotary_pos_emb(self, t, freqs, scale = 1,decoder_segment_ids=None):
      rot_dim, seq_len, orig_dtype = freqs.shape[-1], t.shape[-2], t.dtype

      freqs = freqs[:, -seq_len:, :]
      if decoder_segment_ids is not None:
        freqs = freqs * decoder_segment_ids[...,jnp.newaxis]
      scale = scale[:, -seq_len:, :] if isinstance(scale, jnp.ndarray) else scale

      if t.ndim == 4 and freqs.ndim == 3:
          freqs = rearrange(freqs, 'b n d -> b 1 n d')

      # partial rotary embeddings, Wang et al. GPT-J
      t, t_unrotated = t[..., :rot_dim], t[..., rot_dim:]
      t = (t * jnp.cos(freqs) * scale) + (self.rotate_half(t) * jnp.sin(freqs) * scale)
      out = jnp.concatenate((t, t_unrotated), axis = -1)

      return out.astype(orig_dtype)
  
  def __call__(self, hidden_states, context=None,rope=None,decoder_segment_ids=None,deterministic=True):
    context = hidden_states if context is None else context
    query_proj = self.query(hidden_states)
    key_proj = self.key(context)
    value_proj = self.value(context)

    if rope is not None:
      freqs, xpos_scale = rope
      q_xpos_scale, k_xpos_scale = (xpos_scale, xpos_scale**-1.0) if xpos_scale is not None else (1.0, 1.0)
      query_proj = jnp.reshape(query_proj,(query_proj.shape[0], -1, self.heads, self.dim_head)).transpose(0,2,1,3)
      key_proj = jnp.reshape(key_proj,(key_proj.shape[0], -1, self.heads, self.dim_head)).transpose(0,2,1,3)
      value_proj = jnp.reshape(value_proj,(value_proj.shape[0], -1, self.heads, self.dim_head)).transpose(0,2,1,3)
      query_proj = self.apply_rotary_pos_emb(query_proj, freqs, q_xpos_scale,decoder_segment_ids)
      key_proj = self.apply_rotary_pos_emb(key_proj, freqs, k_xpos_scale,decoder_segment_ids)

    hidden_states = self.attention_op.apply_attention(
      jnp.reshape(query_proj.transpose(0,2,1,3),(query_proj.shape[0], -1, self.heads * self.dim_head)),
      jnp.reshape(key_proj.transpose(0,2,1,3),(query_proj.shape[0], -1, self.heads * self.dim_head)),
      jnp.reshape(value_proj.transpose(0,2,1,3),(query_proj.shape[0], -1, self.heads * self.dim_head)),
      decoder_segment_ids=decoder_segment_ids,)

    hidden_states = jnp.reshape(hidden_states,(hidden_states.shape[0], -1, self.heads * self.dim_head))

    hidden_states = self.proj_attn(hidden_states)
    hidden_states = nn.with_logical_constraint(hidden_states, (BATCH, LENGTH, HEAD))
    hidden_states = self.dropout_layer(hidden_states, deterministic=deterministic)
    if decoder_segment_ids is not None:
      hidden_states = hidden_states * decoder_segment_ids[...,jnp.newaxis]
    return hidden_states