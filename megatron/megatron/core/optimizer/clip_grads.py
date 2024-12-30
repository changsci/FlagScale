# Copyright (c) 2024, NVIDIA CORPORATION. All rights reserved.

"""Gradient clipping."""

from typing import List, Optional, Union

import torch
from torch import inf

try:
    from transformer_engine.pytorch.optimizers import (
        multi_tensor_applier,
        multi_tensor_l2norm,
        multi_tensor_scale,
    )

    l2_norm_impl = multi_tensor_l2norm
    multi_tensor_scale_impl = multi_tensor_scale
except ImportError:
    try:
        import amp_C
        from apex.multi_tensor_apply import multi_tensor_applier

        l2_norm_impl = amp_C.multi_tensor_l2norm
        multi_tensor_scale_impl = amp_C.multi_tensor_scale
    except ImportError:
        import warnings

        warnings.warn(
            f'Transformer Engine and Apex are not installed. '
            'Falling back to local implementations of multi_tensor_applier, '
            'multi_tensor_l2norm, and multi_tensor_scale'
        )

        from megatron.core.utils import (
            local_multi_tensor_applier,
            local_multi_tensor_l2_norm,
            local_multi_tensor_scale,
        )

        multi_tensor_applier = local_multi_tensor_applier
        l2_norm_impl = local_multi_tensor_l2_norm
        multi_tensor_scale_impl = local_multi_tensor_scale


from ..tensor_parallel import param_is_not_tensor_parallel_duplicate
from ..transformer.module import param_is_not_shared
from ..utils import get_data_parallel_group_if_dtensor, to_local_if_dtensor

from flagscale.train.hetero.p2p_communication import get_device_type_for_comm

def get_grad_norm_fp32(
    grads_for_norm: Union[List[torch.Tensor], torch.Tensor],
    norm_type: Union[int, float] = 2,
    grad_stats_parallel_group: Optional[torch.distributed.ProcessGroup] = None,
) -> float:
    """Calculate the norm of gradients in fp32.

    This is adapted from torch.nn.utils.clip_grad.clip_grad_norm_ and
    added functionality to handle model parallel parameters.

    Arguments:
        grads_for_norm (Iterable[Tensor] or Tensor): an iterable of Tensors or a single
            Tensor that will be used for calculating the grad norm.
        norm_type (float or int): type of the used p-norm. Can be ``'inf'`` for
            infinity norm.
        grad_stats_parallel_group (group): Process group for reducing the grad norms. This is
            generally the model-parallel group for non-distributed optimizers, and the entire
            world for the distributed optimizer.

    Returns:
        Total norm of the parameters (viewed as a single vector).
    """

    if isinstance(grads_for_norm, torch.Tensor):
        grads_for_norm = [grads_for_norm]

    data_parallel_group = None
    for grad in grads_for_norm:
        data_parallel_group = get_data_parallel_group_if_dtensor(grad, data_parallel_group)

    grads_for_norm = [to_local_if_dtensor(grad) for grad in grads_for_norm]

    # Norm parameters.
    norm_type = float(norm_type)
    total_norm = 0.0

    # Calculate norm.
    if norm_type == inf:
        total_norm = max(grad.abs().max() for grad in grads_for_norm)
        # For cpu comminication
        tensor_device = get_device_type_for_comm(grad_stats_parallel_group)
        total_norm_cuda = torch.tensor([float(total_norm)], dtype=torch.float, device=tensor_device)
        # Take max across all data-parallel GPUs if using FSDP and then all model-parallel GPUs.
        if data_parallel_group:
            torch.distributed.all_reduce(
                total_norm_cuda, op=torch.distributed.ReduceOp.MAX, group=data_parallel_group
            )
        # Take max across all model-parallel GPUs.
        if isinstance(grad_stats_parallel_group, list):
            for group in grad_stats_parallel_group:
                torch.distributed.all_reduce(
                    total_norm_cuda, op=torch.distributed.ReduceOp.MAX, group=group
                )
        else:
            torch.distributed.all_reduce(
                total_norm_cuda, op=torch.distributed.ReduceOp.MAX, group=grad_stats_parallel_group
            )
        total_norm = total_norm_cuda[0].item()

    else:
        if norm_type == 2.0:
            dummy_overflow_buf = torch.tensor([0], dtype=torch.int, device='cuda')
            # Use apex's multi-tensor applier for efficiency reasons.
            # Multi-tensor applier takes a function and a list of list
            # and performs the operation on that list all in one kernel.
            if grads_for_norm:
                grad_norm, _ = multi_tensor_applier(
                    l2_norm_impl,
                    dummy_overflow_buf,
                    [grads_for_norm],
                    False,  # no per-parameter norm
                )
            else:
                grad_norm = torch.tensor([0], dtype=torch.float, device='cuda')
            # Since we will be summing across data parallel groups,
            # we need the pow(norm-type).
            total_norm = grad_norm**norm_type

        else:
            for grad in grads_for_norm:
                grad_norm = torch.norm(grad, norm_type)
                total_norm += grad_norm**norm_type

        # Sum across all data-parallel GPUs if using FSDP and then all model-parallel GPUs.
        if data_parallel_group:
            torch.distributed.all_reduce(
                total_norm, op=torch.distributed.ReduceOp.SUM, group=data_parallel_group
            )
        # Sum across all model-parallel GPUs.
        # For cpu comminication
        tensor_device = get_device_type_for_comm(grad_stats_parallel_group)
        if isinstance(grad_stats_parallel_group, list):
            original_total_norm = total_norm.clone().detach()
            for mp_group in grad_stats_parallel_group:
                total_norm.data = original_total_norm.data.clone()
                total_norm = total_norm.to(tensor_device)
                torch.distributed.all_reduce(
                    total_norm, op=torch.distributed.ReduceOp.SUM, group=group
                )
        else:
            total_norm = total_norm.to(tensor_device)
            torch.distributed.all_reduce(
                total_norm, op=torch.distributed.ReduceOp.SUM, group=grad_stats_parallel_group
            )
        total_norm = total_norm.item() ** (1.0 / norm_type)

    return total_norm


