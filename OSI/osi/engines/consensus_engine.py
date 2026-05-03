from __future__ import annotations

from typing import Iterable, List, Literal

from osi.core.models import ConsensusOutput, EngineOutput
from osi.core.market_phase import engine_weight, engine_weights_for_phase

class ConsensusEngine:
    def __init__(self) -> None:
        pass

    def combine(
        self,
        outputs: Iterable[EngineOutput],
        *,
        regime: Literal["TRENDING", "SIDEWAYS"] = "SIDEWAYS",
        market_phase: str = "OFF",
    ) -> ConsensusOutput:
        rows: List[EngineOutput] = list(outputs)
        selected = self.select_final_signal(rows, market_phase=market_phase)
        weights = engine_weights_for_phase(market_phase)
        if selected is None:
            return ConsensusOutput(
                signal="NONE",
                confidence=0.0,
                weighted_score=0.0,
                engine_outputs=rows,
                market_phase=market_phase,
                engine_weights=weights,
                selected_engine="",
            )
        conf = float(selected.confidence if selected.confidence is not None else (float(selected.strength) * 100.0))
        weight = engine_weight(selected.engine, market_phase)
        weighted_score = float(selected.strength or 0.0) * weight
        return ConsensusOutput(
            signal=selected.signal,
            confidence=round(max(0.0, min(100.0, conf * weight)), 2),
            weighted_score=round(weighted_score, 4),
            engine_outputs=rows,
            market_phase=market_phase,
            engine_weights=weights,
            selected_engine=selected.engine,
        )

    @staticmethod
    def select_final_signal(rows: List[EngineOutput], *, market_phase: str = "OFF") -> EngineOutput | None:
        directional = [r for r in rows if r.signal in {"BUY_CE", "BUY_PE"}]
        if not directional:
            return None
        return max(
            directional,
            key=lambda r: (
                float(r.strength or 0.0) * engine_weight(r.engine, market_phase),
                float(r.confidence or 0.0),
            ),
        )

