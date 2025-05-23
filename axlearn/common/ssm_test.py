# Some of the code in this file is adapted from:
#
# johnma2006/mamba-minimal
# Copyright 2023 John Ma. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
#
# huggingface/transformers
# Copyright 2024 The Huggingface Inc. team. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License").
#
# ai21labs/Jamba-v0.1
# (https://huggingface.co/ai21labs/Jamba-v0.1)
# Copyright 2024 The AI21 Jamba authors. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License").


"""Tests Mamba/Mamba2 and Jamba implementations."""
import math
from typing import Optional

import jax
import jax.numpy as jnp
import numpy as np
import pytest
import torch
from absl.testing import parameterized
from jax._src.mesh import ResourceEnv, thread_resources
from jax.experimental import mesh_utils
from jax.sharding import Mesh, PartitionSpec
from transformers.activations import ACT2FN
from transformers.configuration_utils import PretrainedConfig

from axlearn.common.attention import KVCache
from axlearn.common.attention_bias import make_causal_biases
from axlearn.common.config import InstantiableConfig
from axlearn.common.module import functional as F
from axlearn.common.ssm import (
    AssociativeScanMambaRecurrence,
    BlockResidualMode,
    JambaMamba2Block,
    JambaMambaBlock,
    LinearScanMambaRecurrence,
    Mamba2MixerLayer,
    MambaBlock,
    MambaMixerLayer,
    PallasSSDRecurrence,
    RepeatedSSMLayer,
    StackedMixedSSMTransformerLayer,
    StackedSSMLayer,
)
from axlearn.common.ssm_kernels.ssd_kernels import ssd
from axlearn.common.test_utils import TestCase, assert_allclose, set_threefry_partitionable
from axlearn.common.utils import Nested, Tensor, TensorSpec, cast_floats

try:
    from mamba_ssm.modules.mamba2_simple import Mamba2Simple  # pytype: disable=import-error

    MAMBA_INSTALLED = True
except ModuleNotFoundError:
    MAMBA_INSTALLED = False

# The following PyTorch Mamba implementations are adapted from:
# https://github.com/huggingface/transformers/blob/v4.39.3/src/transformers/models/mamba/modeling_mamba.py
# and
# https://github.com/huggingface/transformers/blob/v4.39.3/src/transformers/models/mamba/configuration_mamba.py


class MambaConfig(PretrainedConfig):
    """HuffingFace Mamba Config"""

    model_type = "mamba"

    def __init__(
        self,
        vocab_size=50280,
        hidden_size=768,
        state_size=16,
        num_hidden_layers=32,
        layer_norm_epsilon=1e-5,
        pad_token_id=0,
        bos_token_id=0,
        eos_token_id=0,
        expand=2,
        conv_kernel=4,
        use_bias=False,
        use_conv_bias=True,
        hidden_act="silu",
        initializer_range=0.1,
        residual_in_fp32=True,
        time_step_rank="auto",
        time_step_scale=1.0,
        time_step_min=0.001,
        time_step_max=0.1,
        time_step_init_scheme="random",
        time_step_floor=1e-4,
        rescale_prenorm_residual=False,
        use_cache=True,
        **kwargs,
    ):
        self.vocab_size = vocab_size
        self.hidden_size = hidden_size
        self.state_size = state_size
        self.num_hidden_layers = num_hidden_layers
        self.layer_norm_epsilon = layer_norm_epsilon
        self.conv_kernel = conv_kernel
        self.expand = expand
        self.intermediate_size = int(expand * self.hidden_size)
        self.bos_token_id = bos_token_id
        self.eos_token_id = eos_token_id
        self.pad_token_id = pad_token_id
        self.use_bias = use_bias
        self.use_conv_bias = use_conv_bias
        self.hidden_act = hidden_act
        self.initializer_range = initializer_range
        self.time_step_rank = (
            math.ceil(self.hidden_size / 16) if time_step_rank == "auto" else time_step_rank
        )
        self.time_step_scale = time_step_scale
        self.time_step_min = time_step_min
        self.time_step_max = time_step_max
        self.time_step_init_scheme = time_step_init_scheme
        self.time_step_floor = time_step_floor
        self.rescale_prenorm_residual = rescale_prenorm_residual
        self.residual_in_fp32 = residual_in_fp32
        self.use_cache = use_cache

        super().__init__(
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            pad_token_id=pad_token_id,
            **kwargs,
        )


class MambaCache:
    """HuggingFace MambaCache"""

    def __init__(self, config, batch_size, dtype=torch.float16, device=None):
        self.seqlen_offset = 0
        self.dtype = dtype
        intermediate_size = config.intermediate_size
        ssm_state_size = config.state_size
        conv_kernel_size = config.conv_kernel

        self.conv_states = {
            i: torch.zeros(
                batch_size, intermediate_size, conv_kernel_size, device=device, dtype=dtype
            )
            for i in range(config.num_hidden_layers)
        }
        self.ssm_states = {
            i: torch.zeros(
                batch_size, intermediate_size, ssm_state_size, device=device, dtype=dtype
            )
            for i in range(config.num_hidden_layers)
        }


