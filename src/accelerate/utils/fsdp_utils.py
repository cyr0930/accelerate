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
import os
import shutil
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path

import torch

from ..logging import get_logger
from .constants import FSDP_MODEL_NAME, OPTIMIZER_NAME, SAFE_WEIGHTS_NAME, WEIGHTS_NAME
from .modeling import is_peft_model
from .other import save
from .versions import is_torch_version


logger = get_logger(__name__)


def enable_fsdp_ram_efficient_loading():
    """
    Enables RAM efficient loading of Hugging Face models for FSDP in the environment.
    """
    # Sets values for `transformers.modeling_utils.is_fsdp_enabled`
    if "ACCELERATE_USE_FSDP" not in os.environ:
        os.environ["ACCELERATE_USE_FSDP"] = "True"
    os.environ["FSDP_CPU_RAM_EFFICIENT_LOADING"] = "True"


def disable_fsdp_ram_efficient_loading():
    """
    Disables RAM efficient loading of Hugging Face models for FSDP in the environment.
    """
    os.environ["FSDP_CPU_RAM_EFFICIENT_LOADING"] = "False"


def _get_model_state_dict(model, adapter_only=False):
    if adapter_only and is_peft_model(model):
        from peft import get_peft_model_state_dict

        return get_peft_model_state_dict(model, adapter_name=model.active_adapter)
    else:
        return model.state_dict()


def _set_model_state_dict(model, state_dict, adapter_only=False):
    if adapter_only and is_peft_model(model):
        from peft import set_peft_model_state_dict

        return set_peft_model_state_dict(model, state_dict, adapter_name=model.active_adapter)
    else:
        return model.load_state_dict(state_dict)


def save_fsdp_model(fsdp_plugin, accelerator, model, output_dir, model_index=0, adapter_only=False):
    # Note: We import here to reduce import time from general modules, and isolate outside dependencies
    import torch.distributed.checkpoint as dist_cp
    from torch.distributed.checkpoint.default_planner import DefaultSavePlanner
    from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.fully_sharded_data_parallel import StateDictType

    os.makedirs(output_dir, exist_ok=True)

    with nullcontext():
        state_dict = _get_model_state_dict(model, adapter_only=adapter_only)
        if fsdp_plugin.state_dict_type == StateDictType.FULL_STATE_DICT:
            weights_name = f"{FSDP_MODEL_NAME}.bin" if model_index == 0 else f"{FSDP_MODEL_NAME}_{model_index}.bin"
            output_model_file = os.path.join(output_dir, weights_name)
            with torch.no_grad():
                state_dict = {k: v.full_tensor() for k, v in state_dict.items()}
            if accelerator.process_index == 0:
                logger.info(f"Saving model to {output_model_file}")
                torch.save(state_dict, output_model_file)
                logger.info(f"Model saved to {output_model_file}")
        elif fsdp_plugin.state_dict_type == StateDictType.LOCAL_STATE_DICT:
            weights_name = (
                f"{FSDP_MODEL_NAME}_rank{accelerator.process_index}.bin"
                if model_index == 0
                else f"{FSDP_MODEL_NAME}_{model_index}_rank{accelerator.process_index}.bin"
            )
            output_model_file = os.path.join(output_dir, weights_name)
            logger.info(f"Saving model to {output_model_file}")
            torch.save(state_dict, output_model_file)
            logger.info(f"Model saved to {output_model_file}")
        elif fsdp_plugin.state_dict_type == StateDictType.SHARDED_STATE_DICT:
            ckpt_dir = os.path.join(output_dir, f"{FSDP_MODEL_NAME}_{model_index}")
            os.makedirs(ckpt_dir, exist_ok=True)
            logger.info(f"Saving model to {ckpt_dir}")
            state_dict = {"model": state_dict}

            dist_cp.save_state_dict(
                state_dict=state_dict,
                storage_writer=dist_cp.FileSystemWriter(ckpt_dir),
                planner=DefaultSavePlanner(),
            )
            logger.info(f"Model saved to {ckpt_dir}")


