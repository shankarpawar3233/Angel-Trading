from __future__ import annotations

from typing import List

from engines.base import BaseEngine
from engines.call_side_engine import CallSideEngine
from engines.hero_zero_engine import HeroZeroEngine
from engines.hold_engine import HoldEngine
from engines.put_side_engine import PutSideEngine
from engines.scalping_engine import ScalpingEngine


def default_engines() -> List[BaseEngine]:
    """Order affects logging only; aggregator uses its own priority weights."""
    return [
        ScalpingEngine(),
        HoldEngine(),
        CallSideEngine(),
        PutSideEngine(),
        HeroZeroEngine(),
    ]
