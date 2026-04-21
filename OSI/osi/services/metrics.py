from __future__ import annotations

from typing import Dict

from osi.core.models import RuntimeMetrics


class MetricsService:
    def __init__(self) -> None:
        self.metrics = RuntimeMetrics()

    def record_tick(self, total_latency_ms: float) -> None:
        m = self.metrics
        m.ticks_processed += 1
        m.last_tick_latency_ms = total_latency_ms
        m.max_latency_ms = max(m.max_latency_ms, total_latency_ms)
        m.average_latency_ms = (
            ((m.average_latency_ms * (m.ticks_processed - 1)) + total_latency_ms) / m.ticks_processed
        )

    def record_latency_breakdown(
        self,
        *,
        tick_to_process_latency_ms: float,
        process_to_signal_latency_ms: float,
        total_signal_latency_ms: float,
    ) -> None:
        m = self.metrics
        n = max(1, m.ticks_processed)
        m.last_tick_to_process_latency_ms = tick_to_process_latency_ms
        m.last_process_to_signal_latency_ms = process_to_signal_latency_ms
        m.last_total_signal_latency_ms = total_signal_latency_ms
        m.average_tick_to_process_latency_ms = (
            ((m.average_tick_to_process_latency_ms * (n - 1)) + tick_to_process_latency_ms) / n
        )
        m.average_process_to_signal_latency_ms = (
            ((m.average_process_to_signal_latency_ms * (n - 1)) + process_to_signal_latency_ms) / n
        )
        m.average_total_signal_latency_ms = (
            ((m.average_total_signal_latency_ms * (n - 1)) + total_signal_latency_ms) / n
        )

    def record_engine_latency(self, engine: str, elapsed_ms: float) -> None:
        self.metrics.engine_exec_ms[engine] = round(elapsed_ms, 3)

    def record_confidence(self, confidence: float) -> None:
        if confidence >= 80:
            key = "80_100"
        elif confidence >= 60:
            key = "60_79"
        else:
            key = "0_59"
        self.metrics.confidence_distribution[key] += 1

    def record_error(self) -> None:
        self.metrics.pipeline_errors += 1

    def snapshot(self) -> Dict:
        return {
            "ticks_processed": self.metrics.ticks_processed,
            "pipeline_errors": self.metrics.pipeline_errors,
            "average_latency_ms": round(self.metrics.average_latency_ms, 3),
            "max_latency_ms": round(self.metrics.max_latency_ms, 3),
            "last_tick_latency_ms": round(self.metrics.last_tick_latency_ms, 3),
            "tick_to_process_latency_ms": round(self.metrics.last_tick_to_process_latency_ms, 3),
            "process_to_signal_latency_ms": round(self.metrics.last_process_to_signal_latency_ms, 3),
            "total_signal_latency_ms": round(self.metrics.last_total_signal_latency_ms, 3),
            "avg_tick_to_process_latency_ms": round(self.metrics.average_tick_to_process_latency_ms, 3),
            "avg_process_to_signal_latency_ms": round(self.metrics.average_process_to_signal_latency_ms, 3),
            "avg_total_signal_latency_ms": round(self.metrics.average_total_signal_latency_ms, 3),
            "engine_exec_ms": dict(self.metrics.engine_exec_ms),
            "confidence_distribution": dict(self.metrics.confidence_distribution),
        }

