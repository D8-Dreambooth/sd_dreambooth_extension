#!/usr/bin/env python3

"""
This module has the EMA class used to store a copy of the exponentially decayed
model params.

Typical usage of EMA class involves initializing an object using an existing
model (random or from a seed model) and setting the config like ema_decay,
ema_start_update which determine how the EMA model is updated. After every
update of the model i.e. at the end of the train_step, the EMA should be updated
by passing the new model to the EMA.step function. The EMA model state dict
can be stored in the extra state under the key of "ema" and dumped
into a checkpoint and loaded. The EMA object can be passed to tasks
by setting task.uses_ema property.
EMA is a smoothed/ensemble model which might have better performance
when used for inference or further fine-tuning. EMA class has a
reverse function to load the EMA params into a model and use it
like a regular model.
"""

import copy
import logging
import os
import shutil

import torch


class EMAModel(object):

    def __init__(self, model, decay=0.9999, device=None):
        """
        @param model: model to initialize the EMA with
        @param decay: Decay rate to use
        @param device: If provided, copy EMA to this device (e.g. gpu), else EMA is in the same device as the model.
        """

        self.decay = decay
        self.model = copy.deepcopy(model)
        self.model.requires_grad_(False)
        self.fp32_params = {}

        if device is not None:
            logging.info(f"Copying EMA model to device {device}")
            self.model = self.model.to(device=device)

        self.build_fp32_params()

        self.update_freq_counter = 0


    def __call__(self, *args, **kwargs):
        return self.model(*args, **kwargs)
    def get_model(self):
        return self.model

    def build_fp32_params(self, state_dict=None):
        """
        Store a copy of the EMA params in fp32.
        If state dict is passed, the EMA params is copied from
        the provided state dict. Otherwise, it is copied from the
        current EMA model parameters.
        """
        if state_dict is None:
            state_dict = self.model.state_dict()

        def _to_float(t):
            return t.float() if torch.is_floating_point(t) else t

        # for non-float params (like registered symbols), they are copied into this dict and covered in each update
        for param_key in state_dict:
            if param_key in self.fp32_params:
                self.fp32_params[param_key].copy_(state_dict[param_key])
            else:
                self.fp32_params[param_key] = _to_float(state_dict[param_key])

    def load(self, state_dict, build_fp32_params=False):
        """ Load data from a state_dict """
        self.model.load_state_dict(state_dict, strict=False)
        if build_fp32_params:
            self.build_fp32_params(state_dict)

    def get_decay(self):
        return self.decay

    def step(self, new_model):
        """ One update of the EMA model based on new model weights """
        decay = self.decay

        ema_state_dict = {}
        ema_params = self.fp32_params
        for key, param in new_model.state_dict().items():
            try:
                ema_param = ema_params[key]
            except KeyError:
                ema_param = param.float().clone() if param.ndim == 1 else copy.deepcopy(param)

            if param.shape != ema_param.shape:
                raise ValueError(
                    "incompatible tensor shapes between model param and ema param"
                    + "{} vs. {}".format(param.shape, ema_param.shape)
                )
            if "version" in key:
                # Do not decay a model.version pytorch param
                continue

            # for non-float params (like registered symbols), they are covered in each update
            if not torch.is_floating_point(ema_param):
                if ema_param.dtype != param.dtype:
                    raise ValueError(
                        "incompatible tensor dtypes between model param and ema param"
                        + "{} vs. {}".format(param.dtype, ema_param.dtype)
                    )
                ema_param.copy_(param)
            else:
                ema_param.mul_(decay)
                ema_param.add_(param.to(dtype=ema_param.dtype), alpha=1-decay)
            ema_state_dict[key] = ema_param
        self.load(ema_state_dict, build_fp32_params=False)

    def apply(self, model):
        """
        Load the model parameters from EMA model.
        Useful for inference or fine-tuning from the EMA model.
        """
        model.load_state_dict(self.model.state_dict(), strict=False)
        return model

    def save_pretrained(self, model_path, save_safetensors=False):
        self.model.save_pretrained(model_path, save_safetensors)
        model_config_path = os.path.join(model_path, "config.json")
        if not os.path.exists(model_config_path):
            unet_config_path = model_config_path.replace("ema_", "")
            if os.path.exists(unet_config_path):
                shutil.copyfile(unet_config_path, model_config_path)

