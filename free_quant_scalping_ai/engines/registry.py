from __future__ import annotations

from typing import List

from engines.base import BaseEngine
from engines.config import _truthy
from engines.call_side_engine import CallSideEngine
from engines.hold_engine import HoldEngine
from engines.put_side_engine import PutSideEngine
from engines.scalping_engine import ScalpingEngine
from engines.smc_engine import SmcEngine
import os
from engines.hero_zero_engine import HeroZeroEngine


def default_engines() -> List[BaseEngine]:
    """Order affects logging only; aggregator uses its own priority weights."""
    out: List[BaseEngine] = [
        ScalpingEngine(),
        SmcEngine(),
        HoldEngine(),
        CallSideEngine(),
        PutSideEngine(),
    ]
    if _truthy(os.getenv("ENABLE_HERO_ZERO", "0"), "0"):
        out.append(HeroZeroEngine())
    return out