# pylint: disable=line-too-long
# pylint: disable=invalid-name
class MambaMixer(torch.nn.Module):
    """
    Compute ∆, A, B, C, and D the state space parameters and compute the `contextualized_states`.
    A, D are input independent (see Mamba paper [1] Section 3.5.2 "Interpretation of A" for why A isn't selective)
    ∆, B, C are input-dependent (this is a key difference between Mamba and the linear time invariant S4,
    and is why Mamba is called **selective** state spaces)
    """

    def __init__(self, config, layer_idx):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.ssm_state_size = config.state_size
        self.conv_kernel_size = config.conv_kernel
        self.intermediate_size = config.intermediate_size
        self.time_step_rank = config.time_step_rank
        self.layer_idx = layer_idx
        self.use_conv_bias = config.use_conv_bias
        self.conv1d = torch.nn.Conv1d(
            in_channels=self.intermediate_size,
            out_channels=self.intermediate_size,
            bias=config.use_conv_bias,
            kernel_size=config.conv_kernel,
            groups=self.intermediate_size,
            padding=config.conv_kernel - 1,
        )

        self.activation = config.hidden_act
        self.act = ACT2FN[config.hidden_act]

        # projection of the input hidden states
        self.in_proj = torch.nn.Linear(
            self.hidden_size, self.intermediate_size * 2, bias=config.use_bias
        )
        # selective projection used to make dt, B and C input dependant
        self.x_proj = torch.nn.Linear(
            self.intermediate_size, self.time_step_rank + self.ssm_state_size * 2, bias=False
        )
        # time step projection (discretization)
        self.dt_proj = torch.nn.Linear(self.time_step_rank, self.intermediate_size, bias=True)

        # S4D real initialization. These are not discretized!
        # The core is to load them, compute the discrete states, then write the updated state. Keeps the memory bounded
        A = torch.arange(1, self.ssm_state_size + 1, dtype=torch.float32)[None, :]
        A = A.expand(self.intermediate_size, -1).contiguous()

        self.A_log = torch.nn.Parameter(torch.log(A))
        self.D = torch.nn.Parameter(torch.ones(self.intermediate_size))
        self.out_proj = torch.nn.Linear(
            self.intermediate_size, self.hidden_size, bias=config.use_bias
        )
        self.use_bias = config.use_bias

    # fmt: off
    def slow_forward(self, input_states, cache_params: Optional[MambaCache]=None):
        batch_size, seq_len, _ = input_states.shape
        dtype = input_states.dtype
        # 1. Gated MLP's linear projection
        projected_states = self.in_proj(input_states).transpose(1, 2)                   # [batch, 2 * intermediate_size, seq_len]
        hidden_states, gate = projected_states.chunk(2, dim=1)

        # 2. Convolution sequence transformation
        if cache_params is not None:
            ssm_state = cache_params.ssm_states[self.layer_idx]
            if cache_params.seqlen_offset > 0:
                conv_state = cache_params.conv_states[self.layer_idx]                   # [batch, intermediate_size, conv_kernel_size]
                conv_state = torch.roll(conv_state, shifts=-1, dims=-1)
                conv_state[:, :, -1] = hidden_states[:, :, 0]
                cache_params.conv_states[self.layer_idx].copy_(conv_state)
                hidden_states = torch.sum(conv_state * self.conv1d.weight[:, 0, :], dim=-1)
                if self.use_conv_bias:
                    hidden_states += self.conv1d.bias
                hidden_states = self.act(hidden_states).to(dtype).unsqueeze(-1)         # [batch, intermediate_size, 1] : decoding
            else:
                conv_state = torch.nn.functional.pad(
                    hidden_states,
                    (self.conv_kernel_size - hidden_states.shape[-1], 0)
                )
                cache_params.conv_states[self.layer_idx].copy_(conv_state)
                hidden_states = self.act(self.conv1d(hidden_states)[..., :seq_len])     # [batch, intermediate_size, seq_len]
        else:
            ssm_state = torch.zeros(
                (batch_size, self.intermediate_size, self.ssm_state_size),
                device=hidden_states.device, dtype=dtype
            )
            hidden_states = self.act(self.conv1d(hidden_states)[..., :seq_len])         # [batch, intermediate_size, seq_len]

        # 3. State Space Model sequence transformation
        # 3.a. Selection:  [batch, seq_len, self.time_step_rank + self.ssm_state_size * 2]
        ssm_parameters = self.x_proj(hidden_states.transpose(1, 2))
        time_step, B, C = torch.split(
            ssm_parameters, [self.time_step_rank, self.ssm_state_size, self.ssm_state_size], dim=-1
        )
        discrete_time_step = self.dt_proj(time_step)                                    # [batch, seq_len, intermediate_size]
        discrete_time_step = torch.nn.functional.softplus(discrete_time_step).transpose(1, 2) # [batch, intermediate_size, seq_len]

        # 3.b. Discretization: B and C to [batch, seq_len, intermediate_size, ssm_state_size] (SRAM)
        A = -torch.exp(self.A_log.float())                                              # [intermediate_size, ssm_state_size]
        discrete_A = torch.exp(A[None, :, None, :] * discrete_time_step[:, :, :, None]) # [batch, intermediate_size, seq_len, ssm_state_size]
        discrete_B = discrete_time_step[:, :, :, None] * B[:, None, :, :].float()       # [batch, intermediade_size, seq_len, ssm_state_size]
        deltaB_u = discrete_B * hidden_states[:, :, :, None].float()

        # 3.c perform the recurrence y ← SSM(A, B, C)(x)
        scan_outputs = []
        for i in range(seq_len):
            ssm_state = discrete_A[:, :, i, :] * ssm_state + deltaB_u[:, :, i, :]      # [batch, intermediade_size, ssm_state]
            scan_output = torch.matmul(ssm_state.to(dtype), C[:, i, :].unsqueeze(-1))  # [batch, intermediade_size, 1]
            scan_outputs.append(scan_output[:, :, 0])
        scan_output = torch.stack(scan_outputs, dim=-1)                                # [batch, seq_len, intermediade_size]
        scan_output = scan_output + (hidden_states * self.D[None, :, None])
        scan_output = (scan_output * self.act(gate)) # pylint: disable=superfluous-parens

        if cache_params is not None:
            cache_params.ssm_states[self.layer_idx].copy_(ssm_state)

        # 4. Final linear projection
        contextualized_states = self.out_proj(scan_output.transpose(1, 2))             # [batch, seq_len, hidden_size]
        return contextualized_states
    # fmt: on

    def forward(self, hidden_states, cache_params: Optional[MambaCache] = None):
        return self.slow_forward(hidden_states, cache_params)


