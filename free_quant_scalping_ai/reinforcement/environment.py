from __future__ import annotations

from typing import Any, Dict, Tuple

import gymnasium as gym
import numpy as np
from gymnasium import spaces


ActionType = int  # 0=BUY_CE, 1=BUY_PE, 2=HOLD


class OptionsTradingEnv(gym.Env):
    """
    Simple RL environment for index options research.
    State: price and option-chain derived features (pre-computed).
    Reward: profit and basic risk penalty.
    """

    def __init__(self, features: np.ndarray, prices: np.ndarray):
        super().__init__()
        self.features = features
        self.prices = prices
        n_features = self.features.shape[1]
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(n_features,), dtype=np.float32
        )
        self.action_space = spaces.Discrete(3)
        self.t = 0
        self.position: int = 0
        self.entry_price: float = 0.0

    def reset(self, *, seed: int | None = None, options: Dict | None = None):
        super().reset(seed=seed)
        self.t = 0
        self.position = 0
        self.entry_price = 0.0
        obs = self.features[self.t].astype("float32")
        return obs, {}

    def step(self, action: ActionType) -> Tuple[np.ndarray, float, bool, bool, Dict]:
        done = False
        truncated = False

        price = self.prices[self.t]
        reward = 0.0

        # Close any existing position at current price
        if self.position != 0:
            pnl = (price - self.entry_price) * self.position
            reward += pnl - 0.1 * abs(pnl)  # mild risk penalty
            self.position = 0
            self.entry_price = 0.0

        if action == 0:  # BUY_CE
            self.position = 1
            self.entry_price = price
        elif action == 1:  # BUY_PE
            self.position = -1
            self.entry_price = price

        self.t += 1
        if self.t >= len(self.prices) - 1:
            done = True
            truncated = True
            obs = self.features[-1].astype("float32")
        else:
            obs = self.features[self.t].astype("float32")

        return obs, float(reward), done, truncated, {}

