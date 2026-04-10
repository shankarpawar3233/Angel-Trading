from __future__ import annotations

from typing import Any, Dict, List

from engines.config import env_float, env_int
from engines.hero_zero_engine import _is_expiry_day_for_symbol


def _atr_proxy(prices: List[float], win: int) -> float:
    if len(prices) < win + 1:
        return 0.0
    seg = prices[-(win + 1) :]
    diffs = [abs(seg[i] - seg[i - 1]) for i in range(1, len(seg))]
    return sum(diffs) / max(1, len(diffs))


def detect_regime(
    *,
    symbol: str,
    price_history: List[float],
    price: float,
    oi_snap: Dict[str, Any],
    sp_result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    n = env_int("REGIME_WINDOW", 48)
    if len(price_history) < max(8, n // 2) or price <= 0:
        return {"regime": "RANGING", "confidence": 35.0, "metrics": {"insufficient_history": True}}

    win = price_history[-n:]
    p_min = min(win)
    p_max = max(win)
    prange = p_max - p_min
    range_frac = prange / max(1.0, price)
    atr = _atr_proxy(price_history, env_int("REGIME_ATR_WIN", 20))
    atr_frac = atr / max(1.0, price)
    vwap_proxy = sum(win) / max(1, len(win))
    vwap_dev = abs(price - vwap_proxy) / max(1.0, price)

    ce_oi = float(oi_snap.get("ce_oi_total") or 0.0)
    pe_oi = float(oi_snap.get("pe_oi_total") or 0.0)
    oi_total = ce_oi + pe_oi
    oi_imbalance = abs(ce_oi - pe_oi) / max(1.0, oi_total)

    # Consistency from scalping feature strengths (already computed once).
    coi = float((sp_result or {}).get("call_oi") or 0.0)
    poi = float((sp_result or {}).get("put_oi") or 0.0)
    oi_consistency = abs(coi - poi)

    exp_day = _is_expiry_day_for_symbol(symbol)
    gamma_hint = bool((sp_result or {}).get("hero_zero"))

    volatile_cut = env_float("REGIME_VOLATILE_RANGE_FRAC", 0.0032)
    trend_range_min = env_float("REGIME_TREND_RANGE_FRAC", 0.0012)
    trend_vwap_dev = env_float("REGIME_TREND_VWAP_DEV", 0.0009)
    trend_oi_consistency = env_float("REGIME_TREND_OI_CONSISTENCY", 0.08)

    if exp_day and (gamma_hint or atr_frac >= env_float("REGIME_EXPIRY_ATR_FRAC", 0.0009)):
        conf = min(95.0, 60.0 + (12.0 if gamma_hint else 0.0) + min(15.0, atr_frac * 12000.0))
        return {
            "regime": "EXPIRY_HIGH_GAMMA",
            "confidence": round(conf, 2),
            "metrics": {
                "range_frac": range_frac,
                "atr_frac": atr_frac,
                "vwap_dev": vwap_dev,
                "oi_consistency": oi_consistency,
                "oi_imbalance": oi_imbalance,
            },
        }

    if range_frac >= volatile_cut or atr_frac >= env_float("REGIME_VOLATILE_ATR_FRAC", 0.0010):
        conf = min(93.0, 58.0 + min(30.0, range_frac * 7000.0 + atr_frac * 9000.0))
        return {
            "regime": "VOLATILE",
            "confidence": round(conf, 2),
            "metrics": {
                "range_frac": range_frac,
                "atr_frac": atr_frac,
                "vwap_dev": vwap_dev,
                "oi_consistency": oi_consistency,
                "oi_imbalance": oi_imbalance,
            },
        }

    if range_frac >= trend_range_min and vwap_dev >= trend_vwap_dev and oi_consistency >= trend_oi_consistency:
        conf = min(92.0, 55.0 + min(30.0, vwap_dev * 12000.0 + oi_consistency * 45.0))
        return {
            "regime": "TRENDING",
            "confidence": round(conf, 2),
            "metrics": {
                "range_frac": range_frac,
                "atr_frac": atr_frac,
                "vwap_dev": vwap_dev,
                "oi_consistency": oi_consistency,
                "oi_imbalance": oi_imbalance,
            },
        }

    conf = min(90.0, 48.0 + min(25.0, (1.0 - min(1.0, range_frac / max(1e-6, volatile_cut))) * 25.0))
    return {
        "regime": "RANGING",
        "confidence": round(conf, 2),
        "metrics": {
            "range_frac": range_frac,
            "atr_frac": atr_frac,
            "vwap_dev": vwap_dev,
            "oi_consistency": oi_consistency,
            "oi_imbalance": oi_imbalance,
        },
    }