class MambaMixerLayerTest(TestCase):
    def test_forward(self):
        model_dim = 4
        state_dim = 16
        test_cfg = MambaMixerLayer.default_config().set(input_dim=model_dim, state_dim=state_dim)
        test_layer = test_cfg.set(name="test").instantiate(parent=None)
        layer_params = test_layer.initialize_parameters_recursively(jax.random.PRNGKey(0))

        ref_cfg = MambaConfig(hidden_size=model_dim, state_size=state_dim)
        ref_layer = MambaMixer(ref_cfg, layer_idx=0)

        # Copy parameters.
        ref_layer.in_proj.weight.data.copy_(
            torch.from_numpy(
                np.array(layer_params["input_proj"]["weight"].reshape(model_dim, -1).T)
            )
        )
        ref_layer.conv1d.weight.data.copy_(
            torch.from_numpy(np.array(layer_params["conv"]["weight"].swapaxes(0, 2)))
        )
        ref_layer.conv1d.bias.data.copy_(torch.from_numpy(np.array(layer_params["conv"]["bias"])))

        def _copy_linear_params(
            our_linear_params: Nested[Tensor], pt_linear_layer: torch.nn.Linear
        ):
            pt_linear_layer.weight.data.copy_(
                torch.from_numpy(np.array(jnp.swapaxes(our_linear_params["weight"], 0, 1)))
            )
            if pt_linear_layer.bias is not None:
                assert "bias" in our_linear_params
                pt_linear_layer.bias.data.copy_(
                    torch.from_numpy(np.array(our_linear_params["bias"]))
                )
            else:
                assert "bias" not in our_linear_params

        _copy_linear_params(layer_params["x_proj"], ref_layer.x_proj)
        _copy_linear_params(layer_params["dt_proj"], ref_layer.dt_proj)
        _copy_linear_params(layer_params["out_proj"], ref_layer.out_proj)

        # Construct test inputs.
        batch_size, tgt_len = 2, 6
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim])
        x_pt = torch.from_numpy(np.array(x))

        outputs, _ = F(
            test_layer,
            inputs=(x,),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        ref_outputs = ref_layer(x_pt)
        assert_allclose(outputs.data, ref_outputs.detach().numpy())

    def test_associative_scan(self):
        """Tests that a simple linear scan and an associative scan give the same results."""
        model_dim = 8
        state_dim = 16
        test_cfg = MambaMixerLayer.default_config().set(input_dim=model_dim, state_dim=state_dim)
        test_layer1 = test_cfg.set(
            mamba_recurrence=LinearScanMambaRecurrence.default_config(), name="test"
        ).instantiate(parent=None)
        layer_params = test_layer1.initialize_parameters_recursively(jax.random.PRNGKey(0))
        test_layer2 = (
            test_cfg.clone()
            .set(mamba_recurrence=AssociativeScanMambaRecurrence.default_config(), name="test2")
            .instantiate(parent=None)
        )

        # Construct test inputs.
        batch_size, tgt_len = 2, 9
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim])

        outputs1, _ = F(
            test_layer1,
            inputs=(x,),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        outputs2, _ = F(
            test_layer2,
            inputs=(x,),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        assert_allclose(outputs1.data, outputs2.data)

    @parameterized.parameters(jnp.float32, jnp.bfloat16)
    def test_data_types(self, dtype: jnp.dtype):
        model_dim = 16
        state_dim = 8
        cfg = MambaMixerLayer.default_config().set(
            name="test",
            input_dim=model_dim,
            state_dim=state_dim,
            dtype=dtype,
        )
        layer = cfg.instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(123))
        layer_params = cast_floats(layer_params, to_dtype=dtype)

        batch_size, tgt_len = 2, 6
        x = jnp.zeros([batch_size, tgt_len, model_dim], dtype=dtype)
        layer_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(456),
            inputs=(x,),
        )
        self.assertEqual(layer_outputs.data.dtype, dtype)

    @parameterized.parameters(jnp.float32, jnp.bfloat16)
    def test_extend_step(self, dtype: jnp.dtype):
        model_dim = 4
        state_dim = 16
        cfg = MambaMixerLayer.default_config().set(
            input_dim=model_dim,
            state_dim=state_dim,
            cache_dtype=dtype,
            dtype=dtype,
        )
        layer: MambaMixerLayer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)
        batch_size, tgt_len = 2, 6
        query = jax.random.normal(
            jax.random.PRNGKey(1),
            [batch_size, tgt_len, model_dim],
            dtype=dtype,
        )
        inputs = dict(query=query)
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )
        initial_state, initial_output = layer.init_states(
            time_step=None,
            query=TensorSpec([batch_size, tgt_len], dtype=dtype),
        )
        self.assertIsNone(initial_output)
        for k in ["conv_input", "state"]:
            self.assertEqual(initial_state[k].dtype, dtype)

        inputs = dict(cached_states=initial_state)
        decoder_output = jnp.zeros(shape=[tgt_len, batch_size, model_dim])
        for t in range(tgt_len):
            inputs["query"] = jnp.expand_dims(query[:, t, :], axis=1)
            extend_step_outputs, _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(3),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cached_states"] = extend_step_outputs[0]
            decoder_output = decoder_output.at[t].set(
                jnp.squeeze(extend_step_outputs[1].data, axis=1)
            )
        decoder_output_transposed = jnp.transpose(decoder_output, [1, 0, 2])
        assert_allclose(decoder_output_transposed, forward_outputs.data, atol=1e-6)

    @parameterized.parameters(jnp.float32, jnp.bfloat16)
    def test_prefill_states(self, dtype: jnp.dtype):
        model_dim = 4
        state_dim = 16
        cfg = MambaMixerLayer.default_config().set(
            input_dim=model_dim,
            state_dim=state_dim,
            cache_dtype=dtype,
            dtype=dtype,
        )
        layer: MambaMixerLayer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)
        batch_size, tgt_len = 3, 6
        query = jax.random.normal(
            jax.random.PRNGKey(1),
            [batch_size, tgt_len, model_dim],
            dtype=dtype,
        )
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=dict(query=query),
        )
        time_step = jnp.arange(batch_size)
        (initial_states, initial_output), _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(3),
            inputs=dict(time_step=time_step, query=query),
            method="init_states",
        )
        self.assertTrue(jnp.all(time_step == initial_states["time_step"]))
        for k in ["conv_input", "state"]:
            self.assertEqual(initial_states[k].dtype, dtype)
        self.assertEqual(
            initial_states["conv_input"].shape, (batch_size, cfg.conv.window, layer.inner_dim)
        )
        self.assertEqual(
            initial_states["state"].shape, (batch_size, 1, cfg.state_dim, layer.inner_dim)
        )

        # Zero-out outputs starting from initial time_step, and test that we can recover the full
        # outputs by calling extend_step starting from time_step.
        # [batch, tgt_len].
        time_step_mask = (jnp.arange(tgt_len) < time_step[:, None]).astype(dtype)
        # [batch, tgt_len, model_dim].
        decoder_output = initial_output.data * time_step_mask[..., None]

        # Call extend_step from time_step, ensuring that outputs match.
        inputs = dict(cached_states=initial_states)
        while jnp.any(time_step < tgt_len):
            # [batch, tgt_len=1, model_dim].
            inputs["query"] = jnp.take_along_axis(
                query, time_step[:, None, None], axis=1, mode="clip"
            )
            (updated_state, outputs), _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(4),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cached_states"] = updated_state

            # [batch, 1, model_dim]
            curr_outputs = outputs.data

            # [batch, tgt_len, 1].
            oh_indices = jax.nn.one_hot(time_step, tgt_len)[..., None]
            decoder_output = decoder_output + curr_outputs * oh_indices
            time_step = time_step + 1

        assert_allclose(decoder_output, forward_outputs.data, atol=1e-6)


def _test_extend_step(layer_cfg: InstantiableConfig, *, model_dim: int, dtype: jnp.dtype):
    """Tests extend for composite layers."""
    layer = layer_cfg.set(name="test").instantiate(parent=None)
    layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
    layer_params = cast_floats(layer_params, to_dtype=dtype)
    batch_size, tgt_len = 2, 6
    query = jax.random.normal(
        jax.random.PRNGKey(1),
        [batch_size, tgt_len, model_dim],
        dtype=dtype,
    )
    self_attention_logit_biases = make_causal_biases(
        tgt_len
    )  # Only necessary for self-attn layers.
    inputs = dict(data=query, self_attention_logit_biases=self_attention_logit_biases)
    forward_outputs, _ = F(
        layer,
        state=layer_params,
        is_training=False,
        prng_key=jax.random.PRNGKey(2),
        inputs=inputs,
    )
    if isinstance(layer, MambaMixerLayer):
        init_kwargs = dict(query=TensorSpec([batch_size, tgt_len], dtype=dtype))
    else:
        init_kwargs = dict(data=TensorSpec([batch_size, tgt_len], dtype=dtype))
    initial_state, initial_output = layer.init_states(time_step=None, **init_kwargs)
    assert initial_output is None
    inputs = dict(cached_states=initial_state)
    decoder_output = jnp.zeros(shape=[tgt_len, batch_size, model_dim])
    for t in range(tgt_len):
        inputs["data"] = jnp.expand_dims(query[:, t, :], axis=1)
        inputs["self_attention_logit_biases"] = self_attention_logit_biases[
            jnp.newaxis, jnp.newaxis, t, :
        ]
        extend_step_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(3),
            inputs=inputs,
            method="extend_step",
        )
        inputs["cached_states"] = extend_step_outputs[0]
        decoder_output = decoder_output.at[t].set(jnp.squeeze(extend_step_outputs[1].data, axis=1))
    decoder_output_transposed = jnp.transpose(decoder_output, [1, 0, 2])
    atol = 1e-6
    assert_allclose(decoder_output_transposed, forward_outputs.data, atol=atol)


