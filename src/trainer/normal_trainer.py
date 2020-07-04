from typing import Tuple

import torch
from torch.nn.modules.module import Module
from torch.utils.data import DataLoader

from trainer import BaseTrainer


class NormalTrainer(BaseTrainer):
    def __init__(self, model: Module, train_loader: DataLoader,
                 test_loader: DataLoader, checkpoint_path: str = None):
        super(NormalTrainer, self).__init__(model, train_loader, test_loader, checkpoint_path)

    def step_batch(self, inputs: torch.Tensor, labels: torch.Tensor) -> Tuple[float, float]:
        inputs, labels = inputs.to(self._device), labels.to(self._device)
        self.optimizer.zero_grad()

        outputs = self.model(inputs)
        loss = self.criterion(outputs, labels)
        loss.backward()
        self.optimizer.step()

        batch_training_acc = (outputs.argmax(dim=1) == labels).float().mean().item()
        batch_running_loss = loss.item()

        return batch_running_loss, batch_training_acc
