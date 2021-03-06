from typing import Dict
import time

import torch
from torch.utils.data import DataLoader

import numpy as np

from ..adv_trainer import ADVTrainer
from ..mixins import InitializeTensorboardMixin
from src.utils import logger
from src.networks import SupportedAllModuleType, make_blocks

class RobustPlusFeatureMatchingTrainer(ADVTrainer, InitializeTensorboardMixin):
    def __init__(self, k: int, _lambda: float, model: SupportedAllModuleType, train_loader: DataLoader,
                 test_loader: DataLoader, attacker, params: Dict, checkpoint_path: str = None):
        super().__init__(model, train_loader, test_loader,
                         attacker, params, checkpoint_path)
        self._blocks = make_blocks(model)
        self._register_forward_hook_to_k_block(k)
        self._hooked_features_list = []
        self._lambda = _lambda

        self.summary_writer = self.init_writer()

    def step_batch(self, inputs: torch.Tensor, labels: torch.Tensor):
        inputs, labels = inputs.to(self._device), labels.to(self._device)

        # here we freeze parameters for speedup
        self._freeze_trainable_parameters()
        adv_inputs = self._gen_adv(inputs.detach().clone(), labels)
        self._unfreeze_trainable_parameters()

        adv_outputs = self.model(adv_inputs)
        clean_outputs = self.model(inputs)

        r_adv = self._hooked_features_list[0] #type:torch.Tensor
        r_clean = self._hooked_features_list[1] #type:torch.Tensor

        # The regularization term is
        #           || E(f_k(x)) - E(f_k(x') ||^2_2.
        # The idea is from feature matching.
        regularization_term = (r_adv.mean(dim=0) - r_clean.mean(dim=0)).flatten().norm(p=2) ** 2 

        self._hooked_features_list.clear()

        l_term = self.criterion(adv_outputs, labels)

        loss = l_term + regularization_term * self._lambda

        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        batch_training_acc = (clean_outputs.argmax(dim=1) == labels).float().mean().item()
        batch_robust_acc = (adv_outputs.argmax(dim=1) == labels).float().mean().item()
        self._robust_acc += batch_robust_acc
        batch_running_loss = loss.item()

        return batch_running_loss, batch_training_acc, regularization_term.item(), l_term.item()

    def train(self, save_path):
        batch_number = len(self._train_loader)
        best_robustness = self.best_acc
        start_epoch = self.start_epoch

        logger.info(f"starting epoch: {start_epoch}")
        logger.info(f"start lr: {self.current_lr}")
        logger.info(f"best robustness: {best_robustness}")

        for ep in range(start_epoch, self._train_epochs + 1):
            self._adjust_lr(ep)

            # show current learning rate
            logger.debug(f"lr: {self.current_lr}")

            training_acc, running_loss = 0, .0
            reg_loss, ce_loss = 0., 0.
            # record current robustness
            self._robust_acc = 0

            start_time = time.perf_counter()

            for index, data in enumerate(self._train_loader):
                batch_running_loss, batch_training_acc, batch_reg_loss, batch_ce_loss = self.step_batch(data[0], data[1])

                training_acc += batch_training_acc
                running_loss += batch_running_loss
                reg_loss += batch_reg_loss
                ce_loss += batch_ce_loss

                # warm up learning rate
                if ep <= self._warm_up_epochs:
                    self.warm_up_scheduler.step()

                if index % batch_number == batch_number - 1:
                    end_time = time.perf_counter()

                    acc = self.test()
                    average_train_loss = (running_loss / batch_number)
                    average_train_accuracy = training_acc / batch_number
                    average_robust_accuracy = self._robust_acc / batch_number
                    epoch_cost_time = end_time - start_time

                    # write loss, time, test_acc, train_acc to tensorboard
                    if hasattr(self, "summary_writer"):
                        self.summary_writer.add_scalar("train loss", average_train_loss, ep)
                        self.summary_writer.add_scalar("reg loss", reg_loss / batch_number, ep)
                        self.summary_writer.add_scalar("CE loss", ce_loss / batch_number, ep)
                        self.summary_writer.add_scalar("train accuracy", average_train_accuracy, ep)
                        self.summary_writer.add_scalar("test accuracy", acc, ep)
                        self.summary_writer.add_scalar("time per epoch", epoch_cost_time, ep)
                        self.summary_writer.add_scalar("best robustness", average_robust_accuracy, ep)

                    logger.info(
                        f"epoch: {ep}   loss: {average_train_loss:.6f}   train accuracy: {average_train_accuracy}   "
                        f"test accuracy: {acc}   robust accuracy: {average_robust_accuracy}   "
                        f"time: {epoch_cost_time:.2f}s")

                    if best_robustness < average_robust_accuracy:
                        best_robustness = average_robust_accuracy
                        logger.info(f"better robustness: {best_robustness}")
                        logger.info(f"corresponding accuracy on test set: {acc}")
                        self._save_model(f"{save_path}-best_robust")

            self._save_checkpoint(ep, best_robustness)

        logger.info("finished training")
        logger.info(f"best robustness on test set: {best_robustness}")

        self._save_last_model(f"{save_path}-last") # imTyrant added it for saving last model.

    def _register_forward_hook_to_k_block(self, k):
        total_blocks = self._blocks.get_total_blocks()
        assert 1 <= k <= total_blocks
        logger.debug(f"model total blocks: {total_blocks}")
        logger.debug(f"register hook to the last layer of {k}th block from last")
        # block = getattr(self._blocks, f"block{total_blocks+1-k}")
        # input of next block
        block = getattr(self._blocks, f"block{total_blocks-k+1}")
        # FIXME
        if isinstance(block, torch.nn.Sequential):
            # block[-1].register_forward_hook(self._get_layer_outputs)
            # input of next block
            block[0].register_forward_hook(self._get_layer_inputs)
        else:
            block.register_forward_hook(self._get_layer_inputs)

    # FIXME
    def _get_layer_outputs(self, layer, inputs, outputs):
        if self.model.training:
            self._hooked_features_list.append(outputs.clone())

    def _get_layer_inputs(self, layer, inputs, outputs):
        if self.model.training:
            self._hooked_features_list.append(inputs[0].clone())
    
    def _freeze_trainable_parameters(self):
        for p in self.model.parameters():
            p.requires_grad = False
    
    def _unfreeze_trainable_parameters(self):
        for p in self.model.parameters():
            p.requires_grad = True