def _test_prefill_states(layer_cfg: InstantiableConfig, *, model_dim: int, dtype: jnp.dtype):
    """Tests prefill for composite layers."""
    layer = layer_cfg.set(name="test").instantiate(parent=None)
    layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
    layer_params = cast_floats(layer_params, to_dtype=dtype)
    batch_size, tgt_len = 3, 6
    query = jax.random.normal(
        jax.random.PRNGKey(1),
        [batch_size, tgt_len, model_dim],
        dtype=dtype,
    )
    self_attention_logit_biases = make_causal_biases(
        tgt_len
    )  # Only necessary for self-attn layers.
    inputs = dict(data=query, self_attention_logit_biases=self_attention_logit_biases)
    forward_outputs, _ = F(
        layer,
        state=layer_params,
        is_training=False,
        prng_key=jax.random.PRNGKey(2),
        inputs=inputs,
    )
    time_step = jnp.arange(batch_size)
    (initial_states, initial_output), _ = F(
        layer,
        state=layer_params,
        is_training=False,
        prng_key=jax.random.PRNGKey(3),
        inputs=dict(
            time_step=time_step,
            data=query,
            self_attention_logit_biases=self_attention_logit_biases,
        ),
        method="init_states",
    )

    # Zero-out outputs starting from initial time_step, and test that we can recover the full
    # outputs by calling extend_step starting from time_step.
    # [batch, tgt_len].
    time_step_mask = (jnp.arange(tgt_len) < time_step[:, None]).astype(dtype)
    # [batch, tgt_len, model_dim].
    decoder_output = initial_output.data * time_step_mask[..., None]

    # Call extend_step from time_step, ensuring that outputs match.
    inputs = dict(cached_states=initial_states)
    while jnp.any(time_step < tgt_len):
        # [batch, tgt_len=1, model_dim].
        inputs["data"] = jnp.take_along_axis(query, time_step[:, None, None], axis=1, mode="clip")
        inputs["self_attention_logit_biases"] = jnp.take_along_axis(
            self_attention_logit_biases[None, :, :],
            time_step[:, None, None],
            axis=1,
            mode="clip",
        )
        (updated_state, outputs), _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(4),
            inputs=inputs,
            method="extend_step",
        )
        inputs["cached_states"] = updated_state

        # [batch, 1, model_dim]
        curr_outputs = outputs.data

        # [batch, tgt_len, 1].
        oh_indices = jax.nn.one_hot(time_step, tgt_len)[..., None]
        decoder_output = decoder_output + curr_outputs * oh_indices
        time_step = time_step + 1

    atol = 1e-6
    assert_allclose(decoder_output, forward_outputs.data, atol=atol)


class MambaBlockTest(TestCase):
    """Tests that MambaBlocks behave as expected."""

    @parameterized.product(
        block_klass=(MambaBlock, JambaMambaBlock),
        dtype=(jnp.float32, jnp.bfloat16),
        residual_mode=(BlockResidualMode.FP32, BlockResidualMode.NOCAST),
    )
    def test_output_dtype(
        self, block_klass: MambaBlock, dtype: jnp.dtype, residual_mode: BlockResidualMode
    ):
        model_dim = 4
        state_dim = 16
        hidden_dim = 10
        cfg = block_klass.default_config().set(
            input_dim=model_dim,
            state_dim=state_dim,
            residual_mode=residual_mode,
        )
        if hasattr(cfg, "feed_forward"):
            cfg.feed_forward.hidden_dim = hidden_dim

        test_layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = test_layer.initialize_parameters_recursively(jax.random.PRNGKey(0))
        batch_size, tgt_len = 2, 6
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim], dtype=dtype)
        layer_params = cast_floats(layer_params, to_dtype=dtype)
        outputs, _ = F(
            test_layer,
            inputs=dict(data=x),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        self.assertEqual(outputs.data.dtype, dtype)

    @parameterized.product(
        block_klass=(MambaBlock, JambaMambaBlock),
        dtype=(jnp.float32, jnp.bfloat16),
    )
    def test_extend_step(self, block_klass: MambaBlock, dtype: jnp.dtype):
        model_dim = 8
        state_dim = 16
        hidden_dim = 10
        cfg = block_klass.default_config().set(
            input_dim=model_dim,
            state_dim=state_dim,
        )
        cfg.mamba_layer.dtype = dtype
        cfg.mamba_layer.cache_dtype = dtype

        if hasattr(cfg, "feed_forward"):
            cfg.feed_forward.hidden_dim = hidden_dim

        _test_extend_step(cfg, model_dim=model_dim, dtype=dtype)

    @parameterized.product(
        block_klass=(MambaBlock, JambaMambaBlock),
        dtype=(jnp.float32, jnp.bfloat16),
    )
    @set_threefry_partitionable(False)  # TODO(swiseman): update for threefry_partitionable True
    def test_prefill(self, block_klass: MambaBlock, dtype: jnp.dtype):
        model_dim = 8
        state_dim = 16
        hidden_dim = 10
        cfg = block_klass.default_config().set(
            input_dim=model_dim,
            state_dim=state_dim,
        )
        cfg.mamba_layer.dtype = dtype
        cfg.mamba_layer.cache_dtype = dtype

        if hasattr(cfg, "feed_forward"):
            cfg.feed_forward.hidden_dim = hidden_dim

        _test_prefill_states(cfg, model_dim=model_dim, dtype=dtype)


class StackedMambaTest(TestCase):
    """Tests whether mamba layers can be used inside stacked transformers."""

    def test_stacking_forward(self):
        model_dim = 4
        state_dim = 16
        num_layers = 2
        stacked_mamba_cfg = StackedSSMLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            layer=MambaBlock.default_config().set(
                state_dim=state_dim,
            ),
        )
        test_layer = stacked_mamba_cfg.set(name="test").instantiate(parent=None)
        layer_params = test_layer.initialize_parameters_recursively(jax.random.PRNGKey(0))
        batch_size, tgt_len = 2, 6
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim])
        stacked_outputs, _ = F(
            test_layer,
            inputs=dict(data=x),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        for i, layer in enumerate(test_layer._layers):  # pylint: disable=protected-access
            outputs, _ = F(
                layer,
                inputs=dict(data=x),
                state=layer_params[f"layer{i}"],
                is_training=True,
                prng_key=jax.random.PRNGKey(2),
            )
            x = outputs.data
        assert_allclose(x, stacked_outputs.data)

    def test_repeating_forward(self):
        model_dim = 4
        state_dim = 16
        num_layers = 2
        repeated_mamba_cfg = RepeatedSSMLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            layer=MambaBlock.default_config().set(
                state_dim=state_dim,
            ),
        )
        test_layer = repeated_mamba_cfg.set(name="test").instantiate(parent=None)
        layer_params = test_layer.initialize_parameters_recursively(jax.random.PRNGKey(0))
        stacked_mamba_cfg = StackedSSMLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            layer=MambaBlock.default_config().set(
                state_dim=state_dim,
            ),
        )
        stacked_layer = stacked_mamba_cfg.set(name="test2").instantiate(parent=None)
        # Make params in the format a stacked model expects.
        stacked_params = {}
        for i in range(num_layers):
            stacked_params[f"layer{i}"] = jax.tree.map(
                # pylint: disable-next=cell-var-from-loop
                lambda x: x[i],
                layer_params["repeat"]["layer"],
            )
        batch_size, tgt_len = 2, 6
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim])
        repeated_outputs, _ = F(
            test_layer,
            inputs=dict(data=x),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        stacked_outputs, _ = F(
            stacked_layer,
            inputs=dict(data=x),
            state=stacked_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        assert_allclose(repeated_outputs.data, stacked_outputs.data)

    @parameterized.product(
        block_klass=(MambaBlock, JambaMambaBlock),
        dtype=(jnp.float32, jnp.bfloat16),
    )
    def test_extend_step(self, block_klass: MambaBlock, dtype: jnp.dtype):
        model_dim = 16
        state_dim = 16
        hidden_dim = 32
        num_layers = 3

        cfg = StackedSSMLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            layer=block_klass.default_config().set(
                state_dim=state_dim,
            ),
        )
        cfg.layer.mamba_layer.set(dtype=dtype, cache_dtype=None)
        if hasattr(cfg.layer, "feed_forward"):
            cfg.layer.feed_forward.hidden_dim = hidden_dim

        _test_extend_step(cfg, model_dim=model_dim, dtype=dtype)

    @parameterized.product(
        block_klass=(MambaBlock, JambaMambaBlock),
        dtype=(jnp.float32, jnp.bfloat16),
    )
    def test_prefill(self, block_klass: MambaBlock, dtype: jnp.dtype):
        model_dim = 16
        state_dim = 16
        hidden_dim = 32
        num_layers = 3

        cfg = StackedSSMLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            layer=block_klass.default_config().set(
                state_dim=state_dim,
            ),
        )
        cfg.layer.mamba_layer.set(dtype=dtype, cache_dtype=None)

        if hasattr(cfg.layer, "feed_forward"):
            cfg.layer.feed_forward.hidden_dim = hidden_dim

        _test_prefill_states(cfg, model_dim=model_dim, dtype=dtype)


