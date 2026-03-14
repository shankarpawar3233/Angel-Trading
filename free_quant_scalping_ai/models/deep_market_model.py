from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import torch
from torch import nn

from config.settings import settings


LabelType = Literal["BUY_CE", "BUY_PE", "NO_TRADE"]


class LSTMMarketModel(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int = 64, num_layers: int = 2):
        super().__init__()
        self.lstm = nn.LSTM(
            input_dim, hidden_dim, num_layers=num_layers, batch_first=True
        )
        self.fc = nn.Linear(hidden_dim, 3)  # BUY CE, BUY PE, NO TRADE

    def forward(self, x):
        out, _ = self.lstm(x)
        last = out[:, -1, :]
        logits = self.fc(last)
        return logits


@dataclass
class DeepMarketPredictor:
    input_dim: int
    device: str = "cpu"

    def __post_init__(self):
        self.model = LSTMMarketModel(self.input_dim).to(self.device)
        self.sequence_length = settings.sequence_length
        self.softmax = nn.Softmax(dim=-1)

    def predict_proba(self, x_tensor: torch.Tensor) -> torch.Tensor:
        """
        x_tensor: shape (batch, seq_len, input_dim)
        returns probs for [BUY_CE, BUY_PE, NO_TRADE]
        """
        self.model.eval()
        with torch.no_grad():
            logits = self.model(x_tensor.to(self.device))
            probs = self.softmax(logits)
        return probs

