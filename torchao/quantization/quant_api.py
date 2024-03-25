# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""
Quantization APIs

Generally these APIs can be applied directly to any model
with Linear modules to obtain quantized linear ops. The intended
usage involves applying torch.compile to the model afterwards
both because primitives were designed based on the fusions that
come along with it and because that is how we access the intended quantized
and mixed GEMM kernels
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

from .dynamic_quant import DynamicallyPerAxisQuantizedLinear
from .utils import TORCH_VERSION_AFTER_2_3

from .subclass import (
    Int4WeightOnlyQuantizedLinearWeight,
    Int8DynamicallyQuantizedLinearWeight,
    Int8WeightOnlyQuantizedLinearWeight,
    QuantizedLinearWeightBase,
)
from .weight_only import WeightOnlyInt8QuantLinear
from .unified import Quantizer, TwoStepQuantizer

_AFTER_TORCH_2_4_ONLY = [
    "Int8DynActInt4WeightQuantizer",
    "Int8DynActInt4WeightGPTQQuantizer",
]
from .quant_primitives import (
    get_group_qparams_symmetric,
    per_token_dynamic_quant,
)
from typing import Dict, Tuple
from .autoquant import AutoQuantizableLinearWeight, DEFAULT_CLASS_LIST

__all__ = [
    "apply_weight_only_int8_quant",
    "apply_dynamic_quant",
    "change_linear_weights_to_int8_dqtensors",
    "change_linear_weights_to_int8_woqtensors",
    "change_linear_weights_to_int4_woqtensors",
    "swap_conv2d_1x1_to_linear",
    "Quantizer",
    "TwoStepQuantizer",
]

if TORCH_VERSION_AFTER_2_3:
    from .GPTQ import (
        Int8DynActInt4WeightQuantizer,
        Int8DynActInt4WeightGPTQQuantizer,
        Int4WeightQuantizer,
        Int4WeightGPTQQuantizer,
    )
    __all__ += [
        "Int8DynActInt4WeightQuantizer",
        "Int8DynActInt4WeightGPTQQuantizer",
        "Int4WeightQuantizer",
        "Int4WeightGPTQQuantizer",
    ]
=======
    "autoquant",
    "change_linears_to_autoquantizable",
    "change_autoquantizable_to_quantized",
] + (_AFTER_TORCH_2_4_ONLY if TORCH_VERSION_AFTER_2_4 else [])

############################# Unified Quantization APIs ##############################
# API 1, single quantize call to create a quantized model with quantized state_dict
class Quantizer:
    def quantize(
        self, model: torch.nn.Module, *args: Any, **kwargs: Any
    ) -> torch.nn.Module:

        pass


# API 2, flow that needs calibration or training
class TwoStepQuantizer:
    def prepare(
        self, model: torch.nn.Module, *args: Any, **kwargs: Any
    ) -> torch.nn.Module:

        pass

    def convert(
        self, model: torch.nn.Module, *args: Any, **kwargs: Any
    ) -> torch.nn.Module:

        pass


############################# Unified Quantization APIs ##############################


def _replace_with_custom_fn_if_matches_filter(
    model,
    replacement_fn,
    filter_fn,
    cur_fqn="",
) -> None:
    """
    For each `child` in `model`, replaces it with `replacement_fn(child)`
    if `filter_fn(child)` is `True`
    """
    if filter_fn(model, cur_fqn[:-1]):
        model = replacement_fn(model)
        return model
    else:
        for name, child in model.named_children():
            new_child = _replace_with_custom_fn_if_matches_filter(
                child, replacement_fn, filter_fn, f"{cur_fqn}{name}."
            )
            if new_child is not child:
                setattr(model, name, new_child)
        return model


def _is_linear(mod, *args):
    return (
        isinstance(mod, torch.nn.Linear)
        and hasattr(mod, "weight")
        and not isinstance(mod.weight, QuantizedLinearWeightBase)
    )


def _in_features_greater_than_16(mod, *args):
    return hasattr(mod, "in_features") and mod.in_features > 16


def apply_weight_only_int8_quant(model, filter_fn=None):
    """
    Applies weight-only symmetric per-channel int8 quantization to all linear layers
    in the given model using module swaps.
    """
    _replace_with_custom_fn_if_matches_filter(
        model,
        WeightOnlyInt8QuantLinear.from_float,
        _is_linear if filter_fn is None else filter_fn,
    )


def apply_dynamic_quant(model, filter_fn=None):
    """
    Applies dynamic symmetric per-token activation and per-channel weight
    quantization to all linear layers by converting all linear weight
    tensors to the `Int8DynamicallyQuantizedLinearWeight` Tensor subclass.
    """
    change_linear_weights_to_int8_dqtensors(model, filter_fn)


def _get_subclass_inserter(cls, **kwargs):
    # pyre-fixme[53]: Captured variable `cls` is not annotated.
    # pyre-fixme[3]: Return type must be annotated.
    # pyre-fixme[2]: Parameter must be annotated.
    method = kwargs.pop("method", "from_float")
    def insert_subclass(lin):
        lin.weight = torch.nn.Parameter(
            # cls.from_float(...)
            getattr(cls, method)(lin.weight, **kwargs), requires_grad=False
        )
        return lin

    return insert_subclass