class StackedMixedSSMTransformerTest(TestCase):
    """Tests that mixing SSM layers and transformer layers behaves as expected."""

    def test_forward(self):
        model_dim = 8
        state_dim = 16
        num_heads = 4
        hidden_dim = 10
        num_layers = 4
        cfg = StackedMixedSSMTransformerLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            ssm_layer=JambaMambaBlock.default_config().set(
                state_dim=state_dim,
            ),
            transformer_layer_period=2,
            transformer_layer_offset=1,
        )
        cfg.ssm_layer.feed_forward.hidden_dim = hidden_dim
        cfg.layer.feed_forward.hidden_dim = hidden_dim
        cfg.layer.self_attention.attention.num_heads = num_heads

        test_layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = test_layer.initialize_parameters_recursively(jax.random.PRNGKey(0))

        batch_size, tgt_len = 2, 6
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, model_dim])
        self_attention_logit_biases = make_causal_biases(tgt_len)
        stacked_outputs, _ = F(
            test_layer,
            inputs=dict(data=x, self_attention_logit_biases=self_attention_logit_biases),
            state=layer_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )
        for i, layer in enumerate(test_layer._layers):  # pylint: disable=protected-access
            outputs, _ = F(
                layer,
                inputs=dict(data=x, self_attention_logit_biases=self_attention_logit_biases),
                state=layer_params[f"layer{i}"],
                is_training=True,
                prng_key=jax.random.PRNGKey(2),
            )
            x = outputs.data
        assert_allclose(x, stacked_outputs.data)

    @parameterized.parameters(jnp.float32, jnp.bfloat16)
    def test_extend_step(self, dtype: jnp.dtype):
        model_dim = 16
        state_dim = 16
        num_heads = 4
        hidden_dim = 32
        num_layers = 4
        cfg = StackedMixedSSMTransformerLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            transformer_layer_period=3,
            transformer_layer_offset=1,
            ssm_layer=JambaMambaBlock.default_config().set(
                state_dim=state_dim,
            ),
            dtype=dtype,
        )
        cfg.ssm_layer.feed_forward.hidden_dim = hidden_dim
        cfg.ssm_layer.mamba_layer.set(dtype=dtype, cache_dtype=None)
        cfg.layer.feed_forward.hidden_dim = hidden_dim
        cfg.layer.self_attention.attention.num_heads = num_heads
        cfg.layer.self_attention.attention.set(
            kv_cache=KVCache.default_config().set(cache_dtype=dtype)
        )
        _test_extend_step(cfg, model_dim=model_dim, dtype=dtype)

    @parameterized.parameters(jnp.float32, jnp.bfloat16)
    def test_prefill(self, dtype: jnp.dtype):
        model_dim = 16
        state_dim = 16
        num_heads = 4
        hidden_dim = 32
        num_layers = 4
        cfg = StackedMixedSSMTransformerLayer.default_config().set(
            input_dim=model_dim,
            num_layers=num_layers,
            transformer_layer_period=3,
            transformer_layer_offset=1,
            ssm_layer=JambaMambaBlock.default_config().set(
                state_dim=state_dim,
            ),
        )
        cfg.ssm_layer.feed_forward.hidden_dim = hidden_dim
        cfg.ssm_layer.mamba_layer.set(dtype=dtype, cache_dtype=None)
        cfg.layer.feed_forward.hidden_dim = hidden_dim
        cfg.layer.self_attention.attention.num_heads = num_heads
        cfg.layer.self_attention.attention.set(
            kv_cache=KVCache.default_config().set(cache_dtype=dtype)
        )
        _test_prefill_states(cfg, model_dim=model_dim, dtype=dtype)