def clip_grad_by_total_norm_fp32(
    parameters: Union[List[torch.Tensor], torch.Tensor],
    max_norm: Union[int, float],
    total_norm: float,
):
    """Clips gradient of an iterable of parameters in fp32 by total norm.

    Note that the gradients are modified in place.

    Args:
        parameters (Iterable[Tensor] or Tensor): an iterable of Tensors or a
            single Tensor that will have gradients normalized.
        max_norm (float or int): max norm of the gradients.
        total_norm (float): total norm of the gradients.
    """
    # Grads.
    params = []
    grads = []
    for param in parameters:
        if param.grad is not None:
            assert param.grad.type() == 'torch.cuda.FloatTensor'
            params.append(param)
            grads.append(to_local_if_dtensor(param.grad).detach())

    # Scale.
    clip_coeff = max_norm / (total_norm + 1.0e-6)
    if clip_coeff < 1.0:
        dummy_overflow_buf = torch.tensor([0], dtype=torch.int, device='cuda')
        multi_tensor_applier(
            multi_tensor_scale_impl, dummy_overflow_buf, [grads, grads], clip_coeff
        )


def count_zeros_fp32(
    parameters: Union[List[torch.Tensor], torch.Tensor],
    grad_stats_parallel_group: torch.distributed.ProcessGroup,
) -> float:
    """Counts the number of zeros in gradients associated with the passed-in list of
    parameters.

    Args:
        parameters (Iterable[Tensor] or Tensor): an iterable of Tensors or a
            single Tensor that will have the number of zeros in its corresponding
            gradient counted.
        grad_stats_parallel_group (group): Process group for reducing the num_zeros count. This is
            generally the model-parallel group for non-distributed optimizers, and the entire
            world for the distributed optimizer.
    """

    if isinstance(parameters, torch.Tensor):
        parameters = [parameters]

    # Filter parameters based on:
    #   - grad should not be none
    #   - parameter should not be shared
    #   - should not be a replica due to tensor model parallelism
    comm_device = get_device_type_for_comm(grad_stats_parallel_group)
    total_num_zeros = torch.tensor([0.0], dtype=torch.float, device=comm_device)
    data_parallel_group = None
    for param in parameters:
        grad_not_none = param.grad is not None
        is_not_shared = param_is_not_shared(param)
        is_not_tp_duplicate = param_is_not_tensor_parallel_duplicate(param)
        if grad_not_none and is_not_shared and is_not_tp_duplicate:
            data_parallel_group = get_data_parallel_group_if_dtensor(
                param.grad, data_parallel_group
            )
            grad = to_local_if_dtensor(param.grad).detach()
            num_zeros = grad.numel() - torch.count_nonzero(grad)
            total_num_zeros = num_zeros + total_num_zeros

    # Sum across all data-parallel GPUs if using FSDP.
    if data_parallel_group:
        torch.distributed.all_reduce(
            total_num_zeros, op=torch.distributed.ReduceOp.SUM, group=data_parallel_group
        )
    # Sum across all model-parallel GPUs.
    if isinstance(grad_stats_parallel_group, list):
        original_total_num_zeros = total_num_zeros.clone().detach()
        for group in grad_stats_parallel_group:
            total_num_zeros.data = original_total_num_zeros.data.clone()
            torch.distributed.all_reduce(
                total_num_zeros, op=torch.distributed.ReduceOp.SUM, group=group
            )
    else:
        torch.distributed.all_reduce(
            total_num_zeros, op=torch.distributed.ReduceOp.SUM, group=grad_stats_parallel_group
        )

    total_num_zeros = total_num_zeros.item()

    return total_num_zeros
