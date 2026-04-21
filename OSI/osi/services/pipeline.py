from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

from osi.core.models import Candle, EngineOutput, MarketTick
from osi.engines.consensus_engine import ConsensusEngine
from osi.engines.hero_zero_engine import HeroZeroEngine
from osi.engines.market_regime_engine import MarketRegimeEngine
from osi.engines.mean_reversion_engine import MeanReversionEngine
from osi.engines.ml_engine import MLEngine
from osi.engines.option_chain_engine import OptionChainEngine
from osi.engines.scalping_engine import ScalpingEngine
from osi.engines.smc_engine import SMCEngine
from osi.engines.trend_engine import TrendEngine
from osi.infra.postgres_repo import PostgresRepository
from osi.infra.redis_store import RedisStateStore
from osi.services.candle_builder import CandleBuilder
from osi.services.metrics import MetricsService
from osi.services.signal_manager import SignalManager

logger = logging.getLogger(__name__)


class OSIPipeline:
    def __init__(
        self,
        redis_store: RedisStateStore,
        postgres_repo: PostgresRepository,
        metrics: MetricsService,
    ) -> None:
        self.redis_store = redis_store
        self.postgres_repo = postgres_repo
        self.metrics = metrics
        self.signal_manager = SignalManager()
        self.consensus = ConsensusEngine()
        self.regime_engine = MarketRegimeEngine()
        self.engine_state: Dict[str, Any] = {}
        self.tick_queue: asyncio.Queue[MarketTick] = asyncio.Queue(maxsize=10000)
        self.subscribers: List[asyncio.Queue[Dict[str, Any]]] = []
        self._worker_task: asyncio.Task | None = None
        self.candle_builder = CandleBuilder()
        self.decision_history: List[Dict[str, Any]] = []
        self._prev_candle: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._vwap_state: Dict[str, Dict[str, float]] = {}
        self._vwap_day: Dict[str, str] = {}

        self.engines = [
            ScalpingEngine(),
            SMCEngine(),
            HeroZeroEngine(),
            TrendEngine(),
            MeanReversionEngine(),
            OptionChainEngine(),
        ]
        self.ml_engine = MLEngine()

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def on_tick(self, tick: MarketTick) -> None:
        await self.tick_queue.put(tick)

    async def _worker(self) -> None:
        while True:
            tick = await self.tick_queue.get()
            process_start = time.perf_counter()
            try:
                closed_candles = self.candle_builder.add_tick(tick)
                payloads: List[Dict[str, Any]] = []
                for candle in closed_candles:
                    payload = await self._process_candle(candle, tick)
                    payloads.append(payload)
                    for sub in self.subscribers:
                        if not sub.full():
                            sub.put_nowait(payload)
                if not payloads:
                    # Lifecycle updates still run at tick level.
                    self.signal_manager._update_lifecycle(tick)
            except Exception:
                logger.exception("Pipeline tick failed")
                self.metrics.record_error()
            finally:
                elapsed_ms = (time.perf_counter() - process_start) * 1000.0
                self.metrics.record_tick(elapsed_ms)
                self.tick_queue.task_done()

    async def _process_candle(self, candle: Candle, source_tick: MarketTick) -> Dict[str, Any]:
        prev = self._prev_candle.get((candle.symbol, candle.timeframe))
        tick = self._market_tick_from_candle(candle, source_tick.received_at, prev)
        self._prev_candle[(candle.symbol, candle.timeframe)] = candle.model_dump(mode="json")
        started = time.perf_counter()
        outputs: List[EngineOutput] = []
        for engine in self.engines:
            t0 = time.perf_counter()
            out = engine.evaluate(tick, self.engine_state)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.metrics.record_engine_latency(out.engine, elapsed_ms)
            outputs.append(out)

        ml_out = self.ml_engine.evaluate(tick, self.engine_state)
        outputs.append(ml_out)
        regime = self.regime_engine.detect(candle)
        tick.meta["regime"] = regime
        if regime == "SIDEWAYS":
            filtered: List[EngineOutput] = []
            for row in outputs:
                if row.engine != "mean_reversion_engine":
                    filtered.append(EngineOutput(engine=row.engine, signal="NONE", strength=0.0))
                else:
                    filtered.append(row)
            outputs = filtered
            logger.debug("SIDEWAYS regime filter applied: mean_reversion_only symbol=%s", tick.symbol)
        consensus = self.consensus.combine(outputs, regime=regime)
        self.metrics.record_confidence(consensus.confidence)

        signal = self.signal_manager.process_consensus(tick, consensus)
        self.decision_history.append(
            {
                "created_at": tick.timestamp.isoformat(),
                "symbol": tick.symbol,
                "signal": consensus.signal,
                "confidence": consensus.confidence,
                "status": "EXECUTED" if signal is not None else "CANDIDATE_BLOCKED",
                "entry_price": (signal.entry_price if signal is not None else None),
                "stop_loss": (signal.stop_loss if signal is not None else None),
                "target_price": (signal.target_price if signal is not None else None),
                "strike": (signal.strike if signal is not None else None),
                "current_ltp": (signal.current_ltp if signal is not None else None),
                "exit_price": (signal.exit_price if signal is not None else None),
                "pnl": (signal.pnl if signal is not None else None),
            }
        )
        if len(self.decision_history) > 1000:
            self.decision_history = self.decision_history[-1000:]
        if signal is not None:
            await self.postgres_repo.store_signal(
                {
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "signal": signal.engine_signal,
                    "confidence": signal.confidence,
                    "status": signal.status.value,
                    "entry_price": signal.entry_price,
                    "stop_loss": signal.stop_loss,
                    "target_price": signal.target_price,
                    "option_symbol": signal.option_symbol,
                    "created_at": signal.created_at,
                    "closed_at": signal.closed_at,
                    "pnl": signal.pnl,
                    "lifecycle_events": signal.lifecycle_events,
                }
            )

        snapshot_payload = {
            "symbol": tick.symbol,
            "index_price": tick.index_price,
            "timestamp": tick.timestamp.isoformat(),
            "candle": candle.model_dump(mode="json"),
            "regime": regime,
            "engine_outputs": [row.model_dump() for row in outputs],
            "consensus": consensus.model_dump(),
            "active_signal": signal.model_dump() if signal else None,
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
        }
        tick_to_process_ms = (datetime.now(timezone.utc) - source_tick.received_at).total_seconds() * 1000.0
        process_to_signal_ms = (time.perf_counter() - started) * 1000.0
        total_signal_latency_ms = max(0.0, (datetime.now(timezone.utc) - source_tick.received_at).total_seconds() * 1000.0)
        self.metrics.record_latency_breakdown(
            tick_to_process_latency_ms=max(0.0, tick_to_process_ms),
            process_to_signal_latency_ms=max(0.0, process_to_signal_ms),
            total_signal_latency_ms=total_signal_latency_ms,
        )
        snapshot_payload["latency_ms"] = {
            "tick_to_process": round(max(0.0, tick_to_process_ms), 3),
            "process_to_signal": round(max(0.0, process_to_signal_ms), 3),
            "total_signal": round(total_signal_latency_ms, 3),
        }
        await self.redis_store.set_json(f"osi:live:{tick.symbol}", snapshot_payload)
        await self.postgres_repo.store_engine_snapshot(
            snapshot_id=f"ENG-{uuid4().hex[:12]}",
            symbol=tick.symbol,
            ts=datetime.now(timezone.utc),
            payload=snapshot_payload,
        )
        return snapshot_payload

    def _market_tick_from_candle(self, candle: Candle, received_at: datetime, prev_candle: Dict[str, Any] | None) -> MarketTick:
        vwap = self._update_vwap(candle)
        return MarketTick(
            symbol=candle.symbol,
            index_price=candle.close,
            received_at=received_at,
            timestamp=candle.end,
            option_chain=candle.option_chain,
            meta={
                "timeframe": candle.timeframe,
                "candle": candle.model_dump(mode="json"),
                "prev_candle": prev_candle or {},
                "vwap": vwap,
                **(candle.meta or {}),
            },
        )

    def _update_vwap(self, candle: Candle) -> float:
        day_key = candle.end.astimezone(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
        if self._vwap_day.get(candle.symbol) != day_key:
            self._vwap_day[candle.symbol] = day_key
            self._vwap_state[candle.symbol] = {"pv": 0.0, "vol": 0.0}
        st = self._vwap_state.setdefault(candle.symbol, {"pv": 0.0, "vol": 0.0})
        vol = float(candle.volume if candle.volume > 0 else 1.0)
        typ = (float(candle.high) + float(candle.low) + float(candle.close)) / 3.0
        st["pv"] += typ * vol
        st["vol"] += vol
        return float(st["pv"] / max(st["vol"], 1e-6))

    def subscribe(self) -> asyncio.Queue[Dict[str, Any]]:
        q: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=200)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[Dict[str, Any]]) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