@pytest.mark.skipif(
    jax.default_backend() != "tpu" or jax.device_count() != 4,
    reason="Test requires four chips, e.g., one v5p gcp instance.",
)
class Mamba2RecurrenceTest(TestCase):
    """Test the correctness of the Mamba2 recurrence for decoding."""

    @classmethod
    def setup_class(cls):
        devices = mesh_utils.create_device_mesh((2, 1, 1, 1, 2))
        global_mesh = Mesh(devices, axis_names=("data", "expert", "fsdp", "seq", "model"))
        new_env = ResourceEnv(physical_mesh=global_mesh, loops=())
        thread_resources.env = new_env

    @classmethod
    def teardown_class(cls):
        init_env = ResourceEnv(physical_mesh=(), loops=())
        thread_resources.env = init_env

    def test_ssd_parameterization(self):
        batch_size, num_heads, seq_len, state_dim, head_dim = 2, 4, 1024, 128, 256
        key = jax.random.PRNGKey(0)
        dtype = jnp.float32

        # note that construct random params requires that log_a <= 0 and delta > 0.
        x = jax.random.normal(key, (batch_size, num_heads, seq_len, head_dim), dtype=dtype)
        llog_a = jax.random.uniform(key, (1, num_heads, 1), dtype=dtype)
        log_a = -jnp.exp(llog_a)
        b = jax.random.normal(key, (batch_size, num_heads, seq_len, state_dim), dtype=dtype)
        c = jax.random.normal(key, (batch_size, num_heads, seq_len, state_dim), dtype=dtype)
        delta = jax.nn.softplus(
            jax.random.uniform(key, (batch_size, num_heads, seq_len), dtype=dtype) - 4.0
        )
        d = jax.random.normal(key, (1, num_heads, 1, 1), dtype=dtype)

        mamba2_dim_to_partition_spec = {
            "bhtd": PartitionSpec(("data", "expert", "fsdp"), ("seq", "model"), None, None),
            "bht": PartitionSpec(("data", "expert", "fsdp"), ("seq", "model"), None),
        }
        output_partition_spec = PartitionSpec(("data", "expert", "fsdp"), "model", "seq", None)

        cfg = PallasSSDRecurrence.default_config().set(
            name="test",
            mamba2_dim_to_partition_spec=mamba2_dim_to_partition_spec,
            output_partition_spec=output_partition_spec,
        )
        layer = cfg.instantiate(parent=None)
        o_module, _ = F(
            layer,
            inputs=dict(x=x, log_a=log_a, b=b, c=c, delta=delta, d=d),
            state=None,
            is_training=False,
            prng_key=key,
        )

        # alternative input to the kernel; delta by default is applied to x to get x_bar, here we can
        # also apply it to b to get b_bar first.
        b_bar = b * jnp.expand_dims(delta, axis=-1)
        loga_bar = log_a * delta
        o_alternative = ssd(c, b_bar, x, loga_bar) + d * x
        assert_allclose(o_module.data, o_alternative, atol=1e-1, rtol=1e-1)


@pytest.mark.skipif(
    jax.default_backend() != "tpu" or jax.device_count() != 4,
    reason="Test requires four chips, e.g., one v5p gcp instance.",
)
class Mamba2MixerLayerTest(TestCase):
    @classmethod
    def setup_class(cls):
        devices = mesh_utils.create_device_mesh((2, 1, 1, 1, 2))
        global_mesh = Mesh(devices, axis_names=("data", "expert", "fsdp", "seq", "model"))
        new_env = ResourceEnv(physical_mesh=global_mesh, loops=())
        thread_resources.env = new_env

    @classmethod
    def teardown_class(cls):
        init_env = ResourceEnv(physical_mesh=(), loops=())
        thread_resources.env = init_env

    @parameterized.product(
        dtype=(jnp.float32, jnp.bfloat16),
        inference_mode=(True, False),
    )
    def test_extend_step(self, dtype: jnp.dtype, inference_mode: bool):
        batch_size = 2
        input_dim = 512
        state_dim = 128
        num_heads = 2
        seq_len = 1024
        num_groups = 2
        expansion_factor = 1
        output_dim = input_dim
        cache_dtype = dtype

        cfg = Mamba2MixerLayer.default_config().set(
            input_dim=input_dim,
            state_dim=state_dim,
            num_heads=num_heads,
            num_groups=num_groups,
            expansion_factor=expansion_factor,
            dtype=dtype,
            cache_dtype=cache_dtype,
        )

        layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)

        if inference_mode:
            # inference recurrence can return the ssd states for testing
            layer.recurrence = layer.inference_recurrence

        inputs_data = jax.random.uniform(
            jax.random.PRNGKey(1), [batch_size, seq_len, input_dim], dtype=dtype
        )
        inputs = dict(query=inputs_data)
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )

        mamba2_cache = layer.init_states(target_batch_size=batch_size, target_max_len=seq_len)
        self.assertEqual(mamba2_cache.x_conv_state.dtype, cache_dtype)
        self.assertEqual(mamba2_cache.b_conv_state.dtype, cache_dtype)
        self.assertEqual(mamba2_cache.c_conv_state.dtype, cache_dtype)
        self.assertEqual(mamba2_cache.ssd_state.dtype, cache_dtype)
        self.assertEqual(forward_outputs.data.dtype, dtype)

        inputs = dict(cache=mamba2_cache)
        decoder_output = jnp.zeros(shape=[seq_len, batch_size, output_dim], dtype=dtype)
        for t in range(seq_len):
            inputs["query"] = inputs_data[:, t : t + 1, :]
            (mamba2_cache, mamba2output), _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(3),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cache"] = mamba2_cache
            decoder_output = decoder_output.at[t].set(jnp.squeeze(mamba2output.data, axis=1))

        decoder_output_transposed = jnp.transpose(decoder_output, [1, 0, 2])

        if dtype == jnp.float32:
            final_state_diff_tol = 1e-2
            output_tol = 1e-1
        else:
            final_state_diff_tol = 1e-1
            output_tol = 2e0

        if inference_mode:
            forward_final_state = forward_outputs.ssd_state[:, :, -1]
            final_state_diff = jnp.abs((forward_final_state - mamba2_cache.ssd_state)).max()
            self.assertTrue(final_state_diff < final_state_diff_tol)

        # ssm output diff will get a bit amplified by the ffn layer
        assert_allclose(
            decoder_output_transposed, forward_outputs.data, atol=output_tol, rtol=output_tol
        )

    @parameterized.product(dtype=(jnp.float32, jnp.bfloat16))
    def test_prefill_states(self, dtype: jnp.dtype):
        batch_size = 2
        input_dim = 512
        state_dim = 256
        num_heads = 4
        seq_len = 1024
        num_groups = 2
        expansion_factor = 2
        cache_dtype = jnp.float32

        cfg = Mamba2MixerLayer.default_config().set(
            input_dim=input_dim,
            state_dim=state_dim,
            num_heads=num_heads,
            num_groups=num_groups,
            expansion_factor=expansion_factor,
            dtype=dtype,
            cache_dtype=cache_dtype,
        )

        layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)

        # full forward pass as reference
        inputs_data = jax.random.uniform(
            jax.random.PRNGKey(1), [batch_size, seq_len, input_dim], dtype=dtype
        )
        inputs = dict(query=inputs_data)
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )

        # prefill stage
        time_step = jnp.arange(batch_size)
        (initial_state, initial_output), _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(3),
            inputs=dict(time_step=time_step, query=inputs_data),
            method="prefill_states",
        )
        self.assertTrue(initial_state.x_conv_state.dtype, cache_dtype)
        self.assertTrue(initial_state.b_conv_state.dtype, cache_dtype)
        self.assertTrue(initial_state.c_conv_state.dtype, cache_dtype)
        self.assertTrue(initial_state.ssd_state.dtype, cache_dtype)
        self.assertTrue(initial_output.data.dtype, dtype)

        time_step_mask = (jnp.arange(seq_len) < time_step[:, None]).astype(dtype)
        decoder_output = initial_output.data * time_step_mask[..., None]

        inputs = dict(cache=initial_state)
        while jnp.any(time_step < seq_len):
            inputs["query"] = jnp.take_along_axis(
                inputs_data, time_step[:, None, None], axis=1, mode="clip"
            )
            (updated_state, outputs), _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(4),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cache"] = updated_state

            # [batch_size, 1, output_dim]
            cur_outputs = outputs.data

            # [batch_size, seq_len, 1]
            oh_indices = jax.nn.one_hot(time_step, seq_len, dtype=dtype)[..., None]
            decoder_output = decoder_output + cur_outputs * oh_indices

            time_step = time_step + 1

        assert_allclose(decoder_output, forward_outputs.data, atol=1e-1, rtol=1e-1)


