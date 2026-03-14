from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import numpy as np
import pandas as pd


@dataclass
class BacktestResult:
    win_rate: float
    sharpe: float
    max_drawdown: float
    profit_factor: float


def _compute_metrics(returns: np.ndarray) -> BacktestResult:
    if len(returns) == 0:
        return BacktestResult(0.0, 0.0, 0.0, 0.0)
    wins = (returns > 0).sum()
    win_rate = wins / len(returns)
    sharpe = np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(252)
    equity = returns.cumsum()
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    max_dd = drawdown.min()
    pf = returns[returns > 0].sum() / abs(returns[returns < 0].sum() + 1e-9)
    return BacktestResult(
        win_rate=float(win_rate),
        sharpe=float(sharpe),
        max_drawdown=float(max_dd),
        profit_factor=float(pf),
    )


def simple_directional_backtest(
    df: pd.DataFrame, signal_col: str = "signal"
) -> Dict:
    """
    Basic backtest for directional signals on underlying close prices.
    Assumes 'signal' column in {1 (long), -1 (short), 0 (flat)}.
    """
    if df.empty or signal_col not in df.columns:
        return {}
    df = df.copy()
    df["ret"] = df["close"].pct_change().fillna(0.0)
    df["strategy"] = df[signal_col].shift(1).fillna(0.0) * df["ret"]
    metrics = _compute_metrics(df["strategy"].values)
    return {
        "win_rate": metrics.win_rate,
        "sharpe": metrics.sharpe,
        "max_drawdown": metrics.max_drawdown,
        "profit_factor": metrics.profit_factor,
    }

