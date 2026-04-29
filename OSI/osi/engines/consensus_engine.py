from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Literal

from osi.core.models import ConsensusOutput, EngineOutput

logger = logging.getLogger(__name__)


class ConsensusEngine:
    def __init__(self, weights: Dict[str, float] | None = None) -> None:
        self.weights = weights or {
            "scalping_engine": 1.0,
            "smc_engine": 1.0,
            "zero_hero_engine": 0.8,
            "trend_engine": 1.2,
            "mean_reversion_engine": 0.9,
            "option_chain_engine": 1.1,
            "ml_engine": 0.6,
            "intrabar_engine": 1.6,
            "smart_breakout_engine": 1.5,
        }
        self.priorities: Dict[str, str] = {
            "intrabar_engine": "HIGH",
            "smart_breakout_engine": "HIGH",
            "zero_hero_engine": "VERY_HIGH",
            "mean_reversion_engine": "LOW",
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

        active_rows = [row for row in rows if row.signal in {"BUY_CE", "BUY_PE"} and float(row.strength) > 0.0]
        if not active_rows:
            return ConsensusOutput(
                signal="NONE",
                confidence=0.0,
                weighted_score=0.0,
                engine_outputs=rows,
            )

        selected_engine = ""
        ignored_engines: List[str] = []
        override_reason = ""
        effective_rows: List[EngineOutput] = list(active_rows)
        selected_row: EngineOutput | None = None

        # Zero-hero always overrides all others when it emits a directional signal.
        zero_hero_rows = [r for r in active_rows if r.engine == "zero_hero_engine"]
        if zero_hero_rows:
            chosen = max(zero_hero_rows, key=lambda r: float(r.strength))
            selected_row = chosen
            selected_engine = chosen.engine
            selected_signal = chosen.signal
            effective_rows = [r for r in active_rows if r.signal == selected_signal]
            ignored_engines = [r.engine for r in active_rows if r.signal != selected_signal]
            override_reason = "priority_override_zero_hero"
        else:
            high_rows = [
                r
                for r in active_rows
                if self.priorities.get(r.engine, "LOW") in {"HIGH", "VERY_HIGH"} and float(r.strength) >= 0.6
            ]
            if high_rows:
                chosen = max(high_rows, key=lambda r: float(r.strength))
                selected_row = chosen
                selected_engine = chosen.engine
                selected_signal = chosen.signal
                # Follow strongest high-priority direction; drop opposing engines.
                effective_rows = [r for r in active_rows if r.signal == selected_signal]
                ignored_engines = [r.engine for r in active_rows if r.signal != selected_signal]
                override_reason = "priority_override"

        if selected_engine:
            logger.debug(
                "CONSENSUS DECISION selected_engine=%s ignored_engines=%s reason=%s",
                selected_engine,
                ",".join(ignored_engines) or "-",
                override_reason,
            )

        total_weight = max(sum(effective_weights.get(row.engine, 1.0) for row in effective_rows), 1e-6)
        signed_score = 0.0
        ce_score = 0.0
        pe_score = 0.0

        for row in effective_rows:
            w = effective_weights.get(row.engine, 1.0)
            contribution = row.strength * w
            if row.signal == "BUY_CE":
                signed_score += contribution
                ce_score += contribution
            elif row.signal == "BUY_PE":
                signed_score -= contribution
                pe_score += contribution

        strong_bull = sum(1 for r in effective_rows if r.signal == "BUY_CE" and r.strength >= 0.6)
        strong_bear = sum(1 for r in effective_rows if r.signal == "BUY_PE" and r.strength >= 0.6)
        if strong_bull >= 2 and strong_bear == 0:
            signed_score += 0.35
            ce_score += 0.35
        elif strong_bear >= 2 and strong_bull == 0:
            signed_score -= 0.35
            pe_score += 0.35

        has_bull = any(r.signal == "BUY_CE" for r in effective_rows)
        has_bear = any(r.signal == "BUY_PE" for r in effective_rows)
        if has_bull and has_bear:
            signed_score *= 0.65
            ce_score *= 0.75
            pe_score *= 0.75

        if ce_score > pe_score:
            final_signal = "BUY_CE"
        elif pe_score > ce_score:
            final_signal = "BUY_PE"
        else:
            final_signal = "BUY_CE" if signed_score >= 0 else "BUY_PE"

        # Base confidence uses only aligned-direction engines (ignore opposite).
        aligned_rows = [r for r in active_rows if r.signal == final_signal and float(r.strength) > 0.0]
        ignored_for_base = [r.engine for r in active_rows if r.signal != final_signal]
        if aligned_rows:
            avg_strength = sum(float(r.strength) for r in aligned_rows) / len(aligned_rows)
            confidence = max(0.0, min(100.0, avg_strength * 100.0))
        else:
            confidence = 0.0

        # Soft base floor for strong high-priority primary signal.
        primary_row = selected_row or max(active_rows, key=lambda r: float(r.strength))
        if (
            self.priorities.get(primary_row.engine, "LOW") in {"HIGH", "VERY_HIGH"}
            and primary_row.signal == final_signal
            and float(primary_row.strength) >= 0.6
        ):
            confidence = max(20.0, confidence)

        logger.debug(
            "BASE CONFIDENCE CALC selected_direction=%s contributing_engines=%s ignored_engines=%s final_base=%.2f",
            final_signal,
            ",".join(r.engine for r in aligned_rows) or "-",
            ",".join(ignored_for_base) or "-",
            float(confidence),
        )

        return ConsensusOutput(
            signal=final_signal,
            confidence=round(confidence, 2),
            weighted_score=round(signed_score, 4),
            engine_outputs=rows,
        )

