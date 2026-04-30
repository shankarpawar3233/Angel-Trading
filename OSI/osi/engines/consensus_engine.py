from __future__ import annotations

from typing import Iterable, List, Literal

from osi.core.models import ConsensusOutput, EngineOutput

class ConsensusEngine:
    def __init__(self) -> None:
        pass

    def combine(
        self,
        outputs: Iterable[EngineOutput],
        *,
        regime: Literal["TRENDING", "SIDEWAYS"] = "SIDEWAYS",
    ) -> ConsensusOutput:
        rows: List[EngineOutput] = list(outputs)
        selected = self.select_final_signal(rows)
        if selected is None:
            return ConsensusOutput(signal="NONE", confidence=0.0, weighted_score=0.0, engine_outputs=rows)
        conf = float(selected.confidence if selected.confidence is not None else (float(selected.strength) * 100.0))
        return ConsensusOutput(
            signal=selected.signal,
            confidence=round(max(0.0, min(100.0, conf)), 2),
            weighted_score=round(float(selected.strength or 0.0), 4),
            engine_outputs=rows,
        )

    @staticmethod
    def select_final_signal(rows: List[EngineOutput]) -> EngineOutput | None:
        directional = [r for r in rows if r.signal in {"BUY_CE", "BUY_PE"}]
        if not directional:
            return None
        zero = [r for r in directional if r.engine == "zero_hero_engine"]
        if zero:
            return max(zero, key=lambda r: float(r.strength or 0.0))
        intrabar = [
            r for r in directional if r.engine == "intrabar_engine" and float(r.strength or 0.0) >= 0.7
        ]
        if intrabar:
            return max(intrabar, key=lambda r: float(r.strength or 0.0))
        sb = [r for r in directional if r.engine == "smart_breakout_engine"]
        if sb:
            return max(sb, key=lambda r: float(r.strength or 0.0))
        return None