@pytest.mark.skipif(
    jax.default_backend() != "tpu" or jax.device_count() != 4,
    reason="Test requires four chips, e.g., one v5p gcp instance.",
)
class JambaMamba2BlockTest(TestCase):
    @classmethod
    def setup_class(cls):
        devices = mesh_utils.create_device_mesh((2, 1, 1, 1, 2))
        global_mesh = Mesh(devices, axis_names=("data", "expert", "fsdp", "seq", "model"))
        new_env = ResourceEnv(physical_mesh=global_mesh, loops=())
        thread_resources.env = new_env

    @classmethod
    def teardown_class(cls):
        init_env = ResourceEnv(physical_mesh=(), loops=())
        thread_resources.env = init_env

    @parameterized.product(
        input_dim=[1024, 2048],
        state_dim=[128, 256],
        num_heads=[2, 4],
        num_groups=[2, 4],
        dtype=[jnp.float32, jnp.bfloat16],
    )
    def forward(
        self, input_dim: int, state_dim: int, num_heads: int, num_groups: int, dtype: jnp.dtype
    ):
        mamba2block_cfg = JambaMamba2Block.default_config().set(
            name="test",
            input_dim=input_dim,
            state_dim=state_dim,
            num_heads=num_heads,
            num_groups=num_groups,
            dtype=dtype,
        )
        mamba2block_cfg.feed_forward = mamba2block_cfg.feed_forward.set(hidden_dim=2 * input_dim)
        mamba2block = mamba2block_cfg.instantiate(parent=None)
        mamba2block_params = mamba2block.initialize_parameters_recursively(
            prng_key=jax.random.PRNGKey(0)
        )

        batch_size, tgt_len = 2, 1024
        x = jax.random.uniform(jax.random.PRNGKey(1), [batch_size, tgt_len, input_dim], dtype=dtype)

        outputs, _ = F(
            mamba2block,
            inputs=(x,),
            state=mamba2block_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
        )

        self.assertEqual(outputs.data.shape, x.shape)
        self.assertEqual(outputs.data.dtype, x.dtype)

    @parameterized.product(
        batch_size=[2, 4],
        input_dim=[1024, 2048],
        seq_len=[1024, 2048],
        state_dim=[128, 256],
        num_heads=[2, 4],
        num_groups=[2, 4],
        dtype=[jnp.float32, jnp.bfloat16],
    )
    def extend_step(
        self,
        batch_size: int,
        input_dim: int,
        seq_len: int,
        state_dim: int,
        num_heads: int,
        num_groups: int,
        dtype: jnp.dtype,
    ):
        cfg = JambaMamba2Block.default_config().set(
            input_dim=input_dim,
            state_dim=state_dim,
            num_heads=num_heads,
            num_groups=num_groups,
            dtype=dtype,
        )
        cfg.feed_forward = cfg.feed_forward.set(hidden_dim=2 * input_dim)
        layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)

        inputs_data = jax.random.normal(
            jax.random.PRNGKey(1), [batch_size, seq_len, input_dim], dtype=dtype
        )
        inputs = dict(data=inputs_data)
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )

        init_state = layer.init_states(target_batch_size=batch_size, target_max_len=seq_len)
        self.assertEqual(init_state["mamba_block"].x_conv_state.dtype, dtype)
        self.assertEqual(init_state["mamba_block"].b_conv_state.dtype, dtype)
        self.assertEqual(init_state["mamba_block"].c_conv_state.dtype, dtype)
        self.assertEqual(init_state["mamba_block"].ssd_state.dtype, dtype)

        inputs = dict(cached_states=init_state)
        decoder_output = jnp.zeros(shape=[seq_len, batch_size, input_dim])
        for t in range(seq_len):
            inputs["data"] = inputs_data[:, t : t + 1, :]
            extend_step_output, _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(3),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cached_states"] = extend_step_output[0]
            decoder_output = decoder_output.at[t].set(
                jnp.squeeze(extend_step_output[1].data, axis=1)
            )

        decoder_output_transposed = jnp.transpose(decoder_output, [1, 0, 2])
        assert_allclose(decoder_output_transposed, forward_outputs.data, atol=1e-1, rtol=1e-1)

    @parameterized.product(
        batch_size=[2],
        input_dim=[1024],
        state_dim=[256],
        num_heads=[2],
        seq_len=[1024],
        num_groups=[2],
        dtype=[jnp.float32, jnp.bfloat16],
    )
    def test_prefill_states(
        self,
        batch_size: int,
        input_dim: int,
        seq_len: int,
        state_dim: int,
        num_heads: int,
        num_groups: int,
        dtype: jnp.dtype,
    ):
        cfg = JambaMamba2Block.default_config().set(
            input_dim=input_dim,
            state_dim=state_dim,
            num_heads=num_heads,
            num_groups=num_groups,
            dtype=dtype,
        )
        cfg.feed_forward = cfg.feed_forward.set(hidden_dim=2 * input_dim)
        layer = cfg.set(name="test").instantiate(parent=None)
        layer_params = layer.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        layer_params = cast_floats(layer_params, to_dtype=dtype)

        inputs_data = jax.random.normal(
            jax.random.PRNGKey(1), [batch_size, seq_len, input_dim], dtype=dtype
        )
        inputs = dict(data=inputs_data)
        forward_outputs, _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )

        time_step = jnp.arange(batch_size)
        (initial_state, initial_output), _ = F(
            layer,
            state=layer_params,
            is_training=False,
            prng_key=jax.random.PRNGKey(3),
            inputs=dict(time_step=time_step, data=inputs_data),
            method="prefill_states",
        )

        time_step_mask = (jnp.arange(seq_len) < time_step[:, None]).astype(dtype)
        decoder_output = initial_output.data * time_step_mask[..., None]

        inputs = dict(cached_states=initial_state)
        for _ in range(seq_len):
            inputs["data"] = jnp.take_along_axis(
                inputs_data, time_step[:, None, None], axis=1, mode="clip"
            )
            (updated_state, outputs), _ = F(
                layer,
                state=layer_params,
                is_training=False,
                prng_key=jax.random.PRNGKey(3),
                inputs=inputs,
                method="extend_step",
            )
            inputs["cached_states"] = updated_state

            # [batch_size, 1, output_dim]
            cur_outputs = outputs.data

            # [batch_size, seq_len, 1]
            oh_indices = jax.nn.one_hot(time_step, seq_len, dtype=dtype)[..., None]
            decoder_output = decoder_output + cur_outputs * oh_indices

            time_step = time_step + 1

        assert_allclose(decoder_output, forward_outputs.data, atol=1e-1, rtol=1e-1)


