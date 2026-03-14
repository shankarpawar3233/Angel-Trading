from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from stable_baselines3 import PPO

from reinforcement.environment import OptionsTradingEnv


@dataclass
class RLTradingAgent:
    features: np.ndarray
    prices: np.ndarray

    def __post_init__(self):
        self.env = OptionsTradingEnv(self.features, self.prices)
        self.model = PPO("MlpPolicy", self.env, verbose=0)

    def train(self, timesteps: int = 10_000) -> None:
        self.model.learn(total_timesteps=timesteps)

    def predict_action(self, latest_features: np.ndarray) -> dict:
        """
        latest_features: shape (F,)
        """
        action, _ = self.model.predict(latest_features, deterministic=True)
        mapping = {0: "BUY_CE", 1: "BUY_PE", 2: "HOLD"}
        return {"action": mapping.get(int(action), "HOLD")}

