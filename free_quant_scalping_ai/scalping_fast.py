from __future__ import annotations

"""
Rule-based scalping engine for low-latency operation.

Consumes the lightweight features from fast_features.compute_fast_features and
returns an immediate trade suggestion.
"""

from typing import Dict, Any

from utils.logger import get_logger

logger = get_logger(__name__)


def generate_fast_scalping_signal(features: Dict[str, Any]) -> str:
    """
    Simple deterministic rules:
      - BUY_CE: strong call OI + volume + positive momentum
      - BUY_PE: strong put OI + volume + negative momentum
      - otherwise: NO_TRADE
    """
    call_oi = float(features.get("call_oi_strength") or 0.0)
    put_oi = float(features.get("put_oi_strength") or 0.0)
    call_vol = float(features.get("call_volume_strength") or 0.0)
    put_vol = float(features.get("put_volume_strength") or 0.0)
    mom = float(features.get("price_momentum") or 0.0)

    # Thresholds tuned for robustness
    CALL_THRESH = 0.6
    PUT_THRESH = 0.6
    MOM_UP = 0.0005   # 5 bps
    MOM_DOWN = -0.0005

    buy_ce = call_oi >= CALL_THRESH and call_vol >= CALL_THRESH and mom >= MOM_UP
    buy_pe = put_oi >= PUT_THRESH and put_vol >= PUT_THRESH and mom <= MOM_DOWN

    # Avoid conflicting signals (both sides active)
    if buy_ce and buy_pe:
        return "NO_TRADE"
    if buy_ce:
        logger.info("[FAST SIGNAL] BUY_CE (call_oi=%.2f call_vol=%.2f mom=%.4f)", call_oi, call_vol, mom)
        return "BUY_CE"
    if buy_pe:
        logger.info("[FAST SIGNAL] BUY_PE (put_oi=%.2f put_vol=%.2f mom=%.4f)", put_oi, put_vol, mom)
        return "BUY_PE"
    return "NO_TRADE"


def compute_fast_confidence(features: Dict[str, Any]) -> int:
    """
    Lightweight confidence model:
        confidence =
            0.4 * oi_strength +
            0.3 * volume_strength +
            0.3 * price_momentum
    where strengths are max(call, put) and price_momentum is mapped from [-1,1] to [0,1].
    """
    call_oi = float(features.get("call_oi_strength") or 0.0)
    put_oi = float(features.get("put_oi_strength") or 0.0)
    call_vol = float(features.get("call_volume_strength") or 0.0)
    put_vol = float(features.get("put_volume_strength") or 0.0)
    mom = float(features.get("price_momentum") or 0.0)

    oi_strength = max(call_oi, put_oi)
    vol_strength = max(call_vol, put_vol)
    mom_norm = (mom + 1.0) / 2.0  # [-1,1] -> [0,1]

    score = 0.4 * oi_strength + 0.3 * vol_strength + 0.3 * mom_norm
    conf = int(round(max(0.0, min(1.0, score)) * 100))
    return conf

