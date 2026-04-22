from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

from osi.core.config import settings
from osi.core.models import Candle, ConsensusOutput, EngineOutput, MarketTick
from osi.engines.consensus_engine import ConsensusEngine
from osi.engines.hero_zero_engine import HeroZeroEngine
from osi.engines.intrabar_engine import IntrabarEngine
from osi.engines.market_regime_engine import MarketRegimeEngine
from osi.engines.mean_reversion_engine import MeanReversionEngine
from osi.engines.ml_engine import MLEngine
from osi.engines.option_chain_engine import OptionChainEngine
from osi.engines.scalping_engine import ScalpingEngine
from osi.engines.smart_breakout_engine import SmartBreakoutEngine
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
        self.signal_manager = SignalManager(metrics=self.metrics)
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
        self._latest_regime: Dict[str, str] = {}
        self._latest_intrabar_output: Dict[str, Dict[str, Any]] = {}
        self._last_intrabar_trigger_ts: Dict[str, datetime] = {}
        self._last_live_snapshot: Dict[str, Dict[str, Any]] = {}
        self._last_state_emit_ts: Dict[str, float] = {}

        self.scalping_engine = ScalpingEngine()
        self.intrabar_engine = IntrabarEngine()
        self.engines = [
            self.scalping_engine,
            SmartBreakoutEngine(),
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
                tick.meta["regime"] = self._latest_regime.get(tick.symbol, "SIDEWAYS")
                payloads: List[Dict[str, Any]] = []
                intrabar_payload = await self._process_intrabar_engine(tick)
                if intrabar_payload is not None:
                    payloads.append(intrabar_payload)
                    self._fanout_payload(intrabar_payload, tag="intrabar")
                for candle in closed_candles:
                    payload = await self._process_candle(candle, tick)
                    payloads.append(payload)
                    self._fanout_payload(payload, tag="candle")
                if not payloads:
                    # Lifecycle updates still run at tick level.
                    self.signal_manager._update_lifecycle(tick)
                    now_ts = time.time()
                    last_emit = float(self._last_state_emit_ts.get(tick.symbol) or 0.0)
                    # Throttle state-only pushes to keep dashboard rendering stable.
                    if (now_ts - last_emit) >= 0.6:
                        self._last_state_emit_ts[tick.symbol] = now_ts
                        state_payload = self._build_state_payload(tick)
                        await self.redis_store.set_json(f"osi:live:{tick.symbol}", state_payload)
                        self._fanout_payload(state_payload, tag="state")
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
        regime = self.regime_engine.detect(candle)
        self._latest_regime[candle.symbol] = regime
        tick.meta["regime"] = regime
        tick.meta["intrabar_data"] = self._intrabar_context(source_tick)
        self._prev_candle[(candle.symbol, candle.timeframe)] = candle.model_dump(mode="json")
        started = time.perf_counter()
        outputs: List[EngineOutput] = []
        cached_intrabar = self._latest_intrabar_output.get(tick.symbol)
        if cached_intrabar is not None:
            intrabar_ts = cached_intrabar.get("ts")
            if isinstance(intrabar_ts, datetime) and (datetime.now(timezone.utc) - intrabar_ts).total_seconds() <= 5:
                outputs.append(cached_intrabar["output"])
        for engine in self.engines:
            t0 = time.perf_counter()
            out = engine.evaluate(tick, self.engine_state)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.metrics.record_engine_latency(out.engine, elapsed_ms)
            outputs.append(out)

        ml_out = self.ml_engine.evaluate(tick, self.engine_state)
        outputs.append(ml_out)
        # Regime is advisory only; engines are no longer hard-blocked by regime.
        consensus = self.consensus.combine(outputs, regime=regime)
        sb_row = next((r for r in outputs if r.engine == "smart_breakout_engine"), None)
        ib_row = next((r for r in outputs if r.engine == "intrabar_engine"), None)
        last_intrabar_ts = self._last_intrabar_trigger_ts.get(tick.symbol)
        if (
            sb_row is not None
            and sb_row.signal != "NONE"
            and isinstance(last_intrabar_ts, datetime)
            and (datetime.now(timezone.utc) - last_intrabar_ts).total_seconds() <= 5.0
        ):
            # Overlap control: dampen candle smart-breakout if intrabar just triggered the same move window.
            sb_row.strength = round(max(0.0, float(sb_row.strength) * 0.7), 3)
            sb_row.confidence = round(max(0.0, float(sb_row.confidence or 0.0) - 10.0), 2)
            sb_row.reason = f"{sb_row.reason or ''}|overlap_dampened".strip("|")
        if sb_row and float(sb_row.confidence or 0.0) >= 65.0 and sb_row.signal != "NONE":
            consensus.signal = sb_row.signal
            consensus.confidence = max(float(consensus.confidence), float(sb_row.confidence or 0.0))
            consensus.weighted_score = float(sb_row.strength)
        if ib_row and float(ib_row.confidence or 0.0) >= 60.0 and ib_row.signal != "NONE":
            consensus.signal = ib_row.signal
            consensus.confidence = max(float(consensus.confidence), float(ib_row.confidence or 0.0))
            consensus.weighted_score = float(ib_row.strength)
        self.metrics.record_confidence(consensus.confidence)

        signal = self.signal_manager.process_consensus(tick, consensus)
        self._append_decision(tick, consensus, signal)
        if signal is not None:
            await self._persist_signal(signal)

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
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "decision_note": self.signal_manager.latest_decision_note(tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
        }
        tick_to_process_ms = self._real_ingest_latency_ms(source_tick)
        process_to_signal_ms = (time.perf_counter() - started) * 1000.0
        if source_tick.exchange_timestamp is not None:
            total_signal_latency_ms = max(
                0.0, (datetime.now(timezone.utc) - source_tick.exchange_timestamp).total_seconds() * 1000.0
            )
        else:
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
        self._last_live_snapshot[tick.symbol] = snapshot_payload
        await self.postgres_repo.store_engine_snapshot(
            snapshot_id=f"ENG-{uuid4().hex[:12]}",
            symbol=tick.symbol,
            ts=datetime.now(timezone.utc),
            payload=snapshot_payload,
        )
        return snapshot_payload

    async def _process_intrabar_engine(self, tick: MarketTick) -> Dict[str, Any] | None:
        live_candle = self.candle_builder.get_live_candle(tick.symbol, "1m")
        prev_candle = self.candle_builder.get_last_closed(tick.symbol, "1m")
        if not live_candle or not prev_candle:
            return None
        live_candle_json = dict(live_candle)
        for k in ("start", "end"):
            v = live_candle_json.get(k)
            if isinstance(v, datetime):
                live_candle_json[k] = v.isoformat()
        vwap_st = self._vwap_state.get(tick.symbol) or {"pv": 0.0, "vol": 0.0}
        vwap = (
            float(vwap_st["pv"] / max(vwap_st["vol"], 1e-6))
            if float(vwap_st.get("vol") or 0.0) > 0.0
            else float(tick.index_price)
        )
        intrabar_tick = MarketTick(
            symbol=tick.symbol,
            index_price=tick.index_price,
            received_at=tick.received_at,
            timestamp=tick.timestamp,
            option_chain=tick.option_chain,
            meta={
                "timeframe": "intrabar",
                "live_candle": live_candle,
                "prev_candle": prev_candle,
                "candle": live_candle,
                "vwap": vwap,
                "regime": tick.meta.get("regime"),
            },
        )
        t0 = time.perf_counter()
        intrabar = self.intrabar_engine.evaluate(intrabar_tick, self.engine_state)
        self.metrics.record_engine_latency(intrabar.engine, (time.perf_counter() - t0) * 1000.0)
        if intrabar.signal == "NONE":
            return None
        self._last_intrabar_trigger_ts[tick.symbol] = datetime.now(timezone.utc)

        confidence = max(float(settings.intrabar_confidence_floor), float(intrabar.confidence or 0.0), round(intrabar.strength * 100.0, 2))
        if confidence < 50.0:
            return None
        consensus = ConsensusOutput(
            signal=intrabar.signal,
            confidence=confidence,
            weighted_score=round(intrabar.strength, 4),
            engine_outputs=[intrabar],
        )
        signal = self.signal_manager.process_consensus(intrabar_tick, consensus)
        self._append_decision(intrabar_tick, consensus, signal)
        self._latest_intrabar_output[tick.symbol] = {"ts": datetime.now(timezone.utc), "output": intrabar}
        if signal is not None:
            await self._persist_signal(signal)
        payload = {
            "symbol": intrabar_tick.symbol,
            "index_price": intrabar_tick.index_price,
            "timestamp": intrabar_tick.timestamp.isoformat(),
            "candle": {"timeframe": "intrabar", **live_candle_json},
            "regime": intrabar_tick.meta.get("regime"),
            "engine_outputs": [intrabar.model_dump()],
            "consensus": consensus.model_dump(),
            "active_signal": signal.model_dump() if signal else None,
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "decision_note": self.signal_manager.latest_decision_note(intrabar_tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(intrabar_tick.symbol),
            "entry_mode": "intrabar_priority",
        }
        await self.redis_store.set_json(f"osi:live:{tick.symbol}", payload)
        self._last_live_snapshot[tick.symbol] = payload
        await self.postgres_repo.store_engine_snapshot(
            snapshot_id=f"ENG-{uuid4().hex[:12]}",
            symbol=tick.symbol,
            ts=datetime.now(timezone.utc),
            payload=payload,
        )
        return payload

    @staticmethod
    def _real_ingest_latency_ms(tick: MarketTick) -> float:
        if tick.exchange_timestamp is None:
            return max(0.0, (datetime.now(timezone.utc) - tick.received_at).total_seconds() * 1000.0)
        return max(0.0, (tick.received_at - tick.exchange_timestamp).total_seconds() * 1000.0)

    def _fanout_payload(self, payload: Dict[str, Any], *, tag: str) -> None:
        dropped = 0
        for sub in self.subscribers:
            if sub.full():
                dropped += 1
                continue
            sub.put_nowait(payload)
        if dropped > 0:
            logger.warning(
                "Dropped websocket payloads tag=%s dropped_subscribers=%s total_subscribers=%s",
                tag,
                dropped,
                len(self.subscribers),
            )

    def _append_decision(self, tick: MarketTick, consensus: ConsensusOutput, signal: Any) -> None:
        self.decision_history.append(
            {
                "created_at": tick.timestamp.isoformat(),
                "symbol": tick.symbol,
                "signal": consensus.signal,
                "confidence": consensus.confidence,
                "status": "EXECUTED" if signal is not None else "CANDIDATE_BLOCKED",
                "reason": self.signal_manager.latest_decision_note(tick.symbol),
                "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
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

    async def _persist_signal(self, signal: Any) -> None:
        await self.postgres_repo.store_signal(
            {
                "signal_id": signal.signal_id,
                "symbol": signal.symbol,
                "signal": signal.signal,
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

    def _build_state_payload(self, tick: MarketTick) -> Dict[str, Any]:
        last = self._last_live_snapshot.get(tick.symbol, {})
        return {
            "symbol": tick.symbol,
            "index_price": tick.index_price,
            "timestamp": tick.timestamp.isoformat(),
            "candle": last.get("candle"),
            "regime": tick.meta.get("regime") or last.get("regime"),
            "engine_outputs": list(last.get("engine_outputs") or []),
            "consensus": dict(last.get("consensus") or {}),
            "active_signal": last.get("active_signal"),
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "decision_note": self.signal_manager.latest_decision_note(tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
        }

    def _intrabar_context(self, tick: MarketTick) -> Dict[str, float]:
        hist = self.engine_state.setdefault("smart_breakout_tick_hist", {}).setdefault(tick.symbol, [])
        now_ts = float(tick.timestamp.timestamp())
        hist.append((now_ts, float(tick.index_price), float(self._instant_chain_volume(tick))))
        if len(hist) > 120:
            del hist[:-120]
        lookback = now_ts - 3.0
        px_old = hist[0][1]
        vol_recent = 0.0
        for ts, px, vol in hist:
            if ts >= lookback:
                px_old = px
                vol_recent += vol
        return {
            "ltp_now": float(tick.index_price),
            "ltp_3sec_ago": float(px_old),
            "volume_recent": float(vol_recent),
        }

    @staticmethod
    def _instant_chain_volume(tick: MarketTick) -> float:
        total = 0.0
        for row in (tick.option_chain or {}).values():
            if not isinstance(row, dict):
                continue
            total += float(((row.get("CE") or {}).get("volume") or 0.0))
            total += float(((row.get("PE") or {}).get("volume") or 0.0))
        return total

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