def load_fsdp_model(fsdp_plugin, accelerator, model, input_dir, model_index=0, adapter_only=False):
    # Note: We import here to reduce import time from general modules, and isolate outside dependencies
    import torch.distributed.checkpoint as dist_cp
    from torch.distributed.checkpoint.default_planner import DefaultLoadPlanner
    from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.fully_sharded_data_parallel import StateDictType

    accelerator.wait_for_everyone()
    with nullcontext():
        if fsdp_plugin.state_dict_type == StateDictType.FULL_STATE_DICT:
            if type(model) is not FSDP and accelerator.process_index != 0:
                return
            weights_name = f"{FSDP_MODEL_NAME}.bin" if model_index == 0 else f"{FSDP_MODEL_NAME}_{model_index}.bin"
            input_model_file = os.path.join(input_dir, weights_name)
            logger.info(f"Loading model from {input_model_file}")
            state_dict = torch.load(input_model_file)
            logger.info(f"Model loaded from {input_model_file}")
        elif fsdp_plugin.state_dict_type == StateDictType.LOCAL_STATE_DICT:
            weights_name = (
                f"{FSDP_MODEL_NAME}_rank{accelerator.process_index}.bin"
                if model_index == 0
                else f"{FSDP_MODEL_NAME}_{model_index}_rank{accelerator.process_index}.bin"
            )
            input_model_file = os.path.join(input_dir, weights_name)
            logger.info(f"Loading model from {input_model_file}")
            state_dict = torch.load(input_model_file)
            logger.info(f"Model loaded from {input_model_file}")
        elif fsdp_plugin.state_dict_type == StateDictType.SHARDED_STATE_DICT:
            ckpt_dir = (
                os.path.join(input_dir, f"{FSDP_MODEL_NAME}_{model_index}")
                if f"{FSDP_MODEL_NAME}" not in input_dir
                else input_dir
            )
            logger.info(f"Loading model from {ckpt_dir}")
            state_dict = {"model": _get_model_state_dict(model, adapter_only=adapter_only)}
            dist_cp.load_state_dict(
                state_dict=state_dict,
                storage_reader=dist_cp.FileSystemReader(ckpt_dir),
                planner=DefaultLoadPlanner(),
            )
            state_dict = state_dict["model"]
            logger.info(f"Model loaded from {ckpt_dir}")
        from torch.distributed.checkpoint.state_dict import set_model_state_dict
        load_result = set_model_state_dict(model, state_dict)
    return load_result


def save_fsdp_optimizer(fsdp_plugin, accelerator, optimizer, model, output_dir, optimizer_index=0):
    # Note: We import here to reduce import time from general modules, and isolate outside dependencies
    import torch.distributed.checkpoint as dist_cp
    from torch.distributed.checkpoint.default_planner import DefaultSavePlanner
    from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.fully_sharded_data_parallel import StateDictType

    os.makedirs(output_dir, exist_ok=True)
    with nullcontext():
        optim_state = optimizer.state_dict()
        if fsdp_plugin.state_dict_type == StateDictType.FULL_STATE_DICT:
            if accelerator.process_index == 0:
                optim_state_name = (
                    f"{OPTIMIZER_NAME}.bin" if optimizer_index == 0 else f"{OPTIMIZER_NAME}_{optimizer_index}.bin"
                )
                output_optimizer_file = os.path.join(output_dir, optim_state_name)
                logger.info(f"Saving Optimizer state to {output_optimizer_file}")
                torch.save(optim_state, output_optimizer_file)
                logger.info(f"Optimizer state saved in {output_optimizer_file}")
        else:
            ckpt_dir = os.path.join(output_dir, f"{OPTIMIZER_NAME}_{optimizer_index}")
            os.makedirs(ckpt_dir, exist_ok=True)
            logger.info(f"Saving Optimizer state to {ckpt_dir}")
            dist_cp.save_state_dict(
                state_dict={"optimizer": optim_state},
                storage_writer=dist_cp.FileSystemWriter(ckpt_dir),
                planner=DefaultSavePlanner(),
            )
            logger.info(f"Optimizer state saved in {ckpt_dir}")


