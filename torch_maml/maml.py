from collections import namedtuple
from itertools import chain
import torch
import torch.nn as nn
from torch.utils.checkpoint import checkpoint

from .optimizers import IngraphGradientDescent
from .utils import copy_and_replace, do_not_copy, disable_batchnorm_stats


class NaiveMAML(nn.Module):
    Result = namedtuple('Result', ['model', 'loss_history', 'optimizer_state'])

    def __init__(self, model: nn.Module, loss_function: callable,
                 optimizer=IngraphGradientDescent(0.01),
                 get_parameters: callable=nn.Module.parameters):
        """
        MAML: attempts to change model by performing gradient descent steps
        :param model: a torch module that will be updated
        :param loss_function: objective function(model(inputs), targets) that is minimized inside MAML
        :param optimizer: in-graph optimizer that creates updated copies of model
        :param get_parameters: function(model) that returns a list of parameters affected by MAML updates
            Note: this function should always return parameters in the same order
        """
        super().__init__()
        self.model = model
        self.loss_function = loss_function
        self.optimizer = optimizer
        self.get_parameters = get_parameters

    def forward(self, inputs, opt_kwargs=None, loss_kwargs=None, optimizer_state=None, **kwargs):
        """
        Apply optimizer to the model (out-of-place) and return an updated copy
        :param inputs: data that is fed into the model
        :param opt_kwargs: optional overrides for optimizer.get_initial_state
        :param optimizer_state: if specified, the optimizer starts with this state
        :param kwargs: extra parameters passed to optimizer.step
        :returns: updated_model, loss_history, optimizer_state
            * updated_model: a copy of model that was trained for len(inputs) steps, differentiable w.r.t. original
            * loss_history: a list of loss function values BEFORE each optimizer update; differentiable
            * optimizer_state: final state of the chosen optimizer AFTER the last step; you guessed it, differentiable
        :rtype: MAML.Result
        """
        assert len(inputs) > 0, "Non-empty inputs are required"
        opt_kwargs, loss_kwargs = opt_kwargs or {}, loss_kwargs or {}
        if optimizer_state is None:
            optimizer_state = self.optimizer.get_initial_state(self, **opt_kwargs)
        updated_model = self.model

        loss_history = []
        for input in inputs:
            loss = self.loss_function(updated_model, input, **loss_kwargs)
            loss_history.append(loss)
            optimizer_state, updated_model = self.optimizer.step(
                optimizer_state, updated_model, loss, parameters=self.get_parameters(updated_model), **kwargs)

        return self.Result(updated_model, loss_history=loss_history, optimizer_state=optimizer_state)


class GradientCheckpointMAML(NaiveMAML):
    def __init__(self, *args, checkpoint_steps, **kwargs):
        """
        MAML: attempts to change model by performing gradient descent steps
        :param model: a torch module that will be updated
        :param loss_function: objective function(model(inputs), targets) that is minimized inside MAML
        :param optimizer: in-graph optimizer that creates updated copies of model
        :param checkpoint_steps: uses gradient checkpoints every *this many* steps
            Note: this parameter highly affects the memory footprint
        :param get_parameters: function(model) that returns a list of parameters affected by MAML updates
            Note: this function should always return parameters in the same order
        """
        super().__init__(*args, **kwargs)
        self.checkpoint_steps = checkpoint_steps

    def forward(self, inputs, opt_kwargs=None, loss_kwargs=None, optimizer_state=None, **kwargs):
        """
        Apply optimizer to the model (out-of-place) and return an updated copy
        :param inputs: data that is fed into the model
        :param opt_kwargs: optional overrides for optimizer.get_initial_state
        :param optimizer_state: if specified, the optimizer starts with this state
        :param kwargs: extra parameters passed to optimizer.step
        :returns: updated_model, loss_history, optimizer_state
            * updated_model: a copy of model that was trained for len(inputs) steps, differentiable w.r.t. original
            * loss_history: a list of loss function values BEFORE each optimizer update; differentiable
            * optimizer_state: final state of the chosen optimizer AFTER the last step; you guessed it, differentiable
        :rtype: GradientCheckpointMAML.Result
        """
        assert len(inputs) > 0, "Non-empty inputs are required"
        opt_kwargs, loss_kwargs = opt_kwargs or {}, loss_kwargs or {}
        if optimizer_state is None:
            optimizer_state = self.optimizer.get_initial_state(self, **opt_kwargs)

        parameters_to_copy = list(self.get_parameters(self.model))
        parameters_not_to_copy = [param for param in chain(self.model.parameters(), self.model.buffers())
                                  if param not in set(parameters_to_copy)]

        # WARNING: this code treats parameters_to_copy and parameters_not_to_copy as global
        # variables for _maml_internal. Please DO NOT change or delete them in this function
        def _maml_internal(step_index, steps, *trainable_parameters_and_state):
            trainable_parameters = trainable_parameters_and_state[:len(parameters_to_copy)]
            optimizer_state = self.optimizer.OptimizerState(*trainable_parameters_and_state[len(parameters_to_copy):])
            updated_model = copy_and_replace(
                self.model, dict(zip(parameters_to_copy, trainable_parameters)), parameters_not_to_copy)

            is_first_pass = not torch.is_grad_enabled()
            # Note: since we use gradient checkpoining, this code will be executed two times:
            # (1) initial forward with torch.no_grad(), used to create checkpoints
            # (2) second forward with torch.enable_grad() used to backpropagate from those checkpoints
            # During first pass, we deliberately set detach=True to avoid creating inter-checkpoint graph

            inner_losses = []
            for _ in range(int(steps)):
                with torch.enable_grad(), disable_batchnorm_stats(updated_model), do_not_copy(*parameters_not_to_copy):
                    loss = self.loss_function(updated_model, inputs[int(step_index)], **loss_kwargs)
                    inner_losses.append(loss)
                    optimizer_state, updated_model = self.optimizer.step(
                        optimizer_state, updated_model, loss=loss, detach=is_first_pass,
                        parameters=self.get_parameters(updated_model), **kwargs)

                step_index = step_index + 1
            return (step_index, torch.stack(inner_losses), *self.get_parameters(updated_model), *optimizer_state)

        step_index = torch.zeros(1, requires_grad=True)
        trainable_parameters_and_optimizer_state = list(chain(list(parameters_to_copy), optimizer_state))

        loss_history = []
        for chunk_start in range(0, len(inputs), self.checkpoint_steps):
            steps = min(self.checkpoint_steps, len(inputs) - chunk_start)
            step_index, inner_losses, *trainable_parameters_and_optimizer_state = checkpoint(
                _maml_internal, step_index, torch.as_tensor(steps), *trainable_parameters_and_optimizer_state)
            loss_history.extend(inner_losses.split(1))

        final_trainable_parameters = trainable_parameters_and_optimizer_state[:len(parameters_to_copy)]
        final_optimizer_state = self.optimizer.OptimizerState(
            *trainable_parameters_and_optimizer_state[len(parameters_to_copy):]
        )
        final_model = copy_and_replace(
            self.model, dict(zip(parameters_to_copy, final_trainable_parameters)), parameters_not_to_copy)
        return self.Result(final_model, loss_history=loss_history, optimizer_state=final_optimizer_state)