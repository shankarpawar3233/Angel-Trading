"""
Single source of truth for the existing fast scalping path (features → filters → stabilizer).

Does not change rule math: delegates to ``fast_features``, ``scalping_fast``, and ``app.services.signal_engine``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from app.services import signal_engine
from fast_features import compute_fast_features
from hero_zero_fast import detect_hero_zero_fast
from scalping_fast import compute_fast_confidence, generate_fast_scalping_signal, generate_ml_signal
from utils.logger import get_logger

logger = get_logger(__name__)


def run_scalping_pipeline(
    *,
    symbol: str,
    price: float,
    chain_snapshot: Dict[str, Dict[str, Dict[str, Any]]],
    sym_hist: List[float],
    decision_ds: Dict[str, Any],
    side_balance_state: Dict[str, Dict[str, float]],
    now_sec: float,
    prev_market_price: Optional[float],
) -> Dict[str, Any]:
    su = symbol.upper()
    features = compute_fast_features(chain_snapshot, float(price or 0.0), sym_hist)
    vol_avail = bool(features.get("volume_available", True))
    features["volume_fallback_used"] = su == "SENSEX" and not vol_avail
    rule_signal = generate_fast_scalping_signal(features, su)
    ml_signal = generate_ml_signal(features, su)
    scalping_signal = rule_signal
    hero_zero = detect_hero_zero_fast(chain_snapshot, float(price or 0.0))
    confidence = compute_fast_confidence(features)
    if features.get("volume_fallback_used"):
        try:
            pen = int(os.getenv("SENSEX_VOLUME_CONF_PENALTY", "7"))
        except ValueError:
            pen = 7
        pen = max(5, min(10, pen))
        confidence = max(50, int(confidence) - pen)
    call_oi = float(features.get("call_oi_strength") or 0.0)
    put_oi = float(features.get("put_oi_strength") or 0.0)
    call_vol = float(features.get("call_volume_strength") or 0.0)
    put_vol = float(features.get("put_volume_strength") or 0.0)
    mom = float(features.get("price_momentum") or 0.0)

    debug: List[str] = []
    if features.get("volume_fallback_used"):
        debug.append("volume_fallback_used|true")
        logger.debug(
            "[SENSEX] volume_fallback_used=true volume_available=false signal=%s conf_after_penalty=%s",
            rule_signal,
            confidence,
        )
    scalping_signal, confidence = signal_engine.merge_ml_fallback(scalping_signal, ml_signal, confidence)
    scalping_signal, confidence = signal_engine.sensex_premium_fallback(
        su, scalping_signal, chain_snapshot, price, features, confidence
    )
    scalping_signal, confidence = signal_engine.bias_tiebreak(
        su, scalping_signal, confidence, call_oi, put_oi, call_vol, put_vol, mom
    )
    scalping_signal = signal_engine.apply_trend_filter(su, scalping_signal, sym_hist, debug)
    scalping_signal = signal_engine.apply_volatility_filter(su, scalping_signal, sym_hist, price, debug)
    scalping_signal = signal_engine.apply_side_balance_filter(
        symbol, scalping_signal, call_oi, put_oi, call_vol, put_vol, side_balance_state, debug
    )

    scalping_signal, confidence = signal_engine.apply_hysteresis(su, scalping_signal, confidence, decision_ds, now_sec)

    entry_decision, decision_reason, stable_count, lock_remaining = signal_engine.apply_entry_stabilizer(
        su=su,
        symbol=symbol,
        scalping_signal=scalping_signal,
        confidence=confidence,
        price=price,
        prev_market_price=prev_market_price,
        ds=decision_ds,
        now_sec=now_sec,
        debug=debug,
    )

    return {
        "scalping_signal": scalping_signal,
        "confidence": confidence,
        "ml_signal": ml_signal,
        "hero_zero": hero_zero,
        "features": features,
        "volume_available": vol_avail,
        "volume_fallback_used": bool(features.get("volume_fallback_used")),
        "debug": debug,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "call_vol": call_vol,
        "put_vol": put_vol,
        "mom": mom,
        "entry_decision": entry_decision,
        "decision_reason": decision_reason,
        "stable_count": stable_count,
        "lock_remaining": lock_remaining,
    }