def load_fsdp_optimizer(fsdp_plugin, accelerator, optimizer, model, input_dir, optimizer_index=0, adapter_only=False):
    # Note: We import here to reduce import time from general modules, and isolate outside dependencies
    import torch.distributed.checkpoint as dist_cp
    from torch.distributed.checkpoint.optimizer import load_sharded_optimizer_state_dict
    from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.fully_sharded_data_parallel import StateDictType

    accelerator.wait_for_everyone()
    with nullcontext():
        if fsdp_plugin.state_dict_type == StateDictType.FULL_STATE_DICT:
            optim_state = None
            if accelerator.process_index == 0 or not fsdp_plugin.optim_state_dict_config.rank0_only:
                optimizer_name = (
                    f"{OPTIMIZER_NAME}.bin" if optimizer_index == 0 else f"{OPTIMIZER_NAME}_{optimizer_index}.bin"
                )
                input_optimizer_file = os.path.join(input_dir, optimizer_name)
                logger.info(f"Loading Optimizer state from {input_optimizer_file}")
                optim_state = torch.load(input_optimizer_file)
                logger.info(f"Optimizer state loaded from {input_optimizer_file}")
        else:
            ckpt_dir = (
                os.path.join(input_dir, f"{OPTIMIZER_NAME}_{optimizer_index}")
                if f"{OPTIMIZER_NAME}" not in input_dir
                else input_dir
            )
            logger.info(f"Loading Optimizer from {ckpt_dir}")
            optim_state = {"optimizer": optimizer.state_dict()}
            dist_cp.load(
                optim_state,
                checkpoint_id=ckpt_dir,
                storage_reader=dist_cp.FileSystemReader(ckpt_dir),
            )
            optim_state = optim_state["optimizer"]
            logger.info(f"Optimizer loaded from {ckpt_dir}")
        optimizer.load_state_dict(optim_state)


def _distributed_checkpoint_to_merged_weights(checkpoint_dir: str, save_path: str, safe_serialization: bool = True):
    """
    Passthrough to `torch.distributed.checkpoint.format_utils.dcp_to_torch_save`

    Will save under `save_path` as either `model.safetensors` or `pytorch_model.bin`.
    """
    # Note: We import here to reduce import time from general modules, and isolate outside dependencies
    import torch.distributed.checkpoint as dist_cp
    import torch.distributed.checkpoint.format_utils as dist_cp_format_utils

    state_dict = {}
    save_path = Path(save_path)
    save_path.mkdir(exist_ok=True)
    dist_cp_format_utils._load_state_dict(
        state_dict,
        storage_reader=dist_cp.FileSystemReader(checkpoint_dir),
        planner=dist_cp_format_utils._EmptyStateDictLoadPlanner(),
        no_dist=True,
    )
    save_path = save_path / SAFE_WEIGHTS_NAME if safe_serialization else save_path / WEIGHTS_NAME

    # To handle if state is a dict like {model: {...}}
    if len(state_dict.keys()) == 1:
        state_dict = state_dict[list(state_dict)[0]]
    save(state_dict, save_path, safe_serialization=safe_serialization)
    return save_path


