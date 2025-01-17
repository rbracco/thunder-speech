"""Basic building blocks to create the Citrinet model
"""

# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

# Copyright (c) 2021 scart97

# Original file: https://github.com/NVIDIA/NeMo/blob/main/nemo/collections/asr/parts/quartznet.py


__all__ = [
    "SqueezeExcite",
    "CitrinetBlock",
    "stem",
    "body",
    "Citrinet_encoder",
]

from dataclasses import dataclass
from typing import List

import torch
from torch import nn
from torch.nn.common_types import _size_1_t

from thunder.quartznet.blocks import (
    _get_act_dropout_layer,
    _get_conv_bn_layer,
    get_same_padding,
)


class SqueezeExcite(nn.Module):
    def __init__(
        self,
        channels: int,
        reduction_ratio: int,
    ):
        """
        Squeeze-and-Excitation sub-module.
        Args:
            channels: Input number of channels.
            reduction_ratio: Reduction ratio for "squeeze" layer.
        """
        super(SqueezeExcite, self).__init__()

        self.pool = nn.AdaptiveAvgPool1d(1)  # context window = T

        self.fc = nn.Sequential(
            nn.Linear(channels, channels // reduction_ratio, bias=False),
            nn.ReLU(True),
            nn.Linear(channels // reduction_ratio, channels, bias=False),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x : Tensor of shape [batch, channels, time]
        Returns:
            Tensor of shape [batch, channels, time]
        """
        y = self.pool(x)  # [B, C, T - context_window + 1]
        y = y.transpose(1, -1)  # [B, T - context_window + 1, C]
        y = self.fc(y)  # [B, T - context_window + 1, C]
        y = y.transpose(1, -1)  # [B, C, T - context_window + 1]
        y = torch.sigmoid(y)

        return x * y


class CitrinetBlock(nn.Module):
    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        repeat: int = 5,
        kernel_size: _size_1_t = (11,),
        stride: _size_1_t = (1,),
        dilation: _size_1_t = (1,),
        dropout: float = 0.0,
        residual: bool = True,
        separable: bool = False,
    ):
        """Citrinet block. This is a refactoring of the Jasperblock present on the NeMo toolkit,
        but simplified to only support the new citrinet model. Biggest change is that
        dense residual used on Jasper is not supported here.

        Args:
            in_channels : Number of input channels
            out_channels : Number of output channels
            repeat : Repetitions inside block.
            kernel_size : Kernel size.
            stride : Stride of each repetition.
            dilation : Dilation of each repetition.
            dropout : Dropout used before each activation.
            residual : Controls the use of residual connection.
            separable : Controls the use of separable convolutions.
        """
        super().__init__()

        padding_val = get_same_padding(kernel_size[0], 1, dilation[0])

        inplanes_loop = in_channels
        conv = []

        for _ in range(repeat - 1):

            conv.extend(
                _get_conv_bn_layer(
                    inplanes_loop,
                    out_channels,
                    kernel_size=kernel_size,
                    stride=(1,),  # Only stride the last one
                    dilation=dilation,
                    padding=padding_val,
                    separable=separable,
                    bias=False,
                )
            )

            conv.extend(_get_act_dropout_layer(drop_prob=dropout))

            inplanes_loop = out_channels

        padding_val = get_same_padding(kernel_size[0], stride[0], dilation[0])
        conv.extend(
            _get_conv_bn_layer(
                inplanes_loop,
                out_channels,
                kernel_size=kernel_size,
                stride=stride,
                dilation=dilation,
                padding=padding_val,
                separable=separable,
                bias=False,
            )
        )

        conv.append(SqueezeExcite(out_channels, reduction_ratio=8))

        self.mconv = nn.Sequential(*conv)

        if residual:
            stride_residual = stride if stride[0] == 1 else stride[0]

            self.res = nn.Sequential(
                *_get_conv_bn_layer(
                    in_channels,
                    out_channels,
                    kernel_size=1,
                    stride=stride_residual,
                    bias=False,
                )
            )
        else:
            self.res = None

        self.mout = nn.Sequential(*_get_act_dropout_layer(drop_prob=dropout))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Args:
            x : Tensor of shape (batch, features, time) where #features == inplanes

        Returns:
            Result of applying the block on the input, and corresponding output lengths
        """

        # compute forward convolutions
        out = self.mconv(x)

        # compute the residuals
        if self.res is not None:
            res_out = self.res(x)
            out = out + res_out

        # compute the output
        return self.mout(out)


def stem(feat_in: int) -> CitrinetBlock:
    """Creates the Citrinet stem. That is the first block of the model, that process the input directly.

    Args:
        feat_in : Number of input features

    Returns:
        Citrinet stem block
    """
    return CitrinetBlock(
        feat_in,
        256,
        repeat=1,
        kernel_size=(5,),
        residual=False,
        separable=True,
    )


def body(
    filters: List[int],
    kernel_size: List[int],
    strides: List[int],
) -> List[CitrinetBlock]:
    """Creates the body of the Citrinet model. That is the middle part.

    Args:
        filters : List of filters inside each block in the body.
        kernel_size : Corresponding list of kernel sizes for each block. Should have the same length as the first argument.
        strides: Corresponding list of strides for each block. Should have the same length as the first argument.

    Returns:
        List of layers that form the body of the network.
    """
    layers = []
    f_in = 256
    for f, k, s in zip(filters, kernel_size, strides):
        layers.append(
            CitrinetBlock(f_in, f, kernel_size=(k,), stride=(s,), separable=True)
        )
        f_in = f
    layers.append(
        CitrinetBlock(
            f_in,
            640,
            repeat=1,
            kernel_size=(41,),
            residual=False,
            separable=True,
        )
    )
    return layers


@dataclass
class EncoderConfig:
    """Configuration to create [`Citrinet_encoder`][thunder.citrinet.blocks.Citrinet_encoder]

    Attributes:
        filters: List of filter sizes used to create the encoder blocks. required.
        kernel_sizes: List of kernel sizes corresponding to each filter size. required.
        strides: List of stride corresponding to each filter size. required.
        feat_in : Number of input features to the model. defaults to 80.
    """

    filters: List[int]
    kernel_sizes: List[int]
    strides: List[int]
    feat_in: int = 80


def Citrinet_encoder(cfg: EncoderConfig) -> nn.Module:
    """Basic Citrinet encoder setup.

    Args:
        cfg: required config to create instance
    Returns:
        Pytorch model corresponding to the encoder.
    """
    return nn.Sequential(
        stem(cfg.feat_in),
        *body(cfg.filters, cfg.kernel_sizes, cfg.strides),
    )
