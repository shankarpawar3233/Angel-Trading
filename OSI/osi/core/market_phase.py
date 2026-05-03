from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Dict, Literal


MarketPhase = Literal["MORNING", "MIDDAY", "AFTERNOON", "OFF"]

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

PHASE_ENGINE_WEIGHTS: Dict[MarketPhase, Dict[str, float]] = {
    "MORNING": {
        "intrabar_engine": 1.8,
        "smart_breakout_engine": 1.4,
        "mean_reversion_engine": 0.6,
    },
    "MIDDAY": {
        "mean_reversion_engine": 1.6,
        "intrabar_engine": 0.8,
        "smart_breakout_engine": 0.8,
    },
    "AFTERNOON": {
        "smart_breakout_engine": 1.8,
        "intrabar_engine": 1.2,
        "mean_reversion_engine": 0.5,
    },
    "OFF": {},
}

PHASE_VOLUME_MULTIPLIER: Dict[MarketPhase, float] = {
    "MORNING": 1.5,
    "MIDDAY": 2.0,
    "AFTERNOON": 1.8,
    "OFF": 2.0,
}


def get_market_phase(now_ist: datetime) -> MarketPhase:
    local = now_ist.astimezone(IST) if now_ist.tzinfo is not None else now_ist.replace(tzinfo=IST)
    tod = local.time()
    if time(9, 15) <= tod < time(11, 0):
        return "MORNING"
    if time(11, 0) <= tod < time(13, 30):
        return "MIDDAY"
    if time(13, 45) <= tod <= time(15, 30):
        return "AFTERNOON"
    return "OFF"


def engine_weights_for_phase(phase: str) -> Dict[str, float]:
    return dict(PHASE_ENGINE_WEIGHTS.get(str(phase).upper(), PHASE_ENGINE_WEIGHTS["OFF"]))


def engine_weight(engine: str, phase: str) -> float:
    return float(engine_weights_for_phase(phase).get(engine, 1.0))


def volume_multiplier_for_phase(phase: str) -> float:
    return float(PHASE_VOLUME_MULTIPLIER.get(str(phase).upper(), PHASE_VOLUME_MULTIPLIER["OFF"]))


def is_no_trade_soft_zone(now_ist: datetime) -> bool:
    local = now_ist.astimezone(IST) if now_ist.tzinfo is not None else now_ist.replace(tzinfo=IST)
    return time(11, 30) <= local.time() <= time(12, 30)