def merge_fsdp_weights(
    checkpoint_dir: str, output_path: str, safe_serialization: bool = True, remove_checkpoint_dir: bool = False
):
    """
    Merge the weights from sharded FSDP model checkpoints into a single combined checkpoint. Should be used if
    `SHARDED_STATE_DICT` was used for the model. Weights will be saved to `{output_path}/model.safetensors` if
    `safe_serialization` else `pytorch_model.bin`.

    Note: this is a CPU-bound process.

    Args:
        checkpoint_dir (`str`):
            The directory containing the FSDP checkpoints (can be either the model or optimizer).
        output_path (`str`):
            The path to save the merged checkpoint.
        safe_serialization (`bool`, *optional*, defaults to `True`):
            Whether to save the merged weights with safetensors (recommended).
        remove_checkpoint_dir (`bool`, *optional*, defaults to `False`):
            Whether to remove the checkpoint directory after merging.
    """
    checkpoint_dir = Path(checkpoint_dir)
    from accelerate.state import PartialState

    if not is_torch_version(">=", "2.3.0"):
        raise ValueError("`merge_fsdp_weights` requires PyTorch >= 2.3.0`")

    # Verify that the checkpoint directory exists
    if not checkpoint_dir.exists():
        model_path_exists = (checkpoint_dir / "pytorch_model_fsdp_0").exists()
        optimizer_path_exists = (checkpoint_dir / "optimizer_0").exists()
        err = f"Tried to load from {checkpoint_dir} but couldn't find a valid metadata file."
        if model_path_exists and optimizer_path_exists:
            err += " However, potential model and optimizer checkpoint directories exist."
            err += f"Please pass in either {checkpoint_dir}/pytorch_model_fsdp_0 or {checkpoint_dir}/optimizer_0"
            err += "instead."
        elif model_path_exists:
            err += " However, a potential model checkpoint directory exists."
            err += f"Please try passing in {checkpoint_dir}/pytorch_model_fsdp_0 instead."
        elif optimizer_path_exists:
            err += " However, a potential optimizer checkpoint directory exists."
            err += f"Please try passing in {checkpoint_dir}/optimizer_0 instead."
        raise ValueError(err)

    # To setup `save` to work
    state = PartialState()
    if state.is_main_process:
        logger.info(f"Merging FSDP weights from {checkpoint_dir}")
        save_path = _distributed_checkpoint_to_merged_weights(checkpoint_dir, output_path, safe_serialization)
        logger.info(f"Successfully merged FSDP weights and saved to {save_path}")
        if remove_checkpoint_dir:
            logger.info(f"Removing old checkpoint directory {checkpoint_dir}")
            shutil.rmtree(checkpoint_dir)
    state.wait_for_everyone()


def ensure_weights_retied(param_init_fn, model: torch.nn.Module, device: torch.cuda.device):
    _tied_names = getattr(model, "_tied_weights_keys", None)
    if not _tied_names:
        # if no tied names just passthrough
        return param_init_fn

    # get map of parameter instances to params.
    # - needed for replacement later
    _tied_params = {}
    for name in _tied_names:
        name = name.split(".")
        name, param_name = ".".join(name[:-1]), name[-1]
        mod = model.get_submodule(name)
        param = getattr(mod, param_name)

        _tied_params[id(param)] = None  # placeholder for the param first

    # build param_init_fn for the case with tied params
    def param_init_fn_tied_param(module: torch.nn.Module):
        # track which params to tie
        # - usually only 1, but for completeness consider > 1
        params_to_tie = defaultdict(list)
        for n, param in module.named_parameters(recurse=False):
            if id(param) in _tied_params:
                params_to_tie[id(param)].append(n)

        # call the param init fn, which potentially re-allocates the
        # parameters
        module = param_init_fn(module)

        # search the parameters again and tie them up again
        for id_key, _param_names in params_to_tie.items():
            for param_name in _param_names:
                param = _tied_params[id_key]
                if param is None:
                    # everything will be tied to the first time the
                    # param is observed
                    _tied_params[id_key] = getattr(module, param_name)
                else:
                    setattr(module, param_name, param)  # tie

        return module

    return param_init_fn_tied_param


