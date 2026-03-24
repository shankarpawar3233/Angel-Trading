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

    # Slightly relaxed thresholds for live feeds where OI may be sparse.
    CALL_THRESH = 0.45
    PUT_THRESH = 0.45
    VOL_THRESH = 0.50
    MOM_UP = 0.0002   # 2 bps
    MOM_DOWN = -0.0002

    oi_available = (call_oi > 0.0) or (put_oi > 0.0)
    if oi_available:
        buy_ce = call_oi >= CALL_THRESH and call_vol >= VOL_THRESH and mom >= MOM_UP
        buy_pe = put_oi >= PUT_THRESH and put_vol >= VOL_THRESH and mom <= MOM_DOWN
    else:
        # Fallback when OI is unavailable: rely on volume imbalance + momentum.
        buy_ce = call_vol >= 0.60 and call_vol > put_vol and mom >= MOM_UP
        buy_pe = put_vol >= 0.60 and put_vol > call_vol and mom <= MOM_DOWN

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


def generate_ml_signal(features: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ultra-light ML-style scorer (constant-time): combines OI, volume and momentum
    into CE/PE directional probabilities. No blocking or model I/O.
    """
    call_oi = float(features.get("call_oi_strength") or 0.0)
    put_oi = float(features.get("put_oi_strength") or 0.0)
    call_vol = float(features.get("call_volume_strength") or 0.0)
    put_vol = float(features.get("put_volume_strength") or 0.0)
    mom = float(features.get("price_momentum") or 0.0)

    # Linear logits (cheap) then sigmoid.
    z_ce = 1.8 * (call_oi - put_oi) + 1.3 * (call_vol - put_vol) + 4.5 * mom
    z_pe = -z_ce

    def _sigmoid(x: float) -> float:
        # stable enough in this bounded range
        import math

        return 1.0 / (1.0 + math.exp(-max(-12.0, min(12.0, x))))

    p_ce = _sigmoid(z_ce)
    p_pe = _sigmoid(z_pe)
    if p_ce >= p_pe:
        label = "BUY_CE"
        p = p_ce
    else:
        label = "BUY_PE"
        p = p_pe

    confidence = int(round(p * 100))
    # Require a floor so ML fallback does not overtrade noise.
    if confidence < 58:
        label = "NO_TRADE"

    return {
        "label": label,
        "confidence": confidence,
        "prob_ce": round(p_ce, 4),
        "prob_pe": round(p_pe, 4),
    }


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

    score = max(0.0, min(1.0, 0.4 * oi_strength + 0.3 * vol_strength + 0.3 * mom_norm))
    # Dynamic confidence band for live stability: map [0,1] -> [50,95].
    conf = int(round(50.0 + score * 45.0))
    conf = max(50, min(95, conf))
    return conf

