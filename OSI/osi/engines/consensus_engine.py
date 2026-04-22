from __future__ import annotations

from typing import Dict, Iterable, List, Literal

from osi.core.models import ConsensusOutput, EngineOutput


class ConsensusEngine:
    def __init__(self, weights: Dict[str, float] | None = None) -> None:
        self.weights = weights or {
            "scalping_engine": 1.0,
            "smc_engine": 1.0,
            "hero_zero_engine": 0.8,
            "trend_engine": 1.2,
            "mean_reversion_engine": 0.9,
            "option_chain_engine": 1.1,
            "ml_engine": 0.6,
            "intrabar_engine": 1.6,
            "smart_breakout_engine": 1.5,
        }

    def combine(
        self,
        outputs: Iterable[EngineOutput],
        *,
        regime: Literal["TRENDING", "SIDEWAYS"] = "SIDEWAYS",
    ) -> ConsensusOutput:
        rows: List[EngineOutput] = list(outputs)
        effective_weights = dict(self.weights)
        if regime == "TRENDING":
            effective_weights["mean_reversion_engine"] = 0.0
        elif regime == "SIDEWAYS":
            effective_weights["scalping_engine"] = effective_weights.get("scalping_engine", 1.0) * 0.6

        total_weight = max(sum(effective_weights.get(row.engine, 1.0) for row in rows), 1e-6)
        signed_score = 0.0
        ce_score = 0.0
        pe_score = 0.0

        for row in rows:
            w = effective_weights.get(row.engine, 1.0)
            contribution = row.strength * w
            if row.signal == "BUY_CE":
                signed_score += contribution
                ce_score += contribution
            elif row.signal == "BUY_PE":
                signed_score -= contribution
                pe_score += contribution

        strong_bull = sum(1 for r in rows if r.signal == "BUY_CE" and r.strength >= 0.6)
        strong_bear = sum(1 for r in rows if r.signal == "BUY_PE" and r.strength >= 0.6)
        if strong_bull >= 2 and strong_bear == 0:
            signed_score += 0.35
            ce_score += 0.35
        elif strong_bear >= 2 and strong_bull == 0:
            signed_score -= 0.35
            pe_score += 0.35

        has_bull = any(r.signal == "BUY_CE" for r in rows)
        has_bear = any(r.signal == "BUY_PE" for r in rows)
        if has_bull and has_bear:
            signed_score *= 0.65
            ce_score *= 0.75
            pe_score *= 0.75

        dominance = abs(ce_score - pe_score)
        confidence = min(100.0, (dominance / total_weight) * 100.0)
        ml_row = next((r for r in rows if r.engine == "ml_engine"), None)
        if ml_row and ml_row.strength < 0.5:
            confidence = min(confidence, 70.0)
        if ce_score > pe_score:
            final_signal = "BUY_CE"
        elif pe_score > ce_score:
            final_signal = "BUY_PE"
        else:
            final_signal = "BUY_CE" if signed_score >= 0 else "BUY_PE"

        return ConsensusOutput(
            signal=final_signal,
            confidence=round(confidence, 2),
            weighted_score=round(signed_score, 4),
            engine_outputs=rows,
        )

