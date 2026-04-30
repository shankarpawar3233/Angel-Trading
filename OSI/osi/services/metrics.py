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

    def record_tick_received(self) -> None:
        self.metrics.ticks_received += 1

    def record_tick_dropped(self) -> None:
        self.metrics.ticks_dropped += 1

    def record_signal_generated(self) -> None:
        self.metrics.signals_generated += 1

    def record_signal_executed(self) -> None:
        self.metrics.signals_executed += 1

    def record_signal_rejected(self, reason: str) -> None:
        m = self.metrics
        m.signals_rejected += 1
        key = str(reason or "unknown")
        m.rejected_by_reason[key] = int(m.rejected_by_reason.get(key, 0)) + 1

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

    def record_option_data_latency(self, latency_ms: float) -> None:
        self.metrics.option_data_latency_ms = round(max(0.0, latency_ms), 3)

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

    def record_trade_close(self, trade: Dict) -> None:
        m = self.metrics
        pnl = float(trade.get("pnl") or 0.0)
        m.total_trades += 1
        m.total_pnl = round(m.total_pnl + pnl, 2)
        m.current_day_pnl = round(m.current_day_pnl + pnl, 2)
        if pnl > 0:
            m.winning_trades += 1
        else:
            m.losing_trades += 1
        m.win_rate = round((m.winning_trades / max(1, m.total_trades)) * 100.0, 2)
        wins_avg_base = max(1, m.winning_trades)
        losses_avg_base = max(1, m.losing_trades)
        win_sum = ((m.avg_win * (wins_avg_base - 1)) + (pnl if pnl > 0 else 0.0))
        loss_sum = ((m.avg_loss * (losses_avg_base - 1)) + (pnl if pnl <= 0 else 0.0))
        m.avg_win = round(win_sum / wins_avg_base, 2) if m.winning_trades else 0.0
        m.avg_loss = round(loss_sum / losses_avg_base, 2) if m.losing_trades else 0.0
        m.last_5_trades.append(
            {
                "signal_id": trade.get("signal_id"),
                "symbol": trade.get("symbol"),
                "signal": trade.get("signal"),
                "entry_price": trade.get("entry_price"),
                "exit_price": trade.get("exit_price"),
                "pnl": pnl,
                "closed_at": trade.get("closed_at"),
            }
        )
        if len(m.last_5_trades) > 5:
            m.last_5_trades = m.last_5_trades[-5:]

    def snapshot(self) -> Dict:
        return {
            "ticks_received": self.metrics.ticks_received,
            "ticks_dropped": self.metrics.ticks_dropped,
            "ticks_processed": self.metrics.ticks_processed,
            "pipeline_errors": self.metrics.pipeline_errors,
            "signals_generated": self.metrics.signals_generated,
            "signals_executed": self.metrics.signals_executed,
            "signals_rejected": self.metrics.signals_rejected,
            "rejected_by_reason": dict(self.metrics.rejected_by_reason),
            "average_latency_ms": round(self.metrics.average_latency_ms, 3),
            "max_latency_ms": round(self.metrics.max_latency_ms, 3),
            "last_tick_latency_ms": round(self.metrics.last_tick_latency_ms, 3),
            "tick_to_process_latency_ms": round(self.metrics.last_tick_to_process_latency_ms, 3),
            "process_to_signal_latency_ms": round(self.metrics.last_process_to_signal_latency_ms, 3),
            "total_signal_latency_ms": round(self.metrics.last_total_signal_latency_ms, 3),
            "avg_tick_to_process_latency_ms": round(self.metrics.average_tick_to_process_latency_ms, 3),
            "avg_process_to_signal_latency_ms": round(self.metrics.average_process_to_signal_latency_ms, 3),
            "avg_total_signal_latency_ms": round(self.metrics.average_total_signal_latency_ms, 3),
            "option_data_latency_ms": round(self.metrics.option_data_latency_ms, 3),
            "engine_exec_ms": dict(self.metrics.engine_exec_ms),
            "confidence_distribution": dict(self.metrics.confidence_distribution),
            "total_trades": self.metrics.total_trades,
            "winning_trades": self.metrics.winning_trades,
            "losing_trades": self.metrics.losing_trades,
            "win_rate": round(self.metrics.win_rate, 2),
            "total_pnl": round(self.metrics.total_pnl, 2),
            "avg_win": round(self.metrics.avg_win, 2),
            "avg_loss": round(self.metrics.avg_loss, 2),
            "current_day_pnl": round(self.metrics.current_day_pnl, 2),
            "last_5_trades": list(self.metrics.last_5_trades),
        }