@pytest.mark.skipif(
    jax.default_backend() != "gpu" or not MAMBA_INSTALLED,
    reason="Test requires mamba_ssm to be installed on a GPU machine",
)
class GPUMamba2MixerLayerTest(TestCase):
    @classmethod
    def setup_class(cls):
        num_devices = jax.device_count()
        devices = mesh_utils.create_device_mesh((1, 1, 1, 1, num_devices))
        global_mesh = Mesh(devices, axis_names=("data", "expert", "fsdp", "seq", "model"))
        new_env = ResourceEnv(physical_mesh=global_mesh, loops=())
        thread_resources.env = new_env

    @classmethod
    def teardown_class(cls):
        init_env = ResourceEnv(physical_mesh=(), loops=())
        thread_resources.env = init_env

    @parameterized.product(
        batch_size=[2, 4],
        seq_len=[512, 1024],
        expansion_factor=[1, 2],
    )
    def test_forward(self, batch_size: int, seq_len: int, expansion_factor: int):
        if self.mamba_ssm is None:
            self.skipTest("mamba_ssm needs to be installed on a GPU machine for testing")

        d_model, d_state, expansion_factor = 512, 128, 2
        head_dim, num_groups = 128, 4
        d_inner = expansion_factor * d_model
        num_heads = d_inner // head_dim

        def _j2t(param):
            """Convert jax array to torch tensor."""
            return torch.from_numpy(np.array(param))

        inputs_data = jax.random.normal(jax.random.PRNGKey(1), [batch_size, seq_len, d_model])
        inputs_torch = _j2t(inputs_data)

        # pylint: disable=undefined-variable
        ref_model = Mamba2Simple(
            d_model=d_model,
            d_state=d_state,
            headdim=head_dim,
            ngroups=num_groups,
            expand=expansion_factor,
            use_mem_eff_path=False,
        )

        jax_model = (
            Mamba2MixerLayer.default_config()
            .set(
                input_dim=d_model,
                state_dim=d_state,
                num_groups=num_groups,
                num_heads=num_heads,
                expansion_factor=expansion_factor,
                bc_norm=None,
                dtype=jnp.float32,
                cache_dtype=jnp.float32,
            )
            .set(name="test")
            .instantiate(parent=None)
        )
        jax_params = jax_model.initialize_parameters_recursively(prng_key=jax.random.PRNGKey(0))
        jax_params = cast_floats(jax_params, to_dtype=jnp.float32)

        # use linearscan kernel which is already tested against pallas kernel.
        jax_model.recurrence = jax_model.inference_recurrence

        # copying the weights from the jax model to the ref model
        inputs = dict(query=inputs_data)
        forward_outputs, _ = F(
            jax_model,
            state=jax_params,
            is_training=True,
            prng_key=jax.random.PRNGKey(2),
            inputs=inputs,
        )
        jax_output_np = np.array(forward_outputs.data)

        # in_proj <-> [z, x, B, C, dt]
        xz_w = _j2t(jax_params["xz_proj"]["weight"])  # [d_model, 2, d_inner]
        bc_w = _j2t(jax_params["bc_proj"]["weight"])  # [d_model, 2, dk]
        dt_w = _j2t(jax_params["dt_proj"]["weight"])  # [d_model, num_heads]
        zxBCdt_w = torch.cat([xz_w[:, 1], xz_w[:, 0], bc_w[:, 0], bc_w[:, 1], dt_w], dim=1)
        ref_model.in_proj.weight.data.copy_(zxBCdt_w.T)

        # conv1d <-> [x_conv, b_conv, c_conv]
        x_conv_w = _j2t(jax_params["x_conv"]["weight"])
        x_conv_bias = _j2t(jax_params["x_conv"]["bias"])
        b_conv_w = _j2t(jax_params["b_conv"]["weight"])
        b_conv_bias = _j2t(jax_params["b_conv"]["bias"])
        c_conv_w = _j2t(jax_params["c_conv"]["weight"])
        c_conv_bias = _j2t(jax_params["c_conv"]["bias"])
        xbc_conv_w = torch.cat([x_conv_w, b_conv_w, c_conv_w], dim=2)
        xbc_conv_bias = torch.cat([x_conv_bias, b_conv_bias, c_conv_bias], dim=0)
        ref_model.conv1d.weight.data.copy_(xbc_conv_w.T)
        ref_model.conv1d.bias.data.copy_(xbc_conv_bias)

        # out_proj <-> out_proj
        out_w = _j2t(jax_params["out_proj"]["weight"])
        ref_model.out_proj.weight.data.copy_(out_w.T)

        # A_log <-> llog_a
        a_w = _j2t(jax_params["llog_a"])  # [1, num_heads, 1]
        ref_model.A_log.data.copy_(a_w[0, :, 0])

        # dt_bias <-> dt_bias
        dt_bias = _j2t(jax_params["dt_bias"])
        ref_model.dt_bias.data.copy_(dt_bias)

        # D <-> d
        d = _j2t(jax_params["d"])  # [1, 1, num_heads, 1]
        ref_model.D.data.copy_(d[0, 0, :, 0])

        # norm <-> pre_out_proj_norm
        norm_scale = _j2t(jax_params["pre_out_proj_norm"]["scale"])
        ref_model.norm.weight.data.copy_(norm_scale)

        device = "cuda:0"
        ref_model = ref_model.to(device)
        inputs_torch = inputs_torch.to(device)
        torch_output = ref_model(inputs_torch)
        torch_output_np = torch_output.cpu().detach().numpy()

        assert_allclose(torch_output_np, jax_output_np, atol=1e-2, rtol=1e-2)
