from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

from osi.core.config import settings
from osi.core.market_phase import engine_weight, engine_weights_for_phase, get_market_phase
from osi.core.models import Candle, ConsensusOutput, EngineOutput, MarketTick
from osi.engines.consensus_engine import ConsensusEngine
from osi.engines.hero_zero_engine import HeroZeroEngine
from osi.engines.intrabar_engine import IntrabarEngine
from osi.engines.market_regime_engine import MarketRegimeEngine
from osi.engines.mean_reversion_engine import MeanReversionEngine
from osi.engines.smart_breakout_engine import SmartBreakoutEngine
from osi.infra.postgres_repo import PostgresRepository
from osi.infra.redis_store import RedisStateStore
from osi.services.candle_builder import CandleBuilder
from osi.services.metrics import MetricsService
from osi.services.signal_manager import SignalManager
from osi.services.telegram_notifier import TelegramNotifier

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
        self.execution_paused: bool = False
        self.telegram_notifier = TelegramNotifier(
            command_handler=self._handle_telegram_command,
            dashboard_summary_provider=self._telegram_dashboard_summary,
        )
        self.signal_manager = SignalManager(metrics=self.metrics, notifier=self.telegram_notifier)
        self.consensus = ConsensusEngine()
        self.regime_engine = MarketRegimeEngine()
        self.engine_state: Dict[str, Any] = {}
        self.tick_queue: asyncio.Queue[MarketTick] = asyncio.Queue(maxsize=10000)
        self.subscribers: List[asyncio.Queue[Dict[str, Any]]] = []
        self._worker_task: asyncio.Task | None = None
        self._live_ltp_task: asyncio.Task | None = None
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
        self._pending_intrabar: Dict[str, Dict[str, Any]] = {}
        self._latest_strategy_signals: Dict[str, List[Dict[str, Any]]] = {}
        self._last_intrabar_redis_mono: Dict[str, float] = {}
        self.live_index_data: Dict[str, Dict[str, Any]] = {
            "NIFTY": {
                "ltp": 0.0,
                "change": 0.0,
                "change_percent": 0.0,
                "trend": "FLAT",
                "timestamp": None,
                "ticks": [],
            },
            "SENSEX": {
                "ltp": 0.0,
                "change": 0.0,
                "change_percent": 0.0,
                "trend": "FLAT",
                "timestamp": None,
                "ticks": [],
            },
        }

        self.intrabar_engine = IntrabarEngine()
        self.engines = [
            SmartBreakoutEngine(),
            HeroZeroEngine(),
            MeanReversionEngine(),
        ]
        self.ml_engine = None

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
            active = []
            for sym in ("NIFTY", "SENSEX"):
                active.extend(self.signal_manager._active_positions(sym))
            try:
                self.telegram_notifier.notify_system_started(active_count=len(active))
                self.telegram_notifier.notify_active_snapshot(active)
            except Exception:
                logger.exception("Telegram startup notification failed")
        if self._live_ltp_task is None and bool(getattr(settings, "telegram_live_ltp_enabled", True)):
            self._live_ltp_task = asyncio.create_task(self._live_ltp_loop())

    async def stop(self, *, reason: str = "shutdown") -> None:
        if self._live_ltp_task:
            self._live_ltp_task.cancel()
            try:
                await self._live_ltp_task
            except asyncio.CancelledError:
                pass
            self._live_ltp_task = None
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        try:
            self.telegram_notifier.notify_system_stopped(reason=reason)
        except Exception:
            logger.exception("Telegram stop notification failed")
        self.telegram_notifier.close()

    async def _live_ltp_loop(self) -> None:
        """Refresh sticky Telegram trackers for every active signal at a fixed cadence."""
        interval = max(5.0, float(getattr(settings, "telegram_live_ltp_interval_sec", 30.0)))
        try:
            while True:
                await asyncio.sleep(interval)
                if not getattr(self.telegram_notifier, "enabled", False):
                    continue
                if not bool(getattr(settings, "telegram_live_ltp_enabled", True)):
                    continue
                try:
                    for sym in self.signal_manager.enabled_symbols:
                        for sig in self.signal_manager._active_positions(sym):
                            self.telegram_notifier.update_signal_live(sig)
                except Exception:
                    logger.exception("Live LTP refresh failed")
        except asyncio.CancelledError:
            raise

    async def on_tick(self, tick: MarketTick) -> None:
        self.metrics.record_tick_received()
        if tick.symbol not in self.signal_manager.enabled_symbols:
            self.metrics.record_tick_dropped()
            self.signal_manager.record_rejected_event(
                symbol=str(tick.symbol),
                signal="NONE",
                confidence=0.0,
                reason="symbol_disabled",
                notes=[f"enabled_symbols={','.join(self.signal_manager.enabled_symbols)}"],
            )
            return
        await self.tick_queue.put(tick)

    async def _worker(self) -> None:
        while True:
            tick = await self.tick_queue.get()
            process_start = time.perf_counter()
            try:
                self._update_live_index_data(tick)
                closed_candles = self.candle_builder.add_tick(tick)
                tick.meta["regime"] = self._latest_regime.get(tick.symbol, "SIDEWAYS")
                payloads: List[Dict[str, Any]] = []
                confirmation_payload = await self._process_pending_intrabar_confirmation(tick)
                if confirmation_payload is not None:
                    payloads.append(confirmation_payload)
                    self._fanout_payload(confirmation_payload, tag="intrabar_confirmed")
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
        market_phase = get_market_phase(source_tick.timestamp)
        tick.meta["market_phase"] = market_phase
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
        strategy_signals = self._build_strategy_signals(
            tick=tick,
            outputs=outputs,
            source=f"candle_{candle.timeframe}",
        )
        if not strategy_signals and bool(getattr(settings, "strategy_fallback_enabled", True)):
            fallback = self._build_fallback_strategy_signal(
                tick=tick,
                source=f"candle_{candle.timeframe}",
            )
            if fallback is not None:
                strategy_signals = [fallback]
        self._latest_strategy_signals[tick.symbol] = strategy_signals

        self.signal_manager.record_engine_signals(tick, outputs, source=f"candle_{candle.timeframe}")
        # Deterministic decision layer:
        # zero_hero > strong intrabar >=0.7 > smart_breakout > NONE
        ib_row = next((r for r in outputs if r.engine == "intrabar_engine"), None)
        if ib_row is not None and ib_row.signal != "NONE" and float(ib_row.strength or 0.0) >= 0.7:
            phase_weight = engine_weight(ib_row.engine, market_phase)
            consensus = ConsensusOutput(
                signal=ib_row.signal,
                confidence=round(
                    min(
                        100.0,
                        float(
                            ib_row.confidence
                            if ib_row.confidence is not None
                            else max(50.0, float(ib_row.strength or 0.0) * 100.0)
                        )
                        * phase_weight,
                    ),
                    2,
                ),
                weighted_score=round(float(ib_row.strength or 0.0) * phase_weight, 4),
                engine_outputs=[ib_row],
                market_phase=market_phase,
                engine_weights=engine_weights_for_phase(market_phase),
                selected_engine=ib_row.engine,
            )
        else:
            consensus = self.consensus.combine(outputs, regime=regime, market_phase=market_phase)
        logger.info(
            "MARKET PHASE: %s symbol=%s weights=%s selected_engine=%s",
            market_phase,
            tick.symbol,
            consensus.engine_weights,
            consensus.selected_engine or "-",
        )
        logger.info(
            "SIGNAL FLOW symbol=%s phase=%s engines=%s decision=%s conf=%.2f selected_engine=%s",
            tick.symbol,
            market_phase,
            ",".join(f"{o.engine}:{o.signal}:{float(o.strength or 0.0):.3f}" for o in outputs),
            consensus.signal,
            float(consensus.confidence or 0.0),
            consensus.selected_engine or "-",
        )
        sb_row = next((r for r in outputs if r.engine == "smart_breakout_engine"), None)
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
        self.metrics.record_confidence(consensus.confidence)

        signal = self.signal_manager.process_consensus(tick, consensus)
        logger.info(
            "SIGNAL FLOW RESULT symbol=%s decision=%s executed=%s signal_id=%s",
            tick.symbol,
            consensus.signal,
            "yes" if signal is not None else "no",
            (signal.signal_id if signal is not None else "-"),
        )
        self._append_decision(tick, consensus, signal)
        if signal is not None:
            await self._persist_signal(signal)

        snapshot_payload = {
            "symbol": tick.symbol,
            "index_price": tick.index_price,
            "timestamp": tick.timestamp.isoformat(),
            "candle": candle.model_dump(mode="json"),
            "regime": regime,
            "market_phase": market_phase,
            "engine_outputs": [row.model_dump() for row in outputs],
            "strategy_signals": strategy_signals,
            "consensus": consensus.model_dump(),
            "active_signal": signal.model_dump() if signal else None,
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "paper_signals": self.signal_manager.snapshot_paper_signals()[-50:],
            "decision_note": self.signal_manager.latest_decision_note(tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
            "market_indices": self.snapshot_market_indices(),
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
        snapshot_payload["entry_mode"] = f"candle_{candle.timeframe}"
        self._log_latency_path(
            snapshot_payload["entry_mode"],
            tick.symbol,
            source_tick,
            tick_to_process_ms,
            process_to_signal_ms,
        )
        await self._publish_live_snapshot(
            tick.symbol,
            snapshot_payload,
            path=snapshot_payload["entry_mode"],
            force_redis=True,
            persist_pg=True,
        )
        return snapshot_payload

    async def _publish_live_snapshot(
        self,
        symbol: str,
        payload: Dict[str, Any],
        *,
        path: str,
        force_redis: bool = False,
        persist_pg: bool = True,
    ) -> None:
        self._last_live_snapshot[symbol] = payload
        interval_ms = float(getattr(settings, "intrabar_redis_min_interval_ms", 0.0) or 0.0)
        intrabar_snapshot_paths = {
            "intrabar_direct_execution",
            "intrabar_pending_confirmation",
            "intrabar_confirmed_execution",
        }
        throttle = path in intrabar_snapshot_paths and interval_ms > 0 and not force_redis
        if throttle:
            now_m = time.perf_counter()
            last = float(self._last_intrabar_redis_mono.get(symbol) or 0.0)
            if (now_m - last) * 1000.0 < interval_ms:
                return
            self._last_intrabar_redis_mono[symbol] = now_m
        await self.redis_store.set_json(f"osi:live:{symbol}", payload)
        if persist_pg:
            await self.postgres_repo.store_engine_snapshot(
                snapshot_id=f"ENG-{uuid4().hex[:12]}",
                symbol=symbol,
                ts=datetime.now(timezone.utc),
                payload=payload,
            )

    def _log_latency_path(
        self,
        path: str,
        symbol: str,
        tick: MarketTick,
        tick_to_process_ms: float,
        process_to_signal_ms: float,
    ) -> None:
        if not bool(getattr(settings, "latency_path_log_enabled", True)):
            return
        if tick.exchange_timestamp is not None:
            total_ms = max(
                0.0, (datetime.now(timezone.utc) - tick.exchange_timestamp).total_seconds() * 1000.0
            )
        else:
            total_ms = max(0.0, (datetime.now(timezone.utc) - tick.received_at).total_seconds() * 1000.0)
        try:
            qdepth = self.tick_queue.qsize()
        except Exception:
            qdepth = -1
        logger.info(
            "LATENCY_PATH path=%s symbol=%s tick_to_process_ms=%.2f process_to_signal_ms=%.2f total_signal_ms=%.2f queue_depth=%s",
            path,
            symbol,
            max(0.0, tick_to_process_ms),
            max(0.0, process_to_signal_ms),
            total_ms,
            qdepth,
        )

    async def _process_intrabar_engine(self, tick: MarketTick) -> Dict[str, Any] | None:
        live_candle = self.candle_builder.get_live_candle(tick.symbol, "1m")
        prev_candle = self.candle_builder.get_last_closed(tick.symbol, "1m")
        if not live_candle or not prev_candle:
            self.signal_manager.record_rejected_event(
                symbol=tick.symbol,
                reason="intrabar_context_missing",
                signal="NONE",
                confidence=0.0,
                notes=["live_or_prev_candle_missing"],
            )
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
        market_phase = get_market_phase(tick.timestamp)
        intrabar_tick = MarketTick(
            symbol=tick.symbol,
            index_price=tick.index_price,
            received_at=tick.received_at,
            exchange_timestamp=tick.exchange_timestamp,
            timestamp=tick.timestamp,
            option_chain=tick.option_chain,
            meta={
                "timeframe": "intrabar",
                "live_candle": live_candle,
                "prev_candle": prev_candle,
                "candle": live_candle,
                "vwap": vwap,
                "regime": tick.meta.get("regime"),
                "market_phase": market_phase,
            },
        )
        t0 = time.perf_counter()
        intrabar = self.intrabar_engine.evaluate(intrabar_tick, self.engine_state)
        self.metrics.record_engine_latency(intrabar.engine, (time.perf_counter() - t0) * 1000.0)
        if intrabar.signal == "NONE":
            self.signal_manager.record_rejected_event(
                symbol=tick.symbol,
                reason="intrabar_no_signal",
                signal="NONE",
                confidence=float(intrabar.confidence or 0.0),
                notes=[f"strength={float(intrabar.strength or 0.0):.3f}"],
            )
            return None
        self.signal_manager.record_engine_signals(intrabar_tick, [intrabar], source="intrabar")
        intrabar_strategy = self._build_strategy_signals(
            tick=intrabar_tick,
            outputs=[intrabar],
            source="intrabar",
        )
        existing = list(self._latest_strategy_signals.get(tick.symbol) or [])
        merged = [row for row in existing if row.get("engine") != intrabar.engine]
        merged.extend(intrabar_strategy)
        self._latest_strategy_signals[tick.symbol] = merged[-20:]
        strong_ib = float(intrabar.strength or 0.0) >= 0.7
        if strong_ib:
            self._pending_intrabar.pop(tick.symbol, None)
            now = datetime.now(timezone.utc)
            confidence = max(
                float(settings.intrabar_confidence_floor),
                float(intrabar.confidence or 0.0),
                round(float(intrabar.strength or 0.0) * 100.0, 2),
            )
            phase_weight = engine_weight(intrabar.engine, market_phase)
            consensus = ConsensusOutput(
                signal=intrabar.signal,
                confidence=round(min(100.0, float(confidence) * phase_weight), 2),
                weighted_score=round(float(intrabar.strength or 0.0) * phase_weight, 4),
                engine_outputs=[intrabar],
                market_phase=market_phase,
                engine_weights=engine_weights_for_phase(market_phase),
                selected_engine=intrabar.engine,
            )
            self.metrics.record_confidence(consensus.confidence)
            ib_started = time.perf_counter()
            signal = self.signal_manager.process_consensus(intrabar_tick, consensus)
            proc_ib_ms = (time.perf_counter() - ib_started) * 1000.0
            self._append_decision(intrabar_tick, consensus, signal)
            self._latest_intrabar_output[tick.symbol] = {"ts": now, "output": intrabar}
            self._last_intrabar_trigger_ts[tick.symbol] = now
            if signal is not None:
                await self._persist_signal(signal)
            tick_ms_ib = self._real_ingest_latency_ms(tick)
            payload = {
                "symbol": intrabar_tick.symbol,
                "index_price": intrabar_tick.index_price,
                "timestamp": intrabar_tick.timestamp.isoformat(),
                "candle": {"timeframe": "intrabar", **live_candle_json},
                "regime": intrabar_tick.meta.get("regime"),
                "market_phase": market_phase,
                "engine_outputs": [intrabar.model_dump()],
                "strategy_signals": list(self._latest_strategy_signals.get(tick.symbol) or []),
                "consensus": consensus.model_dump(),
                "pending_signal": None,
                "active_signal": signal.model_dump() if signal else None,
                "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
                "active_signals": self.signal_manager.snapshot_active_signals(),
                "history": self.signal_manager.snapshot_history(),
                "rejected_signals": self.signal_manager.snapshot_rejected(),
                "paper_signals": self.signal_manager.snapshot_paper_signals()[-50:],
                "decision_note": self.signal_manager.latest_decision_note(intrabar_tick.symbol),
                "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(intrabar_tick.symbol),
                "entry_mode": "intrabar_direct_execution",
                "market_indices": self.snapshot_market_indices(),
                "latency_ms": {
                    "tick_to_process": round(max(0.0, tick_ms_ib), 3),
                    "process_to_signal": round(max(0.0, proc_ib_ms), 3),
                    "total_signal": round(
                        max(
                            0.0,
                            (datetime.now(timezone.utc) - tick.exchange_timestamp).total_seconds() * 1000.0,
                        )
                        if tick.exchange_timestamp is not None
                        else max(
                            0.0,
                            (datetime.now(timezone.utc) - tick.received_at).total_seconds() * 1000.0,
                        ),
                        3,
                    ),
                },
            }
            self._log_latency_path(
                "intrabar_direct_execution",
                intrabar_tick.symbol,
                tick,
                tick_ms_ib,
                proc_ib_ms,
            )
            await self._publish_live_snapshot(
                tick.symbol,
                payload,
                path="intrabar_direct_execution",
                force_redis=signal is not None,
                persist_pg=True,
            )
            return payload

        if tick.symbol in self._pending_intrabar:
            self.signal_manager.record_rejected_event(
                symbol=tick.symbol,
                reason="intrabar_pending_already_exists",
                signal=str(intrabar.signal),
                confidence=float(intrabar.confidence or 0.0),
            )
            return None
        created_at = datetime.now(timezone.utc)
        self._pending_intrabar[tick.symbol] = {
            "created_at": created_at,
            "activate_after": created_at + timedelta(seconds=1),
            "expires_at": created_at + timedelta(seconds=3),
            "trigger_price": float(tick.index_price),
            "vwap": float(vwap),
            "output": intrabar,
        }
        payload = {
            "symbol": intrabar_tick.symbol,
            "index_price": intrabar_tick.index_price,
            "timestamp": intrabar_tick.timestamp.isoformat(),
            "candle": {"timeframe": "intrabar", **live_candle_json},
            "regime": intrabar_tick.meta.get("regime"),
            "market_phase": market_phase,
            "engine_outputs": [intrabar.model_dump()],
            "strategy_signals": list(self._latest_strategy_signals.get(tick.symbol) or []),
            "consensus": {
                "signal": intrabar.signal,
                "confidence": intrabar.confidence,
                "weighted_score": intrabar.strength,
                "market_phase": market_phase,
                "engine_weights": engine_weights_for_phase(market_phase),
                "selected_engine": intrabar.engine,
            },
            "pending_signal": {
                "status": "PENDING",
                "signal_tag": "INTRABAR",
                "signal": intrabar.signal,
                "confidence": intrabar.confidence,
                "created_at": created_at.isoformat(),
            },
            "active_signal": None,
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "paper_signals": self.signal_manager.snapshot_paper_signals()[-50:],
            "decision_note": self.signal_manager.latest_decision_note(intrabar_tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(intrabar_tick.symbol),
            "entry_mode": "intrabar_pending_confirmation",
            "market_indices": self.snapshot_market_indices(),
        }
        tm_p = self._real_ingest_latency_ms(tick)
        payload["latency_ms"] = {
            "tick_to_process": round(max(0.0, tm_p), 3),
            "process_to_signal": 0.0,
            "total_signal": round(
                max(
                    0.0,
                    (datetime.now(timezone.utc) - tick.exchange_timestamp).total_seconds() * 1000.0,
                )
                if tick.exchange_timestamp is not None
                else max(
                    0.0,
                    (datetime.now(timezone.utc) - tick.received_at).total_seconds() * 1000.0,
                ),
                3,
            ),
        }
        self._log_latency_path(
            "intrabar_pending_confirmation",
            intrabar_tick.symbol,
            tick,
            tm_p,
            0.0,
        )
        await self._publish_live_snapshot(
            tick.symbol,
            payload,
            path="intrabar_pending_confirmation",
            force_redis=False,
            persist_pg=False,
        )
        return payload

    async def _process_pending_intrabar_confirmation(self, tick: MarketTick) -> Dict[str, Any] | None:
        pending = self._pending_intrabar.get(tick.symbol)
        if not pending:
            return None
        live_candle = self.candle_builder.get_live_candle(tick.symbol, "1m")
        prev_candle = self.candle_builder.get_last_closed(tick.symbol, "1m")
        if not live_candle or not prev_candle:
            self.signal_manager.record_rejected_event(
                symbol=str(tick.symbol),
                reason="intrabar_confirmation_context_missing",
                signal=str(getattr(pending.get("output"), "signal", "NONE")),
                confidence=float(getattr(pending.get("output"), "confidence", 0.0) or 0.0),
            )
            return None
        now = datetime.now(timezone.utc)
        if now < pending["activate_after"]:
            return None
        if now > pending["expires_at"]:
            pending_output = pending.get("output")
            pending_signal = str(getattr(pending_output, "signal", "NONE"))
            pending_conf = float(getattr(pending_output, "confidence", 0.0) or 0.0)
            pending_trigger = float(pending.get("trigger_price") or 0.0)
            pending_vwap = float(pending.get("vwap") or 0.0)
            self.signal_manager.record_rejected_event(
                symbol=str(tick.symbol),
                signal=pending_signal,
                confidence=pending_conf,
                reason="intrabar_pending_expired",
                notes=[f"trigger_price={pending_trigger:.2f}", f"vwap={pending_vwap:.2f}"],
            )
            del self._pending_intrabar[tick.symbol]
            return None

        output: EngineOutput = pending["output"]
        market_phase = get_market_phase(tick.timestamp)
        trigger_price = float(pending["trigger_price"])
        vwap = float(pending["vwap"])
        momentum_ctx = self._intrabar_context(tick)
        ltp_now = float(momentum_ctx.get("ltp_now") or tick.index_price)
        ltp_prev = float(momentum_ctx.get("ltp_3sec_ago") or tick.index_price)
        momentum = ltp_now - ltp_prev
        if output.signal == "BUY_CE":
            confirmed = ltp_now >= trigger_price and ltp_now > vwap and momentum > 0
        else:
            confirmed = ltp_now <= trigger_price and ltp_now < vwap and momentum < 0
        if not confirmed:
            self.signal_manager.record_rejected_event(
                symbol=str(tick.symbol),
                signal=str(output.signal),
                confidence=float(output.confidence or 0.0),
                reason="intrabar_no_confirmation",
                notes=[
                    f"ltp_now={ltp_now:.2f}",
                    f"trigger_price={trigger_price:.2f}",
                    f"vwap={vwap:.2f}",
                    f"momentum={momentum:.3f}",
                ],
            )
            return None

        del self._pending_intrabar[tick.symbol]
        self._last_intrabar_trigger_ts[tick.symbol] = now
        confidence = max(
            float(settings.intrabar_confidence_floor),
            float(output.confidence or 0.0),
            round(output.strength * 100.0, 2),
        )
        phase_weight = engine_weight(output.engine, market_phase)
        if confidence < 50.0:
            self.signal_manager.record_rejected_event(
                symbol=str(tick.symbol),
                signal=str(output.signal),
                confidence=float(confidence),
                reason="intrabar_confirmed_low_confidence",
                notes=["min_required=50.0"],
            )
            return None
        consensus = ConsensusOutput(
            signal=output.signal,
            confidence=round(min(100.0, confidence * phase_weight), 2),
            weighted_score=round(output.strength * phase_weight, 4),
            engine_outputs=[output],
            market_phase=market_phase,
            engine_weights=engine_weights_for_phase(market_phase),
            selected_engine=output.engine,
        )
        confirm_tick = MarketTick(
            symbol=tick.symbol,
            index_price=float(tick.index_price),
            received_at=tick.received_at,
            exchange_timestamp=tick.exchange_timestamp,
            timestamp=tick.timestamp,
            option_chain=tick.option_chain,
            meta={
                "timeframe": "intrabar",
                "live_candle": live_candle,
                "prev_candle": prev_candle,
                "candle": live_candle,
                "vwap": vwap,
                "regime": tick.meta.get("regime"),
                "market_phase": market_phase,
            },
        )
        cf_started = time.perf_counter()
        signal = self.signal_manager.process_consensus(confirm_tick, consensus)
        proc_cf_ms = (time.perf_counter() - cf_started) * 1000.0
        self._append_decision(confirm_tick, consensus, signal)
        self._latest_intrabar_output[tick.symbol] = {"ts": now, "output": output}
        if signal is not None:
            await self._persist_signal(signal)
        tick_cf_ms = self._real_ingest_latency_ms(tick)
        payload = {
            "symbol": tick.symbol,
            "index_price": tick.index_price,
            "timestamp": tick.timestamp.isoformat(),
            "regime": tick.meta.get("regime"),
            "market_phase": market_phase,
            "engine_outputs": [output.model_dump()],
            "strategy_signals": list(self._latest_strategy_signals.get(tick.symbol) or []),
            "consensus": consensus.model_dump(),
            "pending_signal": None,
            "active_signal": signal.model_dump() if signal else None,
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "paper_signals": self.signal_manager.snapshot_paper_signals()[-50:],
            "decision_note": self.signal_manager.latest_decision_note(tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
            "entry_mode": "intrabar_confirmed_execution",
            "market_indices": self.snapshot_market_indices(),
            "latency_ms": {
                "tick_to_process": round(max(0.0, tick_cf_ms), 3),
                "process_to_signal": round(max(0.0, proc_cf_ms), 3),
                "total_signal": round(
                    max(
                        0.0,
                        (datetime.now(timezone.utc) - tick.exchange_timestamp).total_seconds() * 1000.0,
                    )
                    if tick.exchange_timestamp is not None
                    else max(
                        0.0,
                        (datetime.now(timezone.utc) - tick.received_at).total_seconds() * 1000.0,
                    ),
                    3,
                ),
            },
        }
        self._log_latency_path(
            "intrabar_confirmed_execution",
            tick.symbol,
            tick,
            tick_cf_ms,
            proc_cf_ms,
        )
        await self._publish_live_snapshot(
            tick.symbol,
            payload,
            path="intrabar_confirmed_execution",
            force_redis=signal is not None,
            persist_pg=True,
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
                self.metrics.record_tick_dropped()
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
            "strategy_signals": list(self._latest_strategy_signals.get(tick.symbol) or last.get("strategy_signals") or []),
            "consensus": dict(last.get("consensus") or {}),
            "active_signal": last.get("active_signal"),
            "cards": [card.model_dump(mode="json") for card in self.signal_manager.active_cards()],
            "active_signals": self.signal_manager.snapshot_active_signals(),
            "history": self.signal_manager.snapshot_history(),
            "rejected_signals": self.signal_manager.snapshot_rejected(),
            "paper_signals": self.signal_manager.snapshot_paper_signals()[-50:],
            "decision_note": self.signal_manager.latest_decision_note(tick.symbol),
            "confidence_breakdown": self.signal_manager.latest_confidence_breakdown(tick.symbol),
            "market_indices": self.snapshot_market_indices(),
        }

    def _update_live_index_data(self, tick: MarketTick) -> None:
        symbol = str(tick.symbol or "")
        if symbol not in self.live_index_data:
            return
        row = self.live_index_data[symbol]
        ltp_now = float(tick.index_price)
        prev_ltp = float(row.get("ltp") or 0.0)
        if prev_ltp > 0:
            change = ltp_now - prev_ltp
            change_percent = (change / prev_ltp) * 100.0
            trend = "UP" if ltp_now > prev_ltp else "DOWN" if ltp_now < prev_ltp else "FLAT"
        else:
            change = 0.0
            change_percent = 0.0
            trend = "FLAT"
        row["ltp"] = round(ltp_now, 2)
        row["change"] = round(change, 2)
        row["change_percent"] = round(change_percent, 2)
        row["trend"] = trend
        row["timestamp"] = tick.timestamp.isoformat()
        ticks = list(row.get("ticks") or [])
        ticks.append(round(ltp_now, 2))
        if len(ticks) > 10:
            ticks = ticks[-10:]
        row["ticks"] = ticks

    def snapshot_market_indices(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for symbol in ("NIFTY", "SENSEX"):
            row = dict(self.live_index_data.get(symbol) or {})
            out[symbol] = {
                "ltp": float(row.get("ltp") or 0.0),
                "change": float(row.get("change") or 0.0),
                "change_percent": float(row.get("change_percent") or 0.0),
                "trend": str(row.get("trend") or "FLAT"),
                "timestamp": row.get("timestamp"),
                "ticks": list(row.get("ticks") or []),
            }
        return out

    def snapshot_strategy_signals(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for symbol in ("NIFTY", "SENSEX"):
            rows.extend(list(self._latest_strategy_signals.get(symbol) or []))
        rows = sorted(rows, key=lambda r: str(r.get("timestamp") or ""), reverse=True)
        return rows[: max(1, min(limit, 1000))]

    def _build_strategy_signals(self, *, tick: MarketTick, outputs: List[EngineOutput], source: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        now_iso = tick.timestamp.isoformat()
        market_phase = str(tick.meta.get("market_phase") or "OFF")
        for out in outputs:
            if out.signal == "NONE":
                continue
            confidence = OSIPipeline._normalized_strategy_confidence(out)
            contract = self.signal_manager._option_contract_preview(
                tick=tick,
                signal=str(out.signal),
                lead_engine=str(out.engine),
            )
            option_symbol = str((contract or {}).get("option_symbol") or "")
            option_ltp = (contract or {}).get("ltp")
            strike = self.signal_manager._strike_from_option_symbol(option_symbol) if option_symbol else None
            entry_price = round(float(option_ltp), 2) if option_ltp is not None else None
            target_price = round(float(entry_price) * 1.20, 2) if entry_price is not None else None
            rows.append(
                {
                    "id": f"STRAT-{uuid4().hex[:10].upper()}",
                    "timestamp": now_iso,
                    "symbol": tick.symbol,
                    "engine": out.engine,
                    "signal": out.signal,
                    "confidence": confidence,
                    "strength": float(out.strength or 0.0),
                    "strike": strike,
                    "option_symbol": option_symbol,
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "reason": str(out.reason or ""),
                    "source": source,
                    "market_phase": market_phase,
                    "mode": str(getattr(settings, "signal_mode", "consensus") or "consensus"),
                }
            )
        return rows

    @staticmethod
    def _normalized_strategy_confidence(out: EngineOutput) -> float:
        raw = float(out.confidence or 0.0)
        by_strength = float(out.strength or 0.0) * 100.0
        floor = float(getattr(settings, "strategy_confidence_floor", 35.0))
        value = max(raw, by_strength, floor)
        return round(max(0.0, min(100.0, value)), 2)

    @staticmethod
    def _build_fallback_strategy_signal(*, tick: MarketTick, source: str) -> Dict[str, Any] | None:
        candle = tick.meta.get("candle") or {}
        try:
            c_open = float(candle.get("open"))
            c_close = float(candle.get("close"))
            c_high = float(candle.get("high"))
            c_low = float(candle.get("low"))
        except (TypeError, ValueError):
            return None
        c_range = max(0.0, c_high - c_low)
        if c_range <= 0.0:
            return None
        move_ratio = min(1.0, abs(c_close - c_open) / c_range)
        if move_ratio < 0.2:
            return None
        signal = "BUY_CE" if c_close > c_open else "BUY_PE"
        confidence_floor = float(getattr(settings, "strategy_confidence_floor", 35.0))
        confidence = round(max(confidence_floor, min(60.0, 30.0 + (move_ratio * 40.0))), 2)
        return {
            "id": f"STRAT-{uuid4().hex[:10].upper()}",
            "timestamp": tick.timestamp.isoformat(),
            "symbol": tick.symbol,
            "engine": "market_bias_fallback",
            "signal": signal,
            "confidence": confidence,
            "strength": round(move_ratio, 3),
            "strike": None,
            "option_symbol": None,
            "entry_price": None,
            "target_price": None,
            "reason": "fallback_from_candle_pressure",
            "source": source,
            "mode": str(getattr(settings, "signal_mode", "consensus") or "consensus"),
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
        for by_strike in (tick.option_chain or {}).values():
            if not isinstance(by_strike, dict):
                continue
            for row in by_strike.values():
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

    def _handle_telegram_command(self, command: str, args: str, chat_id: str) -> str:
        cmd = str(command or "").strip().lower()
        if cmd == "/pause":
            self.execution_paused = True
            changed = self.signal_manager.set_manual_pause(True)
            return "Execution paused." if changed else "Execution already paused."
        if cmd == "/resume":
            self.execution_paused = False
            changed = self.signal_manager.set_manual_pause(False)
            return "Execution resumed." if changed else "Execution already running."
        if cmd == "/active":
            active = self.signal_manager.snapshot_active_signals()
            if not active:
                return "No active trades."
            lines = ["Active trades:"]
            for row in active[:10]:
                lines.append(
                    (
                        f"{row.get('symbol','')} {row.get('signal','')} {row.get('strategy','')} | "
                        f"Entry {float(row.get('entry_price') or 0.0):.2f} | "
                        f"LTP {float(row.get('current_ltp') or 0.0):.2f} | "
                        f"SL {float(row.get('stop_loss') or 0.0):.2f} | "
                        f"T2 {float(row.get('target_2') or row.get('target_price') or 0.0):.2f}"
                    )
                )
            if len(active) > 10:
                lines.append(f"... and {len(active) - 10} more")
            return "\n".join(lines)
        if cmd == "/status":
            m = self.metrics.snapshot()
            active_count = len(self.signal_manager.snapshot_active_signals())
            paused = bool(self.signal_manager.is_manual_paused())
            return (
                "OSI STATUS\n"
                f"Mode: {'PAUSED' if paused else 'RUNNING'}\n"
                f"Active Trades: {active_count}\n"
                f"Queue Size: {self.tick_queue.qsize()}\n"
                f"Ticks: {int(m.get('ticks_processed') or 0)}\n"
                f"Errors: {int(m.get('pipeline_errors') or 0)}\n"
                f"Avg Signal Latency(ms): {float(m.get('avg_total_signal_latency_ms') or 0.0):.2f}\n"
                f"Last Signal Latency(ms): {float(m.get('total_signal_latency_ms') or 0.0):.2f}\n"
                f"Day PnL: {float(m.get('current_day_pnl') or 0.0):.2f}"
            )
        return "Commands: /status /active /pause /resume /snapshot"

    def _telegram_dashboard_summary(self) -> str:
        """Render a rich live-state HTML summary used by /snapshot and startup."""
        from html import escape as _h

        from osi.core.market_phase import engine_weights_for_phase, get_market_phase

        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        today = now_ist.date().isoformat()
        phase = get_market_phase(now_ist)

        active_exec = []
        for sym in self.signal_manager.enabled_symbols:
            for sig in self.signal_manager._active_positions(sym):
                active_exec.append(sig)
        # Risk + execution P&L
        risk = self.signal_manager.risk_snapshot()
        exec_realized_today = float(risk.get("daily_realized_pnl") or 0.0)
        exec_open_mtm = 0.0
        for sig in active_exec:
            entry = float(getattr(sig, "entry_price", 0.0) or 0.0)
            ltp = float(getattr(sig, "current_ltp", None) or entry)
            qty = float(getattr(sig, "remaining_qty", 1.0) or 1.0)
            exec_open_mtm += (ltp - entry) * qty

        # Latency
        m = self.metrics.snapshot() if self.metrics is not None else {}
        avg_total = float(m.get("avg_total_signal_latency_ms") or 0.0)
        last_total = float(m.get("total_signal_latency_ms") or 0.0)
        engine_ms = m.get("engine_exec_ms") or {}

        # Engine weights for current phase
        weights = engine_weights_for_phase(phase)

        def _arrow(v: float) -> str:
            if v > 0:
                return "🟢"
            if v < 0:
                return "🔴"
            return "⚪"

        lines: List[str] = []
        lines.append("📡 <b>OSI SNAPSHOT</b>")
        lines.append(
            f"<i>{now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST · phase {_h(phase)}"
            f" · {'⏸ PAUSED' if self.execution_paused else '▶ RUNNING'}</i>"
        )
        lines.append("")

        # --- Active execution positions
        if active_exec:
            lines.append(f"📊 <b>Active Trades: {len(active_exec)}</b>")
            for sig in active_exec[:5]:
                entry = float(getattr(sig, "entry_price", 0.0) or 0.0)
                ltp = float(getattr(sig, "current_ltp", None) or entry)
                pts = round(ltp - entry, 2)
                pct = round((pts / entry * 100.0), 2) if entry else 0.0
                t1 = float(getattr(sig, "target_1", 0.0) or 0.0)
                t2 = float(getattr(sig, "target_2", 0.0) or 0.0)
                t3 = float(getattr(sig, "target_3", 0.0) or 0.0)
                sl = float(getattr(sig, "stop_loss", 0.0) or 0.0)
                lines.append(
                    f"• <b>{_h(sig.symbol)}</b> {_h(str(sig.signal))} · {_h(sig.strategy)}"
                )
                lines.append(
                    f"   Entry <code>{entry:.2f}</code> · "
                    f"LTP <code>{ltp:.2f}</code> {_arrow(pts)} "
                    f"{pct:+.2f}% ({pts:+.2f})"
                )
                t_parts = [f"SL <code>{sl:.2f}</code>"]
                if t1:
                    t_parts.append(f"T1 <code>{t1:.2f}</code>")
                if t2:
                    t_parts.append(f"T2 <code>{t2:.2f}</code>")
                if t3:
                    t_parts.append(f"T3 <code>{t3:.2f}</code>")
                lines.append("   " + " · ".join(t_parts))
            if len(active_exec) > 5:
                lines.append(f"   ... and <b>{len(active_exec) - 5}</b> more")
        else:
            lines.append("📊 <b>Active Trades:</b> none")
        lines.append("")

        # --- Today's P&L
        total_exec = exec_realized_today + exec_open_mtm
        lines.append("💰 <b>Today's P&amp;L</b>")
        lines.append(
            f"   Execution: realized <b>₹{exec_realized_today:+.2f}</b> · "
            f"open MTM <b>₹{exec_open_mtm:+.2f}</b> · "
            f"total {_arrow(total_exec)} <b>₹{total_exec:+.2f}</b>"
        )
        lines.append("")

        # --- Risk
        cap = float(risk.get("daily_loss_cap") or 0.0)
        max_sl = int(risk.get("max_consecutive_sl") or 0)
        max_notional = float(risk.get("max_trade_notional_rupees") or 0.0)
        lines.append("🛡 <b>Risk</b>")
        lines.append(
            f"   Daily loss cap: <b>₹{cap:,.0f}</b> · "
            f"used <b>₹{exec_realized_today:+.2f}</b>"
        )
        lines.append(
            f"   Consecutive SL: <b>{int(risk.get('consecutive_sl_count') or 0)}/{max_sl}</b>"
        )
        if max_notional > 0:
            lines.append(f"   Max notional/trade: <b>₹{max_notional:,.0f}</b>")
        lines.append(
            f"   Blocked: <b>{'YES' if bool(risk.get('risk_blocked')) else 'no'}</b>"
        )
        lines.append("")

        # --- Latency
        if avg_total or last_total:
            lines.append("⚡ <b>Latency</b>")
            lines.append(
                f"   last <code>{last_total:.1f}ms</code> · "
                f"avg <code>{avg_total:.1f}ms</code>"
            )
            if engine_ms:
                items = ", ".join(
                    f"{_h(str(k))} <code>{float(v):.1f}ms</code>"
                    for k, v in list(engine_ms.items())[:5]
                )
                lines.append(f"   engines: {items}")
            lines.append("")

        # --- Engine weights for current phase
        if weights:
            lines.append("🤖 <b>Engine Weights</b>")
            for eng, w in weights.items():
                lines.append(f"   {_h(eng)}: <b>{float(w):.2f}</b>")
        return "\n".join(lines)

