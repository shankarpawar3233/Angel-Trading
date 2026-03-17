"""
PCR + Gamma + MaxPain Convergence Engine.

Combines Put-Call Ratio, gamma walls, and max pain to produce a single bias (BULLISH / BEARISH / RANGE)
and strength score for use in final signal generation.
"""

from __future__ import annotations

from typing import Any, Dict

from utils.logger import get_logger

logger = get_logger(__name__)


def compute_convergence(
    price: float,
    pcr: float | None = None,
    call_wall: float | None = None,
    put_wall: float | None = None,
    max_pain: float | None = None,
) -> Dict[str, Any]:
    """
    Combine PCR, gamma walls, and max pain into a single bias.

    BULLISH: PCR < 0.8 AND call_gamma_wall above price AND max_pain above price.
    BEARISH: PCR > 1.2 AND put_gamma_wall below price AND max_pain below price.
    Else: RANGE.

    Returns:
        {"bias": "BULLISH" | "BEARISH" | "RANGE", "strength": 0-100}
    """
    if price is None or price <= 0:
        return {"bias": "RANGE", "strength": 0}

    bullish_conditions = 0
    bearish_conditions = 0
    total_checks = 0

    if pcr is not None:
        total_checks += 1
        if pcr < 0.8:
            bullish_conditions += 1
        elif pcr > 1.2:
            bearish_conditions += 1
    if call_wall is not None:
        total_checks += 1
        if call_wall > price:
            bullish_conditions += 1
    if put_wall is not None:
        total_checks += 1
        if put_wall < price:
            bearish_conditions += 1
    if max_pain is not None:
        total_checks += 1
        if max_pain > price:
            bullish_conditions += 1
        elif max_pain < price:
            bearish_conditions += 1

    if total_checks == 0:
        return {"bias": "RANGE", "strength": 0}

    # User spec: BULLISH if PCR < 0.8 AND call_wall above price AND max_pain above price (when available)
    has_any = pcr is not None or call_wall is not None or put_wall is not None or max_pain is not None
    all_bullish = (
        (pcr is None or pcr < 0.8)
        and (call_wall is None or call_wall > price)
        and (max_pain is None or max_pain > price)
        and has_any
    )
    all_bearish = (
        (pcr is None or pcr > 1.2)
        and (put_wall is None or put_wall < price)
        and (max_pain is None or max_pain < price)
        and has_any
    )

    if all_bullish:
        strength = min(100, 50 + bullish_conditions * 15)
        logger.info("[CONVERGENCE] bias detected: BULLISH strength=%s", strength)
        return {"bias": "BULLISH", "strength": strength}
    if all_bearish:
        strength = min(100, 50 + bearish_conditions * 15)
        logger.info("[CONVERGENCE] bias detected: BEARISH strength=%s", strength)
        return {"bias": "BEARISH", "strength": strength}

    strength = max(0, 30 - abs(bullish_conditions - bearish_conditions) * 5)
    logger.info("[CONVERGENCE] bias detected: RANGE strength=%s", strength)
    return {"bias": "RANGE", "strength": strength}
