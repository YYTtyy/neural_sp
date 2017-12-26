#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Hierarchical RNN encoders."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from models.pytorch.linear import LinearND
from models.pytorch.encoders.rnn_utils import _init_hidden
# from models.pytorch.encoders.cnn import CNNEncoder
from models.pytorch.encoders.cnn_v2 import CNNEncoder
from models.pytorch.encoders.cnn_utils import ConvOutSize
from utils.io.variable import var2np


class HierarchicalRNNEncoder(nn.Module):
    """Hierarchical RNN encoder.
    Args:
        input_size (int): the dimension of input features
        rnn_type (string): lstm or gru or rnn
        bidirectional (bool): if True, use the bidirectional encoder
        num_units (int): the number of units in each layer
        num_proj (int): the number of nodes in the projection layer
        num_layers (int): the number of layers in the main task
        num_layers_sub (int): the number of layers in the sub task
        dropout (float): the probability to drop nodes
        parameter_init (float): the range of uniform distribution to
            initialize weight parameters (>= 0)
        use_cuda (bool, optional): if True, use GPUs
        batch_first (bool, optional): if True, batch-major computation will be
            performed
        merge_bidirectional (bool, optional): if True, sum bidirectional outputs
        num_stack (int, optional): the number of frames to stack
        splice (int, optional): frames to splice. Default is 1 frame.
        conv_channels (list, optional):
        conv_kernel_sizes (list, optional):
        conv_strides (list, optional):
        poolings (list, optional):
        activation (string, optional): The activation function of CNN layers.
            Choose from relu or prelu or hard_tanh or maxout
        batch_norm (bool, optional):
        residual (bool, optional):
        dense_residual (bool, optional):
    """

    def __init__(self,
                 input_size,
                 rnn_type,
                 bidirectional,
                 num_units,
                 num_proj,
                 num_layers,
                 num_layers_sub,
                 dropout,
                 parameter_init,
                 use_cuda=False,
                 batch_first=False,
                 merge_bidirectional=False,
                 num_stack=1,
                 splice=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 activation='relu',
                 batch_norm=False,
                 residual=False,
                 dense_residual=False):

        super(HierarchicalRNNEncoder, self).__init__()

        if num_layers_sub < 1 or num_layers < num_layers_sub:
            raise ValueError(
                'Set num_layers_sub between 1 to num_layers.')

        self.rnn_type = rnn_type
        self.bidirectional = bidirectional
        self.num_directions = 2 if bidirectional else 1
        self.num_units = num_units
        self.num_proj = num_proj if num_proj is not None else 0
        self.num_layers = num_layers
        self.num_layers_sub = num_layers_sub
        self.use_cuda = use_cuda
        self.batch_first = batch_first
        self.merge_bidirectional = merge_bidirectional
        assert not (residual and dense_residual)
        self.residual = residual
        self.dense_residual = dense_residual

        # Setting for CNNs before RNNs
        if len(conv_channels) > 0 and len(conv_channels) == len(conv_kernel_sizes) and len(conv_kernel_sizes) == len(conv_strides):
            assert num_stack == 1
            assert splice == 1
            self.conv = CNNEncoder(
                input_size,
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                dropout=dropout,
                parameter_init=parameter_init,
                activation=activation,
                use_cuda=use_cuda,
                batch_norm=batch_norm)
            input_size = self.conv.output_size
            self.conv_out_size = ConvOutSize(self.conv.conv)
        else:
            input_size = input_size * splice * num_stack
            self.conv = None

        self.rnns = []
        self.projections = []
        for i_layer in range(num_layers):
            if i_layer == 0:
                encoder_input_size = input_size
            elif self.num_proj > 0:
                encoder_input_size = num_proj
            else:
                encoder_input_size = num_units * self.num_directions

            if rnn_type == 'lstm':
                rnn_i = nn.LSTM(encoder_input_size,
                                hidden_size=num_units,
                                num_layers=1,
                                bias=True,
                                batch_first=batch_first,
                                dropout=dropout,
                                bidirectional=bidirectional)
            elif rnn_type == 'gru':
                rnn_i = nn.GRU(encoder_input_size,
                               hidden_size=num_units,
                               num_layers=1,
                               bias=True,
                               batch_first=batch_first,
                               dropout=dropout,
                               bidirectional=bidirectional)
            elif rnn_type == 'rnn':
                rnn_i = nn.RNN(encoder_input_size,
                               hidden_size=num_units,
                               num_layers=1,
                               bias=True,
                               batch_first=batch_first,
                               dropout=dropout,
                               bidirectional=bidirectional)
            else:
                raise ValueError('rnn_type must be "lstm" or "gru" or "rnn".')

            setattr(self, rnn_type + '_l' + str(i_layer), rnn_i)
            if use_cuda:
                rnn_i = rnn_i.cuda()
            self.rnns.append(rnn_i)

            if self.num_proj > 0:
                proj_i = LinearND(num_units * self.num_directions, num_proj)
                setattr(self, 'proj_l' + str(i_layer), proj_i)
                if use_cuda:
                    proj_i = proj_i.cuda()
                self.projections.append(proj_i)

    def forward(self, inputs, inputs_seq_len, volatile=True):
        """Forward computation.
        Args:
            inputs: A tensor of size `[B, T, input_size]`
            inputs_seq_len (IntTensor or LongTensor): A tensor of size `[B]`
            volatile (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            outputs:
                if batch_first is True, a tensor of size
                    `[B, T, num_units (* num_directions)]`
                else
                    `[T, B, num_units (* num_directions)]`
            final_state_fw: A tensor of size `[1, B, num_units]`
            outputs_sub:
                if batch_first is True, a tensor of size
                    `[B, T, num_units (* num_directions)]`
                else
                    `[T, B, num_units (* num_directions)]`
            final_state_fw_sub: A tensor of size `[1, B, num_units]`
            perm_indices ():
        """
        # Initialize hidden states (and memory cells) per mini-batch
        h_0 = _init_hidden(batch_size=inputs.size(0),
                           rnn_type=self.rnn_type,
                           num_units=self.num_units,
                           num_directions=self.num_directions,
                           num_layers=1,
                           use_cuda=self.use_cuda,
                           volatile=volatile)

        # Sort inputs by lengths in descending order
        inputs_seq_len, perm_indices = inputs_seq_len.sort(
            dim=0, descending=True)
        inputs = inputs[perm_indices]

        # Path through CNN layers before RNN layers
        if self.conv is not None:
            inputs = self.conv(inputs)

        if not self.batch_first:
            # Reshape to the time-major
            inputs = inputs.transpose(0, 1).contiguous()

        if not isinstance(inputs_seq_len, list):
            inputs_seq_len = var2np(inputs_seq_len).tolist()

        # Modify inputs_seq_len for reducing time resolution by CNN layers
        if self.conv is not None:
            inputs_seq_len = [self.conv_out_size(x, 1) for x in inputs_seq_len]

        # Pack encoder inputs
        inputs = pack_padded_sequence(
            inputs, inputs_seq_len, batch_first=self.batch_first)

        outputs = inputs
        res_outputs_list = []
        # NOTE: exclude residual connection from inputs
        for i_layer in range(self.num_layers):
            if self.rnn_type == 'lstm':
                outputs, (h_n, _) = self.rnns[i_layer](outputs, hx=h_0)
            else:
                outputs, h_n = self.rnns[i_layer](outputs, hx=h_0)

            if self.residual or self.dense_residual or self.num_proj > 0:
                # Unpack encoder outputs
                outputs, unpacked_seq_len = pad_packed_sequence(
                    outputs, batch_first=self.batch_first,
                    padding_value=0.0)
                assert inputs_seq_len == unpacked_seq_len

                # Projection layer (affine transformation)
                if self.num_proj > 0 and i_layer != self.num_layers - 1:
                    outputs = self.projections[i_layer](outputs)
                # NOTE: Exclude the last layer

                # Residual connection
                if self.residual or self.dense_residual:
                    for outputs_lower in res_outputs_list:
                        outputs = outputs + outputs_lower
                    if self.residual:
                        res_outputs_list = [outputs]
                    elif self.dense_residual:
                        res_outputs_list.append(outputs)

                # Pack encoder outputs again
                outputs = pack_padded_sequence(
                    outputs, unpacked_seq_len,
                    batch_first=self.batch_first)

            if i_layer == self.num_layers_sub - 1:
                outputs_sub = outputs
                h_n_sub = h_n

        # Unpack encoder outputs
        outputs, unpacked_seq_len = pad_packed_sequence(
            outputs, batch_first=self.batch_first, padding_value=0.0)
        outputs_sub, unpacked_seq_len_sub = pad_packed_sequence(
            outputs_sub, batch_first=self.batch_first, padding_value=0.0)
        assert inputs_seq_len == unpacked_seq_len
        assert inputs_seq_len == unpacked_seq_len_sub

        # Sum bidirectional outputs
        if self.bidirectional and self.merge_bidirectional:
            outputs = outputs[:, :, :self.num_units] + \
                outputs[:, :, self.num_units:]
            outputs_sub = outputs_sub[:, :, :self.num_units] + \
                outputs_sub[:, :, self.num_units:]

        # Pick up the final state of the top layer (forward)
        if self.num_directions == 2:
            final_state_fw = h_n[-2:-1, :, :]
            final_state_fw_sub = h_n_sub[-2:-1, :, :]
        else:
            final_state_fw = h_n[-1, :, :].unsqueeze(dim=0)
            final_state_fw_sub = h_n_sub[-1, :, :].unsqueeze(dim=0)
        # NOTE: h_n: `[num_layers * num_directions, B, num_units]`
        #   h_n_sub: `[num_layers_sub * num_directions, B, num_units]`

        del h_n, h_n_sub

        return outputs, final_state_fw, outputs_sub, final_state_fw_sub, perm_indices
