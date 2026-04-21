from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from osi.core.config import settings
from osi.core.models import ConsensusOutput, DashboardSignalCard, MarketTick, SignalRecord, SignalStatus
from osi.services.expiry_engine import ExpiryEngine


class SignalManager:
    def __init__(self) -> None:
        self.expiry_engine = ExpiryEngine()
        self.logger = logging.getLogger(__name__)
        self.active_signals: Dict[str, List[SignalRecord]] = {"NIFTY": [], "SENSEX": []}
        self.history: List[SignalRecord] = []
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

    def process_consensus(self, tick: MarketTick, consensus: ConsensusOutput) -> Optional[SignalRecord]:
        self._reset_daily_risk_if_needed()
        self._update_lifecycle(tick)
        if self._risk_blocked():
            return None
        if not self._is_trade_time_open():
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=before_0920_ist", tick.symbol)
            return None

        lead_engine = self._lead_engine(consensus)
        regime = str(tick.meta.get("regime") or "").upper()
        min_conf = max(40.0, float(settings.min_signal_confidence))
        strong_any = any(float(row.strength) > 0.7 for row in consensus.engine_outputs)
        directional_strength = max(
            [float(row.strength) for row in consensus.engine_outputs if row.signal == consensus.signal] or [0.0]
        )
        if directional_strength < 0.6:
            self.logger.debug(
                "SIGNAL BLOCKED symbol=%s reason=low_directional_strength strength=%.3f",
                tick.symbol,
                directional_strength,
            )
            return None

        vwap = float(tick.meta.get("vwap") or 0.0)
        if vwap > 0:
            if tick.index_price > vwap and consensus.signal == "BUY_PE":
                self.logger.debug(
                    "SIGNAL BLOCKED symbol=%s reason=vwap_filter_above_vwap_only_ce index=%.2f vwap=%.2f",
                    tick.symbol,
                    float(tick.index_price),
                    vwap,
                )
                return None
            if tick.index_price < vwap and consensus.signal == "BUY_CE":
                self.logger.debug(
                    "SIGNAL BLOCKED symbol=%s reason=vwap_filter_below_vwap_only_pe index=%.2f vwap=%.2f",
                    tick.symbol,
                    float(tick.index_price),
                    vwap,
                )
                return None

        if (consensus.confidence < min_conf and not strong_any) or consensus.signal == "NONE":
            self.logger.debug(
                "SIGNAL BLOCKED symbol=%s conf=%.2f min_conf=%.2f strong_any=%s regime=%s lead=%s",
                tick.symbol,
                float(consensus.confidence),
                min_conf,
                strong_any,
                regime or "-",
                lead_engine,
            )
            return None

        active = self._active_positions(tick.symbol)
        bypass_cooldown = False
        if active:
            existing = active[0]
            if existing.engine_signal == consensus.signal:
                self.logger.debug(
                    "SIGNAL BLOCKED symbol=%s reason=duplicate_active existing=%s",
                    tick.symbol,
                    existing.signal_id,
                )
                return None
            # Opposite side detected: close existing if new confidence is materially stronger, else ignore.
            if float(consensus.confidence) >= float(existing.confidence) + 5.0:
                latest_price = self._current_option_ltp(tick, existing.option_symbol) or existing.entry_price
                existing.status = SignalStatus.CLOSED
                existing.closed_at = datetime.now(timezone.utc)
                existing.exit_price = round(float(latest_price), 2)
                final_leg_pnl = (float(latest_price) - existing.entry_price) * max(existing.remaining_qty, 0.0)
                existing.pnl = round(existing.realized_pnl + final_leg_pnl, 2)
                existing.remaining_qty = 0.0
                existing.lifecycle_events.append(
                    {
                        "event": "CLOSED",
                        "timestamp": existing.closed_at.isoformat(),
                        "reason": "OPPOSITE_SIGNAL_REPLACED",
                    }
                )
                self._on_trade_closed(existing, sl_hit=False)
                bypass_cooldown = True
                self.logger.info(
                    "SIGNAL REPLACED symbol=%s old=%s new=%s old_conf=%.2f new_conf=%.2f",
                    tick.symbol,
                    existing.engine_signal,
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
                return None

        if (not bypass_cooldown) and self._in_cooldown(tick.symbol):
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=cooldown", tick.symbol)
            return None

        if len(self._active_positions(tick.symbol)) >= self.max_active_per_symbol:
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=max_active_limit", tick.symbol)
            return None

        last_side = self.last_signal_side.get(tick.symbol)
        last_conf = self.last_signal_confidence.get(tick.symbol, 0.0)
        if (
            last_side
            and last_side != consensus.signal
            and self._recent_signal(tick.symbol, seconds=self.flip_flop_seconds)
            and abs(consensus.confidence - last_conf) < self.flip_flop_conf_gap
        ):
            return None

        option_symbol, option_ltp = self._select_option_contract(tick, consensus)
        if option_symbol is None or option_ltp is None:
            return None

        target_mult = self._target_multiplier_from_confidence(float(consensus.confidence))
        t2 = round(option_ltp * target_mult, 2)
        t1 = round(option_ltp + (t2 - option_ltp) * 0.5, 2)
        stop_reference_index = self._stop_reference_from_candle(tick, consensus.signal)
        if stop_reference_index is not None:
            sl_from_ref = self._stop_from_index_reference(
                entry=float(option_ltp),
                index_price=float(tick.index_price),
                stop_ref=float(stop_reference_index),
            )
            stop_loss = round(sl_from_ref, 2)
        else:
            stop_loss = round(option_ltp * 0.9, 2)
        signal = SignalRecord(
            signal_id=f"OSI-{uuid4().hex[:10].upper()}",
            symbol=tick.symbol,
            engine_signal=consensus.signal,
            confidence=consensus.confidence,
            entry_price=round(option_ltp, 2),
            stop_loss=stop_loss,
            target_price=t2,
            option_symbol=option_symbol,
            strike=self._strike_from_option_symbol(option_symbol),
            current_ltp=round(option_ltp, 2),
            exit_price=None,
            stop_reference_index=stop_reference_index,
            partial_booked=False,
            quantity=1.0,
            remaining_qty=1.0,
            realized_pnl=0.0,
            lifecycle_events=[
                {
                    "event": "CREATED",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "price": round(option_ltp, 2),
                    "t1": t1,
                    "t2": t2,
                }
            ],
        )
        self.active_signals[tick.symbol].append(signal)
        self.history.append(signal)
        self.last_generated_at[tick.symbol] = datetime.now(timezone.utc)
        self.last_signal_side[tick.symbol] = consensus.signal
        self.last_signal_confidence[tick.symbol] = consensus.confidence
        self.logger.debug(
            "SIGNAL ALLOWED symbol=%s conf=%.2f min_conf=%.2f strong_any=%s regime=%s lead=%s target_mult=%.3f",
            tick.symbol,
            float(consensus.confidence),
            min_conf,
            strong_any,
            regime or "-",
            lead_engine,
            target_mult,
        )
        self.logger.info(
            "SIGNAL GENERATED symbol=%s type=%s entry=%.2f sl=%.2f target=%.2f confidence=%.2f engines=%s",
            signal.symbol,
            signal.engine_signal,
            signal.entry_price,
            signal.stop_loss,
            signal.target_price,
            signal.confidence,
            ",".join(row.engine for row in consensus.engine_outputs if row.signal != "NONE"),
        )
        return signal

    def _update_lifecycle(self, tick: MarketTick) -> None:
        for signal in self._active_positions(tick.symbol):
            latest_price = self._current_option_ltp(tick, signal.option_symbol)
            if latest_price is None:
                continue
            signal.current_ltp = round(latest_price, 2)

            t1 = signal.entry_price + (signal.target_price - signal.entry_price) * 0.5
            t2 = signal.target_price
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

            if latest_price >= t1 and signal.stop_loss < signal.entry_price:
                signal.stop_loss = signal.entry_price
                signal.lifecycle_events.append(
                    {
                        "event": "SL_MOVED_TO_ENTRY",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "price": round(latest_price, 2),
                    }
                )

            if latest_price >= t2:
                signal.status = SignalStatus.CLOSED
                signal.closed_at = datetime.now(timezone.utc)
                signal.exit_price = round(latest_price, 2)
                final_leg_pnl = (latest_price - signal.entry_price) * max(signal.remaining_qty, 0.0)
                signal.pnl = round(signal.realized_pnl + final_leg_pnl, 2)
                signal.remaining_qty = 0.0
                signal.lifecycle_events.append(
                    {
                        "event": "TARGET_HIT",
                        "timestamp": signal.closed_at.isoformat(),
                        "price": round(latest_price, 2),
                    }
                )
                signal.lifecycle_events.append(
                    {"event": "CLOSED", "timestamp": signal.closed_at.isoformat(), "reason": "T2_REACHED"}
                )
                self._on_trade_closed(signal, sl_hit=False)
            elif latest_price <= signal.stop_loss:
                signal.status = SignalStatus.SL_HIT
                signal.closed_at = datetime.now(timezone.utc)
                signal.exit_price = round(latest_price, 2)
                final_leg_pnl = (latest_price - signal.entry_price) * max(signal.remaining_qty, 0.0)
                signal.pnl = round(signal.realized_pnl + final_leg_pnl, 2)
                signal.remaining_qty = 0.0
                signal.lifecycle_events.append(
                    {"event": "SL_HIT", "timestamp": signal.closed_at.isoformat(), "price": round(latest_price, 2)}
                )
                self._on_trade_closed(signal, sl_hit=True)
            elif self._is_market_time_exit():
                signal.status = SignalStatus.CLOSED
                signal.closed_at = datetime.now(timezone.utc)
                signal.exit_price = round(latest_price, 2)
                final_leg_pnl = (latest_price - signal.entry_price) * max(signal.remaining_qty, 0.0)
                signal.pnl = round(signal.realized_pnl + final_leg_pnl, 2)
                signal.remaining_qty = 0.0
                signal.lifecycle_events.append(
                    {"event": "TIME_EXIT", "timestamp": signal.closed_at.isoformat(), "price": round(latest_price, 2)}
                )
                self._on_trade_closed(signal, sl_hit=False)

    def _select_option_contract(self, tick: MarketTick, consensus: ConsensusOutput) -> Tuple[Optional[str], Optional[float]]:
        direction = consensus.signal
        strikes = sorted(float(k) for k in tick.option_chain.keys()) if tick.option_chain else []
        if not strikes:
            return None, None
        atm = min(strikes, key=lambda s: abs(s - tick.index_price))
        lead_engine = self._lead_engine(consensus)
        strike_value = self._strategy_strike(atm, direction, lead_engine)
        strike = min(strikes, key=lambda s: abs(s - strike_value))
        leg = "CE" if direction == "BUY_CE" else "PE"
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
        if lead_engine == "hero_zero_engine":
            return atm + (100.0 if direction == "BUY_CE" else -100.0)
        if lead_engine == "smc_engine":
            return atm + (50.0 if direction == "BUY_CE" else -50.0)
        return atm

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
        return [row for row in self.active_signals.get(symbol, []) if row.status == SignalStatus.ACTIVE]

    def _on_trade_closed(self, signal: SignalRecord, *, sl_hit: bool) -> None:
        self.daily_realized_pnl = round(self.daily_realized_pnl + float(signal.pnl or 0.0), 2)
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
        return ts >= time(9, 20)

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

    def active_cards(self) -> List[DashboardSignalCard]:
        cards: List[DashboardSignalCard] = []
        for symbol in ("NIFTY", "SENSEX"):
            for sig in self.active_signals.get(symbol, []):
                if sig.status != SignalStatus.ACTIVE:
                    continue
                cards.append(
                    DashboardSignalCard(
                        signal_id=sig.signal_id,
                        symbol=sig.symbol,
                        signal=sig.engine_signal,
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
                        timestamp=sig.created_at,
                    )
                )
        return cards