def change_linear_weights_to_int8_dqtensors(model, filter_fn=None):
    """
    Converts all linear weight tensors to the `Int8DynamicallyQuantizedLinearWeight`
    Tensor subclass, effectively applying the same form of quantization
    as apply_dynamic_quant while not modifying the linear modules.
    """
    if filter_fn is None:
        filter_fn = lambda *args: _is_linear(*args) and _in_features_greater_than_16(
            *args
        )

    _replace_with_custom_fn_if_matches_filter(
        model, _get_subclass_inserter(Int8DynamicallyQuantizedLinearWeight), filter_fn
    )


def change_linear_weights_to_int8_woqtensors(model, filter_fn=None):
    """
    Converts all linear weight tensors to the
    `Int8WeightOnlyQuantizedLinearWeight` tensor subclass,
    effectively applying the same form of quantization
    as apply_dynamic_quant while not modifying the linear modules.
    """
    _replace_with_custom_fn_if_matches_filter(
        model,
        _get_subclass_inserter(Int8WeightOnlyQuantizedLinearWeight),
        _is_linear if filter_fn is None else filter_fn,
    )


def change_linear_weights_to_int4_woqtensors(model, **kwargs):
    """
    Converts all linear weight tensors to the
    `Int4WeightOnlyQuantizedLinearWeight` tensor subclass,
    effectively applying the same form of quantization
    as apply_dynamic_quant while not modifying the linear modules.
    """
    filter_fn = kwargs.pop("filter_fn", _is_linear)

    _replace_with_custom_fn_if_matches_filter(
        model,
        _get_subclass_inserter(Int4WeightOnlyQuantizedLinearWeight, **kwargs),
        filter_fn,
    )

# pyre-fixme[3]: Return type must be annotated.
# pyre-fixme[2]: Parameter must be annotated.

def change_linears_to_autoquantizable(model, **kwargs):
    """
    Converts all linear weight tensors to the
    AutoQuantizableLinearWeight tensor subclass. Expectation is that this is followed
    by running the model and then calling change_autoquantizable_to_quantized
    """
    filter_fn = kwargs.pop("filter_fn", _is_linear)
    kwargs["qtensor_class_list"] = kwargs.get("qtensor_class_list", DEFAULT_CLASS_LIST)
    kwargs["mode"] = kwargs.get("mode", ["relu", None])
    _replace_with_custom_fn_if_matches_filter(
        model,
        _get_subclass_inserter(AutoQuantizableLinearWeight, **kwargs),
        filter_fn if filter_fn is not None else _is_linear,
    )

def change_autoquantizable_to_quantized(model, **kwargs):
    """
    Converts AutoQuantizableLinearWeight tensor subclasses
    to various quantized/non-quantized tensor subclasses depending
    on benchmark results. Expectation is that these modules are
    torch.compiled afterwards.
    """
    hold =  torch._dynamo.config.automatic_dynamic_shapes
    torch._dynamo.config.automatic_dynamic_shapes = False

    filter_fn = kwargs.pop(
        "filter_fn",
        lambda mod, *args:
            hasattr(mod, "weight") and isinstance(mod.weight, AutoQuantizableLinearWeight)
    )
    error_on_unseen=kwargs.pop("error_on_unseen", True)
    _replace_with_custom_fn_if_matches_filter(
        model,
        _get_subclass_inserter(
            AutoQuantizableLinearWeight, method="to_quantized", error_on_unseen=error_on_unseen, **kwargs
        ),
        filter_fn,
    )
    torch._dynamo.config.automatic_dynamic_shapes = hold
    torch._dynamo.reset()

@torch.no_grad()
def autoquant(model, example_input, qtensor_class_list=DEFAULT_CLASS_LIST, filter_fn=_is_linear, mode=["relu",None], **kwargs):
    """
    Runs the model with example_input to record shapes and then compares benchmark performance of the seen shape
    across the qtensor subclasses in qtensor_class_list. Determines best performing qtensor subclass for each layer
    and applies that type of quantization.
    """
    change_linears_to_autoquantizable(model, filter_fn=filter_fn, qtensor_class_list=qtensor_class_list, mode=mode, **kwargs)
    if not isinstance(example_input, (tuple, list)):
        assert isinstance(example_input, torch.Tensor)
        example_input = [example_input]
    model(*example_input)
    change_autoquantizable_to_quantized(model, **kwargs)
    return model

def swap_conv2d_1x1_to_linear(model, filter_fn=None):
    """
    Changes all conv2d 1x1 modules to equivalent linear modules so that they can then be quantized.
    """

    class PermuteSandwich(torch.nn.Module):
        def __init__(self, mod):
            super().__init__()
            self.mod = mod

        def forward(self, *args):
            return self.mod(args[0].permute(0, 2, 3, 1)).permute(-0, 3, 1, 2)

    def replace_conv2d_1x1(conv):
        assert conv.kernel_size == (1, 1)
        lin = torch.nn.Linear(
            conv.in_channels, conv.out_channels, bias=(conv.bias is None)
        )
        lin.weight = torch.nn.Parameter(conv.weight.squeeze(-1, -2))
        lin.bias = conv.bias
        return PermuteSandwich(lin)

    if filter_fn is None:
        filter_fn = lambda mod, *args: isinstance(
            mod, torch.nn.Conv2d
        ) and mod.kernel_size == (1, 1)

    _replace_with_custom_fn_if_matches_filter(
        model, replace_conv2d_1x1, filter_fn=filter_fn
    )
