# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

# Modifications copyright (C) 2024 Maksim Ploter
"""
Backbone modules.
"""
from collections import OrderedDict

import math
import torch
import torch.nn.functional as F
import torchvision
from torch import nn
from torchvision.models._utils import IntermediateLayerGetter
from typing import Dict, List

from util.misc import NestedTensor, is_main_process

from .position_encoding import build_position_encoding


class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt,
    without which any other models than torchvision.models.resnet[18,34,50,101]
    produce nans.
    """

    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias


class BackboneBase(nn.Module):

    def __init__(self, backbone: nn.Module, train_backbone: bool, num_channels: int, return_interm_layers: bool,
                 layers_used: set = None):
        super().__init__()
        if layers_used is None:
            layers_used = {'layer1', 'layer2', 'layer3', 'layer4'}
        for name, parameter in backbone.named_parameters():
            if not train_backbone or not any(layer in name for layer in layers_used):
                parameter.requires_grad_(False)
        if return_interm_layers:
            return_layers = {"layer1": "0", "layer2": "1", "layer3": "2", "layer4": "3"}
        else:
            return_layers = {'layer4': "0"}
        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers)
        self.num_channels = num_channels

    def forward(self, tensor_list: NestedTensor):
        xs = self.body(tensor_list.tensors)
        out: Dict[str, NestedTensor] = {}
        for name, x in xs.items():
            m = tensor_list.mask
            assert m is not None
            mask = F.interpolate(m[None].float(), size=x.shape[-2:]).to(torch.bool)[0]
            out[name] = NestedTensor(x, mask)
        return out


class Backbone(BackboneBase):
    """ResNet backbone with frozen BatchNorm."""
    def __init__(self, name: str,
                 train_backbone: bool,
                 return_interm_layers: bool,
                 dilation: bool,
                 layers_used: set[str] = None):
        backbone = getattr(torchvision.models, name)(
            replace_stride_with_dilation=[False, False, dilation],
            pretrained=is_main_process(), norm_layer=FrozenBatchNorm2d)
        num_channels = 512 if name in ('resnet18', 'resnet34') else 2048
        super().__init__(backbone, train_backbone, num_channels, return_interm_layers, layers_used)


class IntermediateLayerGetterBackbone(nn.Module):

    def __init__(self, layer: str, backbone: Backbone):
        super().__init__()
        self.layer = layer if layer is not None else '0'
        self.num_channels = [256, 512, 1024, 2048][int(layer)] if layer is not None else backbone.num_channels
        self.backbone = backbone

    def forward(self, tensor_list: NestedTensor):
        xs = self.backbone.forward(tensor_list)
        return xs[self.layer]

class Joiner(nn.Sequential):
    def __init__(self, backbone, position_embedding):
        super().__init__(backbone, position_embedding)

    def forward(self, tensor_list: NestedTensor):
        xs = self[0](tensor_list)
        out: List[NestedTensor] = []
        pos = []
        for name, x in xs.items():
            out.append(x)
            # position encoding
            pos.append(self[1](x).to(x.tensors.dtype))

        return out, pos


class NoBackbone(nn.Module):
    def __init__(self):
        super(NoBackbone, self).__init__()
        self.backbone = nn.Identity()
        self.num_channels = 3 #RGB

    def forward(self, tensor_list: NestedTensor):
        output = self.backbone(tensor_list)
        return output


# source: https://discuss.pytorch.org/t/tf-extract-image-patches-in-pytorch/43837/9
def extract_image_patches(x, kernel, stride=1, dilation=1):
    # Do TF 'SAME' Padding
    b, c, h, w = x.shape
    h2 = math.ceil(h / stride)
    w2 = math.ceil(w / stride)
    pad_row = (h2 - 1) * stride + (kernel - 1) * dilation + 1 - h
    pad_col = (w2 - 1) * stride + (kernel - 1) * dilation + 1 - w
    x = F.pad(x, (pad_row // 2, pad_row - pad_row // 2, pad_col // 2, pad_col - pad_col // 2))

    # Extract patches
    patches = x.unfold(2, kernel, stride).unfold(3, kernel, stride)
    patches = patches.permute(0, 4, 5, 1, 2, 3).contiguous()

    return patches.view(b, -1, patches.shape[-2], patches.shape[-1])


class PatchBackbone(nn.Module):
    def __init__(self, kernel=3, stride=1, dilation=1):
        super(PatchBackbone, self).__init__()
        self.kernel = kernel
        self.num_channels = (kernel ** 2) * 3
        self.stride = stride
        self.dilation = dilation

    def forward(self, tensor_list: NestedTensor):
        x = extract_image_patches(tensor_list.tensors, kernel=self.kernel, stride=self.stride, dilation=self.dilation)
        output = NestedTensor(x, tensor_list.mask)
        return output


def build_backbone(args):
    if args.model == 'detr':
        train_backbone = args.lr_backbone > 0
        return_interm_layers = args.masks
        backbone = Backbone(args.backbone, train_backbone, return_interm_layers, args.dilation)
        position_embedding = build_position_encoding(args)
        model = Joiner(backbone, position_embedding)
        model.num_channels = backbone.num_channels
        return model
    else:
        if args.backbone == 'n/a':
            # wrap in list to make compatible with Joiner (nn.Sequential) where first element is backbone
            backbone = NoBackbone()
            backbone.num_channels = 3  # RGB
            return backbone
        elif args.backbone == 'patch':
            backbone = PatchBackbone(
                kernel=args.patch_kernel,
                stride=args.patch_stride,
                dilation=args.patch_dilation
            )
            return backbone
        elif args.backbone == 'resnet50':
            train_backbone = args.lr_backbone > 0
            return_interm_layers = bool(args.interm_layer)
            layers_used = {f'layer{i}' for i in range(1, int(args.interm_layer) + 2)} if return_interm_layers else None
            backbone = Backbone(args.backbone, train_backbone, return_interm_layers, args.dilation,
                                layers_used=layers_used)
            # For perceiver models we return backbone's feature output from particular layer
            return IntermediateLayerGetterBackbone(layer=args.interm_layer, backbone=backbone)

    raise NotImplementedError('Backbone {} not implemented'.format(args.backbone))