def fsdp2_load_full_state_dict(accelerator, model: torch.nn.Module, full_sd: dict):
    import torch.distributed as dist
    from torch.distributed.tensor import distribute_tensor

    sharded_sd = model.state_dict()
    if accelerator.is_main_process:
        for (param_name, full_param), sharded_param in zip(full_sd.items(), sharded_sd.values()):
            full_param = full_param.detach().cuda()
            mesh = sharded_param.device_mesh
            dist.broadcast(full_param, src=0, group=mesh.get_group())
            sharded_tensor = distribute_tensor(full_param, mesh, sharded_param.placements)
            sharded_sd[param_name] = sharded_tensor
    else:
        for param_name, sharded_param in sharded_sd.items():
            full_tensor = torch.empty(sharded_param.size(), device="cuda", dtype=sharded_param.dtype)
            mesh = sharded_param.device_mesh
            dist.broadcast(full_tensor, src=0, group=mesh.get_group())
            sharded_tensor = distribute_tensor(full_tensor, mesh, sharded_param.placements)
            sharded_sd[param_name] = sharded_tensor

    model.load_state_dict(sharded_sd)


def fsdp2_prepare_model(accelerator, model: torch.nn.Module) -> torch.nn.Module:
    from torch.distributed._composable.fsdp import fully_shard, MixedPrecisionPolicy, CPUOffloadPolicy, FSDPModule, OffloadPolicy
    is_type_fsdp = isinstance(model, FSDPModule) or (
        is_compiled_module(model) and isinstance(model._orig_mod, FSDPModule)
    )

    if not is_type_fsdp:
        fsdp_plugin = accelerator.state.fsdp_plugin
        fsdp_plugin.set_auto_wrap_policy(model)

        kwargs = {
            "reshard_after_forward": fsdp_plugin.reshard_after_forward,
            "cpu_offload": fsdp_plugin.cpu_offload,
            "mixed_precision": fsdp_plugin.mixed_precision_policy,
            "forward_prefetch": fsdp_plugin.forward_prefetch,
            "ignored_modules": fsdp_plugin.ignored_modules,
        }                    

        fsdp2_kwargs = {
            "reshard_after_forward": kwargs["reshard_after_forward"],
            "mesh": None,
            "mp_policy": MixedPrecisionPolicy(),
            "offload_policy": OffloadPolicy(),
        }

        if kwargs["mixed_precision"] is not None:
            fsdp2_kwargs["mp_policy"] = MixedPrecisionPolicy(
                param_dtype=kwargs["mixed_precision"].param_dtype,
                reduce_dtype=kwargs["mixed_precision"].reduce_dtype,
                cast_forward_inputs=kwargs["mixed_precision"].cast_forward_inputs,
            )

        if kwargs["cpu_offload"] is not None and kwargs["cpu_offload"].offload_params:
            fsdp2_kwargs["offload_policy"] = CPUOffloadPolicy()

        fully_shard(model.model.vision_tower.vision_tower.vision_model.embeddings, **fsdp2_kwargs)
        for module in model.model.vision_tower.vision_tower.vision_model.encoder.layers:
            fully_shard(module, **fsdp2_kwargs)
        fully_shard(model.model.mm_projector, **fsdp2_kwargs)
        # fully_shard(model.model.embed_tokens, **fsdp2_kwargs)
        for module in model.model.layers:
            fully_shard(module, **fsdp2_kwargs)
        fully_shard(model.lm_head, **fsdp2_kwargs)
        fully_shard(model, **fsdp2_kwargs)

        if fsdp_plugin.activation_checkpointing:
            from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
                CheckpointImpl,
                apply_activation_checkpointing,
                checkpoint_wrapper,
            )

            apply_activation_checkpointing(
                model,
                checkpoint_wrapper_fn=functools.partial(
                    checkpoint_wrapper,
                    checkpoint_impl=CheckpointImpl.NO_REENTRANT,
                ),
                auto_wrap_policy=fsdp_plugin.auto_wrap_policy,
            )
    return model
