from __future__ import annotations

import logging
from json import JSONDecodeError
import json
from collections import deque
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Deque, Dict, List, Optional, Tuple
from uuid import uuid4

from osi.core.config import settings
from osi.core.models import ConsensusOutput, DashboardSignalCard, MarketTick, SignalRecord, SignalStatus
from osi.services.expiry_engine import ExpiryEngine
from osi.services.option_momentum import OptionMomentumService

if TYPE_CHECKING:
    from osi.services.metrics import MetricsService
    from osi.services.telegram_notifier import TelegramNotifier


class SignalManager:
    def __init__(self, metrics: "MetricsService | None" = None, notifier: "TelegramNotifier | None" = None) -> None:
        self.expiry_engine = ExpiryEngine()
        self.logger = logging.getLogger(__name__)
        self.metrics = metrics
        self.notifier = notifier
        self.enabled_symbols: Tuple[str, ...] = tuple(["NIFTY"] + (["SENSEX"] if bool(settings.enable_sensex) else []))
        self.active_signals: Dict[str, List[SignalRecord]] = {sym: [] for sym in self.enabled_symbols}
        self.history: List[SignalRecord] = []
        self.rejected_signals: List[Dict] = []
        self.paper_signals: List[Dict] = []
        self.max_history_records: int = 200
        self.last_generated_at: Dict[str, datetime] = {}
        self.last_signal_side: Dict[str, str] = {}
        self.last_signal_confidence: Dict[str, float] = {}
        self.cooldown_seconds = 120
        self.max_active_per_symbol = 1
        self.flip_flop_seconds = 150
        self.flip_flop_conf_gap = 12.0
        self.daily_realized_pnl: float = 0.0
        self.consecutive_sl_count: int = 0
        self._risk_day = datetime.now(timezone.utc).date().isoformat()
        self._ist = timezone(timedelta(hours=5, minutes=30))
        self.option_momentum = OptionMomentumService()
        self.storage_path = Path(__file__).resolve().parents[2] / "storage" / "signals.json"
        self._last_persist_at = datetime.now(timezone.utc)
        self._recovery_inactive_sec = 10.0
        self.premium_min = 20.0
        self.premium_max = 200.0
        self.no_move_exit_seconds = 240
        self.no_move_threshold_ratio = 0.03
        self.enable_runner_t3 = True
        self.execution_lots = max(1.0, float(getattr(settings, "execution_lots", 20.0)))
        self.nifty_lots = max(1.0, float(getattr(settings, "nifty_lots", self.execution_lots)))
        self.nifty_lot_size = max(1.0, float(getattr(settings, "nifty_lot_size", 65.0)))
        self.sensex_lots = max(1.0, float(getattr(settings, "sensex_lots", self.execution_lots)))
        self.sensex_lot_size = max(1.0, float(getattr(settings, "sensex_lot_size", 20.0)))
        self.last_decision_note: Dict[str, str] = {}
        self.last_confidence_breakdown: Dict[str, Dict[str, float]] = {}
        self._last_intrabar_fire_at: Dict[str, datetime] = {}
        self._market_range_hist: Dict[str, Deque[float]] = {"NIFTY": deque(maxlen=60), "SENSEX": deque(maxlen=60)}
        self.manual_pause: bool = False
        self._paper_last_option_ltp: Dict[str, float] = {}
        self._paper_positions: Dict[str, Dict] = {}
        self._load_storage()

    def process_consensus(self, tick: MarketTick, consensus: ConsensusOutput) -> Optional[SignalRecord]:
        if tick.symbol not in self.enabled_symbols:
            return None
        self._reset_daily_risk_if_needed()
        self._update_lifecycle(tick)
        if self.manual_pause:
            self._mark_rejected(tick, consensus, "manual_pause", breakdown=self._empty_breakdown(float(consensus.confidence)))
            return None
        if self._risk_blocked():
            self._mark_rejected(tick, consensus, "risk_blocked", breakdown=self._empty_breakdown(float(consensus.confidence)))
            return None
        if not self._is_trade_time_open():
            # User policy: do not record any signal/rejection rows outside market window.
            return None

        lead_engine = self._lead_engine(consensus)
        regime = str(tick.meta.get("regime") or "").upper()
        min_conf = max(30.0, float(settings.min_signal_confidence) - 8.0)
        strong_any = any(float(row.strength) > 0.7 for row in consensus.engine_outputs)
        directional_strength = max(
            [float(row.strength) for row in consensus.engine_outputs if row.signal == consensus.signal] or [0.0]
        )
        if not self._mean_reversion_regime_allowed(
            tick=tick,
            consensus=consensus,
            lead_engine=lead_engine,
            regime=regime,
            directional_strength=directional_strength,
        ):
            self._mark_rejected(
                tick,
                consensus,
                "mean_reversion_throttled_trending",
                notes=["regime=TRENDING", f"strength={directional_strength:.3f}"],
                breakdown=self._empty_breakdown(float(consensus.confidence)),
            )
            return None

        base_confidence = float(consensus.confidence)
        if consensus.signal == "NONE":
            fallback_signal, fallback_conf, fallback_note = self._aggressive_none_fallback(tick)
            if fallback_signal != "NONE":
                consensus.signal = fallback_signal
                consensus.confidence = fallback_conf
                base_confidence = float(fallback_conf)
                self.logger.info(
                    "AGGRESSIVE FALLBACK signal=%s confidence=%.2f note=%s symbol=%s",
                    fallback_signal,
                    float(fallback_conf),
                    fallback_note,
                    tick.symbol,
                )
            else:
                breakdown = self._build_breakdown(
                    base=base_confidence,
                    primary=0.0,
                    directional=0.0,
                    vwap=0.0,
                    regime=0.0,
                    momentum=0.0,
                    dead_zone=0.0,
                    final=base_confidence,
                )
                self._mark_rejected(tick, consensus, "none_signal", breakdown=breakdown)
                return None
        low_quality_base = 10.0 <= base_confidence < 20.0
        if base_confidence < 10.0:
            self._mark_rejected(
                tick,
                consensus,
                "low_base_confidence",
                breakdown=self._build_breakdown(
                    base=base_confidence,
                    primary=0.0,
                    directional=0.0,
                    vwap=0.0,
                    regime=0.0,
                    momentum=0.0,
                    dead_zone=0.0,
                    final=base_confidence,
                ),
            )
            return None

        primary_trigger = lead_engine in {"intrabar_engine", "smart_breakout_engine"}
        primary_boost = 10.0 if primary_trigger else 0.0
        directional_boost = min(12.0, directional_strength * 12.0)
        vwap_boost = 0.0
        regime_boost = 0.0
        momentum_adjustment = 0.0
        dead_zone_penalty = 0.0
        notes: List[str] = []
        if primary_trigger:
            notes.append(f"primary_trigger={lead_engine}")
        notes.append(f"directional_strength={directional_strength:.3f}")
        if low_quality_base:
            notes.append("LOW_QUALITY_BASE")

        vwap = float(tick.meta.get("vwap") or 0.0)
        if vwap > 0:
            aligned = (tick.index_price > vwap and consensus.signal == "BUY_CE") or (
                tick.index_price < vwap and consensus.signal == "BUY_PE"
            )
            if aligned:
                vwap_boost = 6.0
                notes.append("vwap_aligned")
            else:
                vwap_boost = -6.0
                notes.append("vwap_misaligned")
        if regime == "SIDEWAYS" and lead_engine == "mean_reversion_engine":
            regime_boost = 4.0
            notes.append("regime_sideways_meanrev_boost")
        if regime == "TRENDING" and lead_engine in {"trend_engine", "smart_breakout_engine", "intrabar_engine"}:
            regime_boost = 4.0
            notes.append("regime_trending_breakout_boost")

        # Confidence inflation control.
        total_boost = min(20.0, primary_boost + directional_boost + vwap_boost + regime_boost)

        active = self._active_positions(tick.symbol)
        bypass_cooldown = False
        if active:
            existing = active[0]
            if existing.signal == consensus.signal:
                self.logger.debug(
                    "SIGNAL BLOCKED symbol=%s reason=duplicate_active existing=%s",
                    tick.symbol,
                    existing.signal_id,
                )
                self._mark_rejected(tick, consensus, "duplicate_active", notes, self._empty_breakdown(base_confidence))
                return None
            # Opposite side detected: close existing if new confidence is materially stronger, else ignore.
            if float(consensus.confidence) >= float(existing.confidence) + 5.0:
                latest_price = self._current_option_ltp(tick, existing.option_symbol) or existing.entry_price
                self._close_signal(
                    existing,
                    status=SignalStatus.CLOSED,
                    exit_price=float(latest_price),
                    reason="OPPOSITE_SIGNAL_REPLACED",
                    sl_hit=False,
                )
                bypass_cooldown = True
                self.logger.info(
                    "SIGNAL REPLACED symbol=%s old=%s new=%s old_conf=%.2f new_conf=%.2f",
                    tick.symbol,
                    existing.signal,
                    consensus.signal,
                    float(existing.confidence),
                    float(consensus.confidence),
                )
            else:
                self.logger.debug(
                    "SIGNAL BLOCKED symbol=%s reason=opposite_ignored old_conf=%.2f new_conf=%.2f",
                    tick.symbol,
                    float(existing.confidence),
                    float(consensus.confidence),
                )
                self._mark_rejected(tick, consensus, "opposite_ignored", notes, self._empty_breakdown(base_confidence))
                return None

        if (not bypass_cooldown) and self._in_cooldown(tick.symbol):
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=cooldown", tick.symbol)
            self._mark_rejected(tick, consensus, "cooldown", notes, self._empty_breakdown(base_confidence))
            return None

        if len(self._active_positions(tick.symbol)) >= self.max_active_per_symbol:
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=max_active_limit", tick.symbol)
            self._mark_rejected(tick, consensus, "max_active_limit", notes, self._empty_breakdown(base_confidence))
            return None

        last_side = self.last_signal_side.get(tick.symbol)
        last_conf = self.last_signal_confidence.get(tick.symbol, 0.0)
        if (
            last_side
            and last_side != consensus.signal
            and self._recent_signal(tick.symbol, seconds=self.flip_flop_seconds)
            and abs(consensus.confidence - last_conf) < self.flip_flop_conf_gap
        ):
            self._mark_rejected(tick, consensus, "flip_flop_guard", notes, self._empty_breakdown(base_confidence))
            return None

        option_symbol, option_ltp, option_ts = self._select_option_contract(tick, consensus)
        if option_symbol is None or option_ltp is None:
            self._mark_rejected(tick, consensus, "option_contract_unavailable", notes, self._empty_breakdown(base_confidence))
            return None
        if not (self.premium_min <= float(option_ltp) <= self.premium_max):
            notes.append(f"premium_out_of_range:{round(float(option_ltp),2)}")
            self._mark_rejected(tick, consensus, "premium_filter_blocked", notes, self._empty_breakdown(base_confidence))
            return None
        now_ts = (tick.received_at if isinstance(tick.received_at, datetime) else datetime.now(timezone.utc)).timestamp()
        opt_ts = float(option_ts or 0.0)
        option_latency_ms = max(0.0, (now_ts - opt_ts) * 1000.0) if opt_ts > 0 else 1e9
        if self.metrics is not None:
            self.metrics.record_option_data_latency(option_latency_ms)
        stale_limit_ms = max(500.0, float(getattr(settings, "option_data_stale_sec", 1.0)) * 1000.0)
        if option_latency_ms > stale_limit_ms:
            self.logger.warning(
                "Signal rejected due to stale option data symbol=%s option=%s option_latency_ms=%.2f limit_ms=%.2f",
                tick.symbol,
                option_symbol,
                option_latency_ms,
                stale_limit_ms,
            )
            self._mark_rejected(tick, consensus, "stale_option_data", notes, self._empty_breakdown(base_confidence))
            return None
        opt_momentum = self.option_momentum.evaluate(
            tick=tick,
            option_symbol=option_symbol,
            option_ltp=float(option_ltp),
            signal=consensus.signal,
        )
        if not bool(opt_momentum.get("option_momentum_confirmed", False)):
            momentum_adjustment -= 5.0 if primary_trigger else 7.0
            notes.append("option_momentum_penalty")

        if lead_engine == "intrabar_engine":
            now = datetime.now(timezone.utc)
            last_fire = self._last_intrabar_fire_at.get(tick.symbol)
            if last_fire and (now - last_fire).total_seconds() < 30.0:
                momentum_adjustment -= 7.0
                notes.append("intrabar_refire_penalty")
            self._last_intrabar_fire_at[tick.symbol] = now

        dead_zone_penalty = self._dead_zone_penalty(tick)
        if dead_zone_penalty < 0:
            notes.append("dead_zone_penalty")

        final_confidence = base_confidence + total_boost + momentum_adjustment + dead_zone_penalty
        final_confidence = max(0.0, min(100.0, final_confidence))
        consensus.confidence = round(final_confidence, 2)

        breakdown = self._build_breakdown(
            base=base_confidence,
            primary=primary_boost,
            directional=directional_boost,
            vwap=vwap_boost,
            regime=regime_boost,
            momentum=momentum_adjustment,
            dead_zone=dead_zone_penalty,
            final=final_confidence,
        )
        self.last_confidence_breakdown[tick.symbol] = breakdown

        if float(consensus.confidence) < 25.0 and (not primary_trigger):
            self._mark_rejected(tick, consensus, "low_final_confidence_no_primary", notes, breakdown)
            return None
        if lead_engine == "zero_hero_engine" and float(consensus.confidence) < 80.0:
            self._mark_rejected(tick, consensus, "zero_hero_needs_high_confidence", notes, breakdown)
            return None
        if (not primary_trigger) and float(consensus.confidence) < 25.0 and directional_strength < 0.7:
            self._mark_rejected(tick, consensus, "no_primary_or_strong_base", notes, breakdown)
            return None
        # Momentum remains a soft penalty; no hard reject by momentum alone.

        t1 = round(float(option_ltp) * 1.20, 2)
        t2 = round(float(option_ltp) * 1.50, 2)
        t3 = round(float(option_ltp) * 1.80, 2) if self.enable_runner_t3 else None
        stop_reference_index = self._stop_reference_from_candle(tick, consensus.signal)
        strategy = self._strategy_name(lead_engine)
        sl_ratio = self._premium_stop_ratio(strategy)
        stop_loss = round(float(option_ltp) * (1.0 - sl_ratio), 2)
        entry_filter_ok, entry_filter_notes = self._run_entry_filter(
            tick=tick,
            signal=consensus.signal,
            option_ltp=float(option_ltp),
            stop_loss=float(stop_loss),
            target_price=float(t2),
            option_momentum_confirmed=bool(opt_momentum.get("option_momentum_confirmed", False)),
        )
        if not entry_filter_ok:
            notes.extend(entry_filter_notes)
            self._mark_rejected(tick, consensus, "entry_filter_failed", notes, breakdown)
            return None
        strength = max([float(row.strength) for row in consensus.engine_outputs if row.signal == consensus.signal] or [0.0])
        reason = next((str(row.reason or "") for row in consensus.engine_outputs if row.engine == lead_engine), "")
        expiry = self._expiry_from_option_symbol(option_symbol)
        signal_tag = self._signal_tag(lead_engine)
        now = datetime.now(timezone.utc)
        signal = SignalRecord(
            signal_id=f"OSI-{uuid4().hex[:10].upper()}",
            symbol=tick.symbol,
            signal=consensus.signal,
            engine_signal=consensus.signal,
            strategy=strategy,
            trigger_engine=lead_engine,
            confidence=consensus.confidence,
            strength=round(strength, 3),
            status=SignalStatus.CONFIRMED,
            signal_tag=signal_tag,
            quality_tag=self._quality_tag_from_confidence(float(consensus.confidence)),
            expiry=expiry,
            entry_price=round(option_ltp, 2),
            current_ltp=round(option_ltp, 2),
            stop_loss=stop_loss,
            target_1=t1,
            target_2=t2,
            target_3=t3,
            target_price=t2,
            option_symbol=option_symbol,
            strike=self._strike_from_option_symbol(option_symbol),
            reason=reason,
            exit_price=None,
            stop_reference_index=stop_reference_index,
            partial_booked=False,
            quantity=self._execution_quantity_for_symbol(tick.symbol),
            remaining_qty=self._execution_quantity_for_symbol(tick.symbol),
            realized_pnl=0.0,
            max_favorable=0.0,
            max_adverse=0.0,
            confidence_breakdown=breakdown,
            entry_time=now,
            last_ltp_update_at=now,
            created_at=now,
            lifecycle_events=[
                {
                    "event": "CREATED",
                    "timestamp": now.isoformat(),
                    "price": round(option_ltp, 2),
                    "t1": t1,
                    "t2": t2,
                    "option_momentum_strength": float(opt_momentum.get("momentum_strength") or 0.0),
                }
            ],
        )
        self.active_signals[tick.symbol].append(signal)
        self.last_generated_at[tick.symbol] = datetime.now(timezone.utc)
        self.last_signal_side[tick.symbol] = consensus.signal
        self.last_signal_confidence[tick.symbol] = consensus.confidence
        self._persist_storage_if_due(force=True)
        self.logger.debug(
            "SIGNAL ALLOWED symbol=%s signal=%s base=%.2f boosts=%.2f penalties=%.2f final=%.2f quality=%s breakdown=%s notes=%s",
            tick.symbol,
            consensus.signal,
            base_confidence,
            max(0.0, primary_boost + directional_boost + vwap_boost + regime_boost),
            abs(min(0.0, momentum_adjustment + dead_zone_penalty)),
            float(consensus.confidence),
            signal.quality_tag,
            breakdown,
            "|".join(notes),
        )
        self.logger.info(
            "SIGNAL CREATED id=%s symbol=%s type=%s strategy=%s entry=%.2f sl=%.2f target=%.2f confidence=%.2f engines=%s",
            signal.signal_id,
            signal.symbol,
            signal.signal,
            signal.strategy,
            signal.entry_price,
            signal.stop_loss,
            signal.target_price,
            signal.confidence,
            ",".join(row.engine for row in consensus.engine_outputs if row.signal != "NONE"),
        )
        if self.notifier is not None:
            try:
                self.notifier.notify_signal_created(signal, lead_engine=lead_engine)
            except Exception:
                self.logger.exception("Telegram notify create failed")
        self.last_decision_note[tick.symbol] = f"allowed:{'|'.join(notes)}"
        return signal

    def _update_lifecycle(self, tick: MarketTick) -> None:
        for signal in self._active_positions(tick.symbol):
            now = datetime.now(timezone.utc)
            recovery_inactive = self._is_recovery_inactive(signal, now=now)
            latest_price = self._current_option_ltp(tick, signal.option_symbol)
            if latest_price is None:
                if recovery_inactive:
                    signal.lifecycle_events.append(
                        {
                            "event": "RECOVERY_EXIT",
                            "timestamp": now.isoformat(),
                            "reason": "RECOVERY_EXIT_NO_LTP",
                        }
                    )
                    self._close_signal(
                        signal,
                        status=SignalStatus.CLOSED,
                        exit_price=float(signal.current_ltp or signal.entry_price),
                        reason="RECOVERY_EXIT_NO_LTP",
                        sl_hit=False,
                    )
                continue
            signal.current_ltp = round(latest_price, 2)
            signal.last_ltp_update_at = now
            signed = self._signed_move(signal, float(latest_price))
            signal.max_favorable = round(max(signal.max_favorable, signed), 2)
            signal.max_adverse = round(min(signal.max_adverse, signed), 2)
            self.logger.debug(
                "SIGNAL UPDATED id=%s ltp=%.2f mfe=%.2f mae=%.2f",
                signal.signal_id,
                float(latest_price),
                signal.max_favorable,
                signal.max_adverse,
            )

            t1 = float(signal.target_1 or 0.0)
            t2 = float(signal.target_2 or signal.target_price or 0.0)
            t3 = float(signal.target_3 or 0.0) if signal.target_3 is not None else None
            if (not signal.partial_booked) and latest_price >= t1:
                signal.partial_booked = True
                book_qty = min(signal.remaining_qty, signal.quantity * 0.5)
                signal.remaining_qty = max(0.0, signal.remaining_qty - book_qty)
                partial_pnl = (latest_price - signal.entry_price) * book_qty
                signal.realized_pnl = round(signal.realized_pnl + partial_pnl, 2)
                signal.lifecycle_events.append(
                    {
                        "event": "PARTIAL_BOOK_T1",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "price": round(latest_price, 2),
                        "booked_qty": round(book_qty, 3),
                    }
                )
                if self.notifier is not None:
                    try:
                        self.notifier.notify_signal_update(signal, event="PARTIAL_BOOK_T1", price=float(latest_price))
                    except Exception:
                        self.logger.exception("Telegram notify update failed")

            if latest_price >= t1 and signal.stop_loss < signal.entry_price:
                signal.stop_loss = signal.entry_price
                signal.lifecycle_events.append(
                    {
                        "event": "SL_MOVED_TO_ENTRY",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "price": round(latest_price, 2),
                    }
                )
                if self.notifier is not None:
                    try:
                        self.notifier.notify_signal_update(signal, event="SL_MOVED_TO_ENTRY", price=float(latest_price))
                    except Exception:
                        self.logger.exception("Telegram notify update failed")

            if latest_price >= t2:
                if signal.stop_loss < t1:
                    signal.stop_loss = round(t1, 2)
                    signal.lifecycle_events.append(
                        {
                            "event": "TRAIL_SL_TO_T1",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "price": round(latest_price, 2),
                        }
                    )
                if t3 is None or t3 <= 0:
                    signal.lifecycle_events.append(
                        {
                            "event": "TARGET_HIT",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "price": round(latest_price, 2),
                        }
                    )
                    self._close_signal(
                        signal,
                        status=SignalStatus.TARGET_HIT,
                        exit_price=float(latest_price),
                        reason="T2_REACHED",
                        sl_hit=False,
                    )
                elif latest_price >= t3:
                    signal.lifecycle_events.append(
                        {
                            "event": "TARGET_HIT",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "price": round(latest_price, 2),
                        }
                    )
                    self._close_signal(
                        signal,
                        status=SignalStatus.TARGET_HIT,
                        exit_price=float(latest_price),
                        reason="T3_REACHED",
                        sl_hit=False,
                    )
            elif latest_price <= signal.stop_loss:
                if recovery_inactive:
                    signal.lifecycle_events.append(
                        {
                            "event": "RECOVERY_EXIT",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "price": round(latest_price, 2),
                        }
                    )
                    self._close_signal(
                        signal,
                        status=SignalStatus.CLOSED,
                        exit_price=float(latest_price),
                        reason="RECOVERY_EXIT_STALE",
                        sl_hit=False,
                    )
                else:
                    signal.lifecycle_events.append(
                        {"event": "SL_HIT", "timestamp": datetime.now(timezone.utc).isoformat(), "price": round(latest_price, 2)}
                    )
                    self._close_signal(
                        signal,
                        status=SignalStatus.SL_HIT,
                        exit_price=float(latest_price),
                        reason="STOP_LOSS",
                        sl_hit=True,
                    )
            elif self._is_market_time_exit():
                signal.lifecycle_events.append(
                    {"event": "TIME_EXIT", "timestamp": datetime.now(timezone.utc).isoformat(), "price": round(latest_price, 2)}
                )
                self._close_signal(
                    signal,
                    status=SignalStatus.CLOSED,
                    exit_price=float(latest_price),
                    reason="TIME_EXIT",
                    sl_hit=False,
                )
            elif self._is_no_move_exit(signal, latest_price):
                signal.lifecycle_events.append(
                    {
                        "event": "TIME_EXIT",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "price": round(latest_price, 2),
                    }
                )
                self._close_signal(
                    signal,
                    status=SignalStatus.CLOSED,
                    exit_price=float(latest_price),
                    reason="TIME_EXIT_NO_MOVE",
                    sl_hit=False,
                )
        self._persist_storage_if_due()

    def _select_option_contract(self, tick: MarketTick, consensus: ConsensusOutput) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        direction = consensus.signal
        strikes = sorted(float(k) for k in tick.option_chain.keys()) if tick.option_chain else []
        if not strikes:
            return None, None, None
        atm = min(strikes, key=lambda s: abs(s - tick.index_price))
        lead_engine = self._lead_engine(consensus)
        strike_value = self._strategy_strike(atm, direction, lead_engine)
        strike = min(strikes, key=lambda s: abs(s - strike_value))
        leg = "CE" if direction == "BUY_CE" else "PE"
        row = tick.option_chain.get(str(int(strike))) or tick.option_chain.get(str(strike))
        if not row:
            return None, None, None
        ltp = row.get(leg, {}).get("ltp")
        if ltp is None:
            return None, None, None
        option_ts = row.get(leg, {}).get("option_timestamp")

        leg_expiry_raw = str((row.get(leg) or {}).get("expiry") or "").strip().upper()
        if tick.symbol == "SENSEX":
            expected_expiry = self.expiry_engine.nearest_expiry("SENSEX")
            if not leg_expiry_raw:
                return None, None, None
            try:
                leg_expiry_date = datetime.strptime(leg_expiry_raw, "%d%b%Y").date()
            except ValueError:
                return None, None, None
            if leg_expiry_date != expected_expiry:
                return None, None, None
        if leg_expiry_raw:
            try:
                expiry = datetime.strptime(leg_expiry_raw, "%d%b%Y").strftime("%Y%m%d")
            except ValueError:
                expiry = self.expiry_engine.nearest_expiry(tick.symbol).strftime("%Y%m%d")
        else:
            expiry = self.expiry_engine.nearest_expiry(tick.symbol).strftime("%Y%m%d")
        option_symbol = f"{tick.symbol}_{expiry}_{int(strike)}_{leg}"
        try:
            ts_float = float(option_ts) if option_ts is not None else None
        except (TypeError, ValueError):
            ts_float = None
        return option_symbol, float(ltp), ts_float

    @staticmethod
    def _current_option_ltp(tick: MarketTick, option_symbol: str) -> Optional[float]:
        parts = option_symbol.split("_")
        if len(parts) < 4:
            return None
        strike = parts[2]
        leg = parts[3]
        row = tick.option_chain.get(strike)
        if not row:
            return None
        price = row.get(leg, {}).get("ltp")
        return None if price is None else float(price)

    def _strategy_strike(self, atm: float, direction: str, lead_engine: str) -> float:
        if lead_engine == "intrabar_engine":
            offset = 0.0
        elif lead_engine == "zero_hero_engine":
            offset = 150.0
        elif lead_engine in {"trend_engine", "ml_engine"}:
            offset = 100.0
        elif lead_engine in {"smart_breakout_engine", "scalping_engine", "smc_engine"}:
            offset = 50.0
        else:
            offset = 50.0
        if direction == "BUY_CE":
            return atm + offset
        if direction == "BUY_PE":
            return atm - offset
        return atm

    @staticmethod
    def _strategy_name(lead_engine: str) -> str:
        if lead_engine == "smart_breakout_engine":
            return "smart_breakout"
        if lead_engine == "intrabar_engine":
            return "intrabar"
        if lead_engine == "mean_reversion_engine":
            return "mean_reversion"
        if lead_engine == "zero_hero_engine":
            return "zero_hero"
        return "scalping"

    @staticmethod
    def _signal_tag(lead_engine: str) -> str:
        if lead_engine == "intrabar_engine":
            return "INTRABAR"
        if lead_engine == "zero_hero_engine":
            return "ZERO_HERO"
        return "STANDARD"

    @staticmethod
    def _expiry_from_option_symbol(option_symbol: str) -> str:
        parts = option_symbol.split("_")
        return parts[1] if len(parts) > 1 else ""

    @staticmethod
    def _strike_from_option_symbol(option_symbol: str) -> Optional[float]:
        try:
            return float(option_symbol.split("_")[2])
        except (IndexError, TypeError, ValueError):
            return None

    @staticmethod
    def _lead_engine(consensus: ConsensusOutput) -> str:
        candidates = [row for row in consensus.engine_outputs if row.signal != "NONE"]
        if not candidates:
            return "scalping_engine"
        return max(candidates, key=lambda row: row.strength).engine

    def _active_positions(self, symbol: str) -> List[SignalRecord]:
        return [
            row
            for row in self.active_signals.get(symbol, [])
            if row.status in {SignalStatus.ACTIVE, SignalStatus.CONFIRMED}
        ]

    def _on_trade_closed(self, signal: SignalRecord, *, sl_hit: bool) -> None:
        self.daily_realized_pnl = round(self.daily_realized_pnl + float(signal.pnl or 0.0), 2)
        if self.notifier is not None:
            try:
                self.notifier.notify_signal_closed(signal)
            except Exception:
                self.logger.exception("Telegram notify close failed")
        if self.metrics is not None:
            self.metrics.record_trade_close(
                {
                    "signal_id": signal.signal_id,
                    "symbol": signal.symbol,
                    "signal": signal.signal,
                    "entry_price": signal.entry_price,
                    "exit_price": signal.exit_price,
                    "pnl": float(signal.pnl or 0.0),
                    "closed_at": (signal.closed_at.isoformat() if signal.closed_at else datetime.now(timezone.utc).isoformat()),
                }
            )
        if sl_hit:
            self.consecutive_sl_count += 1
        else:
            self.consecutive_sl_count = 0
        self.logger.info(
            "SIGNAL CLOSED id=%s status=%s exit=%.2f pnl=%.2f day_pnl=%.2f sl_streak=%s",
            signal.signal_id,
            signal.status.value,
            float(signal.exit_price or 0.0),
            float(signal.pnl or 0.0),
            self.daily_realized_pnl,
            self.consecutive_sl_count,
        )

    def _in_cooldown(self, symbol: str) -> bool:
        last = self.last_generated_at.get(symbol)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < self.cooldown_seconds

    def _recent_signal(self, symbol: str, seconds: int) -> bool:
        last = self.last_generated_at.get(symbol)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < seconds

    def _reset_daily_risk_if_needed(self) -> None:
        day_now = datetime.now(timezone.utc).date().isoformat()
        if day_now != self._risk_day:
            self._risk_day = day_now
            self.daily_realized_pnl = 0.0
            self.consecutive_sl_count = 0

    def _risk_blocked(self) -> bool:
        if self.daily_realized_pnl <= -abs(float(settings.daily_loss_cap)):
            return True
        if self.consecutive_sl_count >= int(settings.max_consecutive_sl):
            return True
        return False

    def _is_market_time_exit(self) -> bool:
        ts = datetime.now(self._ist).time()
        try:
            hh, mm = [int(x) for x in settings.market_exit_time_ist.split(":")]
            cutoff = time(hh, mm)
        except Exception:
            cutoff = time(15, 25)
        return ts >= cutoff

    def _is_trade_time_open(self) -> bool:
        ts = datetime.now(self._ist).time()
        return time(9, 15) <= ts <= time(15, 30)

    def _is_no_move_exit(self, signal: SignalRecord, latest_price: float) -> bool:
        if signal.entry_time is None:
            return False
        held_seconds = (datetime.now(timezone.utc) - signal.entry_time).total_seconds()
        if held_seconds < float(self.no_move_exit_seconds):
            return False
        movement = abs(float(latest_price) - float(signal.entry_price))
        threshold = max(1.0, float(signal.entry_price) * float(self.no_move_threshold_ratio))
        return movement < threshold

    @staticmethod
    def _premium_stop_ratio(strategy: str) -> float:
        if strategy == "intrabar":
            return 0.25
        if strategy == "zero_hero":
            return 0.45
        return 0.30

    @staticmethod
    def _target_multiplier_from_confidence(confidence: float) -> float:
        # Map confidence to 15%..25% target range.
        c = max(40.0, min(90.0, float(confidence)))
        ratio = (c - 40.0) / 50.0
        return 1.15 + (0.10 * ratio)

    @staticmethod
    def _stop_reference_from_candle(tick: MarketTick, direction: str) -> Optional[float]:
        candle = tick.meta.get("candle") or {}
        try:
            c_high = float(candle.get("high"))
            c_low = float(candle.get("low"))
        except (TypeError, ValueError):
            return None
        if direction == "BUY_CE":
            return c_low
        if direction == "BUY_PE":
            return c_high
        return None

    @staticmethod
    def _stop_from_index_reference(entry: float, index_price: float, stop_ref: float) -> float:
        if entry <= 0 or index_price <= 0:
            return entry * 0.9
        index_risk = abs(index_price - stop_ref) / index_price
        mapped_risk = max(0.05, min(0.20, index_risk * 1.2))
        return entry * (1.0 - mapped_risk)

    def _persist_storage(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_signals": self.snapshot_active_signals(),
            "history": self.snapshot_history(),
            "rejected_signals": list(self.rejected_signals[-self.max_history_records :]),
            "paper_signals": list(self.paper_signals[-self.max_history_records :]),
        }
        self.storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._last_persist_at = datetime.now(timezone.utc)

    def _persist_storage_if_due(self, *, force: bool = False) -> None:
        if force:
            self._persist_storage()
            return
        now = datetime.now(timezone.utc)
        if (now - self._last_persist_at).total_seconds() >= 1.0:
            self._persist_storage()

    def _load_storage(self) -> None:
        try:
            if not self.storage_path.exists():
                self._persist_storage_if_due(force=True)
                return
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, JSONDecodeError, ValueError):
            self.logger.exception("Failed to load signals storage, starting clean")
            raw = {"active_signals": [], "history": []}
        self.active_signals = {sym: [] for sym in self.enabled_symbols}
        self.history = []
        self.rejected_signals = []
        self.paper_signals = []
        self._paper_positions = {}
        for row in raw.get("active_signals", []):
            try:
                if "signal" not in row and "engine_signal" in row:
                    row["signal"] = row.get("engine_signal")
                if "entry_time" not in row and "created_at" in row:
                    row["entry_time"] = row.get("created_at")
                if "exit_time" not in row and "closed_at" in row:
                    row["exit_time"] = row.get("closed_at")
                sig = SignalRecord.model_validate(row)
            except Exception:
                continue
            if sig.status in {SignalStatus.ACTIVE, SignalStatus.CONFIRMED}:
                now = datetime.now(timezone.utc)
                sig.trade_state = "RECOVERED"
                if sig.recovered_at is None:
                    sig.recovered_at = now
                self.active_signals.setdefault(sig.symbol, []).append(sig)
        for row in raw.get("history", []):
            try:
                if "signal" not in row and "engine_signal" in row:
                    row["signal"] = row.get("engine_signal")
                if "entry_time" not in row and "created_at" in row:
                    row["entry_time"] = row.get("created_at")
                if "exit_time" not in row and "closed_at" in row:
                    row["exit_time"] = row.get("closed_at")
                sig = SignalRecord.model_validate(row)
            except Exception:
                continue
            if sig.symbol in self.enabled_symbols and sig.status not in {SignalStatus.ACTIVE, SignalStatus.CONFIRMED}:
                self.history.append(sig)
        self.history = self.history[-self.max_history_records :]
        for row in raw.get("rejected_signals", []):
            if isinstance(row, dict) and str(row.get("symbol") or "") in self.enabled_symbols:
                self.rejected_signals.append(row)
        self.rejected_signals = self.rejected_signals[-self.max_history_records :]
        for row in raw.get("paper_signals", []):
            if isinstance(row, dict) and str(row.get("symbol") or "") in self.enabled_symbols:
                self.paper_signals.append(row)
                key = f"{row.get('symbol')}|{row.get('engine')}"
                status = str(row.get("status") or "").upper()
                if status == "PAPER_OPEN":
                    self._paper_positions[key] = dict(row)
                elif status == "PAPER_CLOSED" and key in self._paper_positions:
                    self._paper_positions.pop(key, None)
        self.paper_signals = self.paper_signals[-self.max_history_records :]

    def snapshot_active_signals(self) -> List[Dict]:
        rows: List[Dict] = []
        for symbol in self.enabled_symbols:
            for sig in self._active_positions(symbol):
                rows.append(sig.model_dump(mode="json"))
        return rows

    def snapshot_history(self) -> List[Dict]:
        return [
            row.model_dump(mode="json")
            for row in self.history[-self.max_history_records :]
            if row.symbol in self.enabled_symbols
        ]

    def snapshot_rejected(self) -> List[Dict]:
        return list(self.rejected_signals[-self.max_history_records :])

    def snapshot_paper_signals(self) -> List[Dict]:
        return list(self.paper_signals[-self.max_history_records :])

    def active_paper_positions(self) -> List[Dict]:
        rows = [dict(v) for v in self._paper_positions.values()]
        rows.sort(key=lambda r: str(r.get("timestamp") or ""), reverse=True)
        return rows

    def paper_engine_stats(self) -> Dict[str, Dict[str, float]]:
        tracked = {
            "intrabar": "intrabar_engine",
            "smart_breakout": "smart_breakout_engine",
            "mean_reversion": "mean_reversion_engine",
            "zero_hero": "zero_hero_engine",
        }
        totals: Dict[str, int] = {k: 0 for k in tracked}
        for row in self.paper_signals:
            eng = str(row.get("engine") or "")
            for key, engine_name in tracked.items():
                if eng == engine_name:
                    totals[key] += 1

        closed_by_engine: Dict[str, List[SignalRecord]] = {k: [] for k in tracked}
        for row in self.history:
            engine_key = self._engine_bucket_from_signal(row)
            if engine_key in closed_by_engine and row.closed_at is not None:
                closed_by_engine[engine_key].append(row)

        stats: Dict[str, Dict[str, float]] = {}
        for key in tracked:
            closed = sorted(
                closed_by_engine.get(key, []),
                key=lambda r: (r.closed_at or r.exit_time or r.created_at or datetime.now(timezone.utc)),
            )
            pnls = [float(r.pnl or 0.0) for r in closed]
            wins = sum(1 for p in pnls if p > 0)
            losses = sum(1 for p in pnls if p < 0)
            decided = wins + losses
            win_rate = (wins / decided * 100.0) if decided > 0 else 0.0
            avg_pnl = (sum(pnls) / len(pnls)) if pnls else 0.0
            total_pnl = sum(pnls) if pnls else 0.0
            max_drawdown = self._max_drawdown(pnls)
            last_5_results = [("✔" if p > 0 else "❌" if p < 0 else "•") for p in pnls[-5:]]
            stats[key] = {
                "total_signals": float(totals.get(key, 0)),
                "wins": float(wins),
                "losses": float(losses),
                "win_rate": round(win_rate, 2),
                "avg_pnl": round(avg_pnl, 2),
                "total_pnl": round(total_pnl, 2),
                "max_drawdown": round(max_drawdown, 2),
                "last_5_results": last_5_results,
            }
        return stats

    @staticmethod
    def _max_drawdown(pnls: List[float]) -> float:
        if not pnls:
            return 0.0
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            equity += float(p)
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @staticmethod
    def _engine_bucket_from_signal(sig: SignalRecord) -> str:
        tag = str(getattr(sig, "signal_tag", "") or "")
        strategy = str(getattr(sig, "strategy", "") or "")
        if tag == "INTRABAR" or strategy == "intrabar":
            return "intrabar"
        if strategy == "smart_breakout":
            return "smart_breakout"
        if strategy == "mean_reversion":
            return "mean_reversion"
        if tag == "ZERO_HERO" or strategy == "zero_hero":
            return "zero_hero"
        return ""

    def record_engine_signals(self, tick: MarketTick, engine_outputs: List, *, source: str) -> None:
        if not self._is_trade_time_open():
            return
        now = datetime.now(timezone.utc).isoformat()
        for row in engine_outputs:
            if getattr(row, "signal", "NONE") == "NONE":
                continue
            engine_name = str(getattr(row, "engine", "") or "")
            if engine_name == "mean_reversion_engine":
                # User preference: keep strategy signal visibility, but skip paper trade simulation for mean reversion.
                continue
            signal = str(getattr(row, "signal", "NONE") or "NONE")
            strategy = self._strategy_name(engine_name)
            option_symbol, option_ltp = self._paper_option_contract_preview(
                tick=tick,
                signal=signal,
                lead_engine=engine_name,
            )
            strike = self._strike_from_option_symbol(option_symbol) if option_symbol else None
            expiry = self._expiry_from_option_symbol(option_symbol) if option_symbol else ""
            qty = self._execution_quantity_for_symbol(tick.symbol)
            current_ltp: Optional[float] = None
            paper_target: Optional[float] = None
            paper_exit: Optional[float] = None
            if option_symbol is not None and option_ltp is not None:
                current_ltp = round(float(option_ltp), 2)
                paper_target = round(float(option_ltp) * 1.20, 2)
                paper_exit = round(float(option_ltp), 2)
            if option_symbol is None or option_ltp is None:
                continue
            position_key = f"{tick.symbol}|{engine_name}"
            existing = self._paper_positions.get(position_key)
            if existing and str(existing.get("signal") or "") == signal:
                existing["current_ltp"] = current_ltp
                existing["timestamp"] = now
                existing["paper_exit"] = paper_exit
                move = (
                    float(current_ltp) - float(existing.get("entry_price") or 0.0)
                    if signal == "BUY_CE"
                    else float(existing.get("entry_price") or 0.0) - float(current_ltp)
                )
                existing["simulated_pnl"] = round(move * float(existing.get("qty") or qty), 2)
                existing["status"] = "PAPER_OPEN"
                self.paper_signals.append(dict(existing))
                continue
            if existing:
                exit_ltp = float(current_ltp)
                prev_side = str(existing.get("signal") or "")
                move = (
                    exit_ltp - float(existing.get("entry_price") or 0.0)
                    if prev_side == "BUY_CE"
                    else float(existing.get("entry_price") or 0.0) - exit_ltp
                )
                closed = dict(existing)
                closed["timestamp"] = now
                closed["paper_exit"] = round(exit_ltp, 2)
                closed["current_ltp"] = round(exit_ltp, 2)
                closed["simulated_pnl"] = round(move * float(existing.get("qty") or qty), 2)
                closed["status"] = "PAPER_CLOSED"
                self.paper_signals.append(closed)
            opened = {
                "paper_id": f"PAPER-{uuid4().hex[:10].upper()}",
                "timestamp": now,
                "symbol": tick.symbol,
                "engine": engine_name,
                "strategy": strategy,
                "signal": signal,
                "strength": float(getattr(row, "strength", 0.0) or 0.0),
                "confidence": float(getattr(row, "confidence", 0.0) or 0.0),
                "reason": str(getattr(row, "reason", "") or ""),
                "source": source,
                "mode": "PAPER",
                "index_price": float(tick.index_price),
                "strike": strike,
                "option_symbol": option_symbol,
                "expiry": expiry,
                "option_ltp": round(float(option_ltp), 2),
                "qty": float(qty),
                "status": "PAPER_OPEN",
                "entry_price": round(float(option_ltp), 2),
                "current_ltp": current_ltp,
                "simulated_pnl": 0.0,
                "paper_target": paper_target,
                "paper_exit": paper_exit,
            }
            self._paper_positions[position_key] = opened
            self.paper_signals.append(dict(opened))
        self.paper_signals = self.paper_signals[-1000:]
        self._persist_storage_if_due()

    def _paper_option_contract_preview(
        self,
        *,
        tick: MarketTick,
        signal: str,
        lead_engine: str,
    ) -> tuple[Optional[str], Optional[float]]:
        if signal not in {"BUY_CE", "BUY_PE"}:
            return None, None
        strikes = sorted(float(k) for k in tick.option_chain.keys()) if tick.option_chain else []
        if not strikes:
            return None, None
        atm = min(strikes, key=lambda s: abs(s - float(tick.index_price)))
        strike_value = self._strategy_strike(atm, signal, lead_engine)
        strike = min(strikes, key=lambda s: abs(s - strike_value))
        leg = "CE" if signal == "BUY_CE" else "PE"
        row = tick.option_chain.get(str(int(strike))) or tick.option_chain.get(str(strike))
        if not row:
            return None, None
        ltp = row.get(leg, {}).get("ltp")
        if ltp is None:
            return None, None
        leg_expiry_raw = str((row.get(leg) or {}).get("expiry") or "").strip().upper()
        if leg_expiry_raw:
            try:
                expiry = datetime.strptime(leg_expiry_raw, "%d%b%Y").strftime("%Y%m%d")
            except ValueError:
                expiry = self.expiry_engine.nearest_expiry(tick.symbol).strftime("%Y%m%d")
        else:
            expiry = self.expiry_engine.nearest_expiry(tick.symbol).strftime("%Y%m%d")
        option_symbol = f"{tick.symbol}_{expiry}_{int(strike)}_{leg}"
        return option_symbol, float(ltp)

    def _execution_quantity_for_symbol(self, symbol: str) -> float:
        sym = str(symbol or "").upper()
        if sym == "NIFTY":
            return float(self.nifty_lots * self.nifty_lot_size)
        if sym == "SENSEX":
            return float(self.sensex_lots * self.sensex_lot_size)
        return float(self.execution_lots)

    def _close_signal(self, signal: SignalRecord, *, status: SignalStatus, exit_price: float, reason: str, sl_hit: bool) -> None:
        signal.status = status
        now = datetime.now(timezone.utc)
        signal.closed_at = now
        signal.exit_time = now
        signal.exit_price = round(float(exit_price), 2)
        signed_move = self._signed_move(signal, float(exit_price))
        signal.pnl = round(signal.realized_pnl + (signed_move * max(signal.remaining_qty, 0.0)), 2)
        signal.remaining_qty = 0.0
        signal.lifecycle_events.append({"event": status.value, "timestamp": now.isoformat(), "reason": reason})
        if reason:
            # Preserve close context for history consumers.
            signal.reason = str(reason)
        if signal in self.active_signals.get(signal.symbol, []):
            self.active_signals[signal.symbol] = [x for x in self.active_signals.get(signal.symbol, []) if x.signal_id != signal.signal_id]
        self.history.append(signal)
        self.history = self.history[-self.max_history_records :]
        self._on_trade_closed(signal, sl_hit=sl_hit)
        self._persist_storage_if_due(force=True)

    @staticmethod
    def _signed_move(signal: SignalRecord, current_price: float) -> float:
        if signal.signal == "BUY_PE":
            return signal.entry_price - current_price
        return current_price - signal.entry_price

    def _mark_rejected(
        self,
        tick: MarketTick,
        consensus: ConsensusOutput,
        reason: str,
        notes: Optional[List[str]] = None,
        breakdown: Optional[Dict[str, float]] = None,
    ) -> None:
        if tick.symbol not in self.enabled_symbols:
            return
        note_rows = notes or []
        conf_breakdown = breakdown or self._empty_breakdown(float(consensus.confidence))
        self.last_confidence_breakdown[tick.symbol] = conf_breakdown
        payload = {
            "timestamp": tick.timestamp.isoformat(),
            "symbol": tick.symbol,
            "signal": consensus.signal,
            "confidence": float(consensus.confidence),
            "reason": reason,
            "notes": note_rows,
            "confidence_breakdown": conf_breakdown,
        }
        self.rejected_signals.append(payload)
        self.rejected_signals = self.rejected_signals[-self.max_history_records :]
        self.last_decision_note[tick.symbol] = f"blocked:{reason}:{'|'.join(note_rows)}"
        self.logger.debug(
            "SIGNAL REJECTED symbol=%s signal=%s base=%.2f penalties=%.2f final=%.2f reason=%s notes=%s breakdown=%s",
            tick.symbol,
            consensus.signal,
            float(conf_breakdown.get("base", 0.0)),
            abs(min(0.0, float(conf_breakdown.get("momentum", 0.0)) + float(conf_breakdown.get("dead_zone", 0.0)))),
            float(conf_breakdown.get("final", float(consensus.confidence))),
            reason,
            "|".join(note_rows),
            conf_breakdown,
        )
        self.logger.debug(
            "SIGNAL DECISION symbol=%s signal=%s confidence=%.2f breakdown=%s reason=rejected notes=%s",
            tick.symbol,
            consensus.signal,
            float(consensus.confidence),
            conf_breakdown,
            "|".join(note_rows),
        )
        self._persist_storage_if_due()

    def _is_recovery_inactive(self, signal: SignalRecord, *, now: datetime) -> bool:
        if signal.trade_state != "RECOVERED":
            return False
        reference = signal.last_ltp_update_at or signal.recovered_at
        if reference is None:
            return False
        return (now - reference).total_seconds() > self._recovery_inactive_sec

    def latest_decision_note(self, symbol: str) -> str:
        return self.last_decision_note.get(symbol, "")

    def latest_confidence_breakdown(self, symbol: str) -> Dict[str, float]:
        return dict(self.last_confidence_breakdown.get(symbol, {}))

    def set_manual_pause(self, paused: bool) -> bool:
        changed = self.manual_pause != bool(paused)
        self.manual_pause = bool(paused)
        return changed

    def is_manual_paused(self) -> bool:
        return bool(self.manual_pause)

    @staticmethod
    def _build_breakdown(
        *,
        base: float,
        primary: float,
        directional: float,
        vwap: float,
        regime: float,
        momentum: float,
        dead_zone: float,
        final: float,
    ) -> Dict[str, float]:
        return {
            "base": round(base, 2),
            "primary": round(primary, 2),
            "directional": round(directional, 2),
            "vwap": round(vwap, 2),
            "regime": round(regime, 2),
            "momentum": round(momentum, 2),
            "dead_zone": round(dead_zone, 2),
            "final": round(final, 2),
        }

    @staticmethod
    def _empty_breakdown(base_conf: float) -> Dict[str, float]:
        return {
            "base": round(base_conf, 2),
            "primary": 0.0,
            "directional": 0.0,
            "vwap": 0.0,
            "regime": 0.0,
            "momentum": 0.0,
            "dead_zone": 0.0,
            "final": round(base_conf, 2),
        }

    def _dead_zone_penalty(self, tick: MarketTick) -> float:
        candle = tick.meta.get("candle") or {}
        try:
            c_range = float(candle.get("high")) - float(candle.get("low"))
        except (TypeError, ValueError):
            return 0.0
        hist = self._market_range_hist.setdefault(
            tick.symbol, deque(maxlen=max(5, int(settings.market_activity_lookback_candles)))
        )
        hist.append(max(0.0, c_range))
        if not hist:
            return 0.0
        avg_range = sum(hist) / len(hist)
        if avg_range < float(settings.min_market_activity_threshold):
            return -3.0
        return 0.0

    @staticmethod
    def _quality_tag_from_confidence(final_confidence: float) -> str:
        c = float(final_confidence)
        if c >= 50.0:
            return "STRONG"
        if c >= 35.0:
            return "NORMAL"
        return "LOW"

    def _aggressive_none_fallback(self, tick: MarketTick) -> tuple[str, float, str]:
        mode = str(getattr(settings, "scalping_entry_mode", "confirmed") or "confirmed").strip().lower()
        if mode != "aggressive":
            return "NONE", 0.0, "mode_not_aggressive"
        candle = tick.meta.get("candle") or {}
        try:
            c_open = float(candle.get("open"))
            c_close = float(candle.get("close"))
            c_high = float(candle.get("high"))
            c_low = float(candle.get("low"))
        except (TypeError, ValueError):
            return "NONE", 0.0, "candle_missing"
        c_range = max(0.0, c_high - c_low)
        if c_range < max(2.0, float(getattr(settings, "min_market_activity_threshold", 8.0)) * 0.25):
            return "NONE", 0.0, "range_too_small"
        vwap = float(tick.meta.get("vwap") or 0.0)
        if c_close > c_open and (vwap <= 0.0 or float(tick.index_price) >= vwap):
            return "BUY_CE", 14.0, "bull_candle_pressure"
        if c_close < c_open and (vwap <= 0.0 or float(tick.index_price) <= vwap):
            return "BUY_PE", 14.0, "bear_candle_pressure"
        return "NONE", 0.0, "no_directional_pressure"

    @staticmethod
    def _mean_reversion_regime_allowed(
        *,
        tick: MarketTick,
        consensus: ConsensusOutput,
        lead_engine: str,
        regime: str,
        directional_strength: float,
    ) -> bool:
        # Throttle mean reversion in trending tape, but keep a narrow high-conviction path.
        if lead_engine != "mean_reversion_engine":
            return True
        if regime != "TRENDING":
            return True
        if directional_strength < 0.78:
            return False
        vwap = float(tick.meta.get("vwap") or 0.0)
        if vwap <= 0:
            return False
        index_price = float(tick.index_price or 0.0)
        if index_price <= 0:
            return False
        # Require meaningful extension from VWAP before fading trend.
        extension_ratio = abs(index_price - vwap) / index_price
        if extension_ratio < 0.0015:  # 0.15%
            return False
        direction = str(consensus.signal or "")
        # BUY_CE should only trigger if price is stretched below VWAP; BUY_PE vice versa.
        if direction == "BUY_CE" and index_price >= vwap:
            return False
        if direction == "BUY_PE" and index_price <= vwap:
            return False
        return True

    def active_cards(self) -> List[DashboardSignalCard]:
        cards: List[DashboardSignalCard] = []
        for symbol in self.enabled_symbols:
            for sig in self.active_signals.get(symbol, []):
                if sig.status not in {SignalStatus.ACTIVE, SignalStatus.CONFIRMED}:
                    continue
                cards.append(
                    DashboardSignalCard(
                        signal_id=sig.signal_id,
                        symbol=sig.symbol,
                        signal=sig.signal,
                        confidence=sig.confidence,
                        status=sig.status,
                        entry_price=sig.entry_price,
                        stop_loss=sig.stop_loss,
                        target_price=sig.target_price,
                        option_symbol=sig.option_symbol,
                        strike=sig.strike,
                        current_ltp=sig.current_ltp,
                        exit_price=sig.exit_price,
                        stop_reference_index=sig.stop_reference_index,
                        partial_booked=sig.partial_booked,
                        remaining_qty=sig.remaining_qty,
                        realized_pnl=sig.realized_pnl,
                        timestamp=sig.entry_time,
                    )
                )
        return cards

    def _run_entry_filter(
        self,
        *,
        tick: MarketTick,
        signal: str,
        option_ltp: float,
        stop_loss: float,
        target_price: float,
        option_momentum_confirmed: bool,
    ) -> tuple[bool, list[str]]:
        notes: list[str] = []
        checks = {
            "price_confirmation": self._price_confirmation(tick, signal),
            "option_momentum": option_momentum_confirmed,
            "range_expansion": self._range_expansion(tick),
            "vwap_alignment": self._vwap_alignment(tick, signal),
            "rr_validation": self._rr_validation(option_ltp, stop_loss, target_price),
        }
        for key, ok in checks.items():
            if not ok:
                notes.append(f"entry_filter_fail:{key}")
        mode = str(getattr(settings, "scalping_entry_mode", "confirmed") or "confirmed").strip().lower()
        pass_count = sum(1 for ok in checks.values() if ok)
        # Aggressive mode allows momentum breakouts with partial confirmation.
        if mode == "aggressive":
            return pass_count >= 3, notes
        return all(checks.values()), notes

    @staticmethod
    def _rr_validation(entry: float, stop_loss: float, target_price: float) -> bool:
        risk = max(0.01, entry - stop_loss)
        reward = max(0.0, target_price - entry)
        return (reward / risk) >= 1.2

    @staticmethod
    def _vwap_alignment(tick: MarketTick, signal: str) -> bool:
        vwap = float(tick.meta.get("vwap") or 0.0)
        if vwap <= 0:
            return False
        if signal == "BUY_CE":
            return float(tick.index_price) > vwap
        if signal == "BUY_PE":
            return float(tick.index_price) < vwap
        return False

    @staticmethod
    def _range_expansion(tick: MarketTick) -> bool:
        candle = tick.meta.get("candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        try:
            cur_range = float(candle.get("high")) - float(candle.get("low"))
            prev_range = float(prev.get("high")) - float(prev.get("low"))
        except (TypeError, ValueError):
            return False
        if cur_range <= 0:
            return False
        return cur_range >= max(1.0, prev_range * 1.05)

    @staticmethod
    def _price_confirmation(tick: MarketTick, signal: str) -> bool:
        candle = tick.meta.get("candle") or {}
        prev = tick.meta.get("prev_candle") or {}
        try:
            px = float(tick.index_price)
            prev_high = float(prev.get("high"))
            prev_low = float(prev.get("low"))
            c_open = float(candle.get("open"))
            c_close = float(candle.get("close"))
        except (TypeError, ValueError):
            return False
        if signal == "BUY_CE":
            return px >= prev_high and c_close >= c_open
        if signal == "BUY_PE":
            return px <= prev_low and c_close <= c_open
        return False

