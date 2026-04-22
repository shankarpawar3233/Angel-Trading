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


class SignalManager:
    def __init__(self, metrics: "MetricsService | None" = None) -> None:
        self.expiry_engine = ExpiryEngine()
        self.logger = logging.getLogger(__name__)
        self.metrics = metrics
        self.active_signals: Dict[str, List[SignalRecord]] = {"NIFTY": [], "SENSEX": []}
        self.history: List[SignalRecord] = []
        self.rejected_signals: List[Dict] = []
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
        self.last_decision_note: Dict[str, str] = {}
        self.last_confidence_breakdown: Dict[str, Dict[str, float]] = {}
        self._last_intrabar_fire_at: Dict[str, datetime] = {}
        self._market_range_hist: Dict[str, Deque[float]] = {"NIFTY": deque(maxlen=60), "SENSEX": deque(maxlen=60)}
        self._load_storage()

    def process_consensus(self, tick: MarketTick, consensus: ConsensusOutput) -> Optional[SignalRecord]:
        self._reset_daily_risk_if_needed()
        self._update_lifecycle(tick)
        if self._risk_blocked():
            self._mark_rejected(tick, consensus, "risk_blocked", breakdown=self._empty_breakdown(float(consensus.confidence)))
            return None
        if not self._is_trade_time_open():
            self.logger.debug("SIGNAL BLOCKED symbol=%s reason=before_0920_ist", tick.symbol)
            self._mark_rejected(tick, consensus, "before_0920_ist", breakdown=self._empty_breakdown(float(consensus.confidence)))
            return None

        lead_engine = self._lead_engine(consensus)
        regime = str(tick.meta.get("regime") or "").upper()
        min_conf = max(30.0, float(settings.min_signal_confidence) - 8.0)
        strong_any = any(float(row.strength) > 0.7 for row in consensus.engine_outputs)
        directional_strength = max(
            [float(row.strength) for row in consensus.engine_outputs if row.signal == consensus.signal] or [0.0]
        )

        base_confidence = float(consensus.confidence)
        if base_confidence < 20.0:
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

        if consensus.signal == "NONE":
            breakdown = self._build_breakdown(
                base=base_confidence,
                primary=primary_boost,
                directional=directional_boost,
                vwap=vwap_boost,
                regime=regime_boost,
                momentum=0.0,
                dead_zone=0.0,
                final=max(0.0, min(100.0, base_confidence + total_boost)),
            )
            self._mark_rejected(tick, consensus, "none_signal", notes, breakdown)
            return None

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
        now_ts = (tick.received_at if isinstance(tick.received_at, datetime) else datetime.now(timezone.utc)).timestamp()
        opt_ts = float(option_ts or 0.0)
        option_latency_ms = max(0.0, (now_ts - opt_ts) * 1000.0) if opt_ts > 0 else 1e9
        if self.metrics is not None:
            self.metrics.record_option_data_latency(option_latency_ms)
        if option_latency_ms > 1000.0:
            self.logger.warning(
                "Signal rejected due to stale option data symbol=%s option=%s option_latency_ms=%.2f",
                tick.symbol,
                option_symbol,
                option_latency_ms,
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
            momentum_adjustment -= 10.0 if primary_trigger else 15.0
            notes.append("option_momentum_penalty")

        if lead_engine == "intrabar_engine":
            now = datetime.now(timezone.utc)
            last_fire = self._last_intrabar_fire_at.get(tick.symbol)
            if last_fire and (now - last_fire).total_seconds() < 30.0:
                momentum_adjustment -= 15.0
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

        if (consensus.confidence < min_conf and not primary_trigger and not strong_any):
            self._mark_rejected(tick, consensus, "low_dynamic_conf", notes, breakdown)
            return None
        if (not primary_trigger) and base_confidence < 35.0 and directional_strength < 0.7:
            self._mark_rejected(tick, consensus, "no_primary_or_strong_base", notes, breakdown)
            return None
        if not bool(opt_momentum.get("option_momentum_confirmed", False)):
            if float(consensus.confidence) < min_conf:
                self._mark_rejected(tick, consensus, "option_momentum_unconfirmed", notes, breakdown)
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
        strategy = self._strategy_name(lead_engine)
        strength = max([float(row.strength) for row in consensus.engine_outputs if row.signal == consensus.signal] or [0.0])
        reason = next((str(row.reason or "") for row in consensus.engine_outputs if row.engine == lead_engine), "")
        expiry = self._expiry_from_option_symbol(option_symbol)
        now = datetime.now(timezone.utc)
        signal = SignalRecord(
            signal_id=f"OSI-{uuid4().hex[:10].upper()}",
            symbol=tick.symbol,
            signal=consensus.signal,
            engine_signal=consensus.signal,
            strategy=strategy,
            confidence=consensus.confidence,
            strength=round(strength, 3),
            expiry=expiry,
            entry_price=round(option_ltp, 2),
            current_ltp=round(option_ltp, 2),
            stop_loss=stop_loss,
            target_1=t1,
            target_2=t2,
            target_price=t2,
            option_symbol=option_symbol,
            strike=self._strike_from_option_symbol(option_symbol),
            reason=reason,
            exit_price=None,
            stop_reference_index=stop_reference_index,
            partial_booked=False,
            quantity=1.0,
            remaining_qty=1.0,
            realized_pnl=0.0,
            max_favorable=0.0,
            max_adverse=0.0,
            confidence_breakdown=breakdown,
            entry_time=now,
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
        self._persist_storage()
        self.logger.debug(
            "SIGNAL DECISION symbol=%s signal=%s confidence=%.2f breakdown=%s reason=allowed notes=%s",
            tick.symbol,
            consensus.signal,
            float(consensus.confidence),
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
        self.last_decision_note[tick.symbol] = f"allowed:{'|'.join(notes)}"
        return signal

    def _update_lifecycle(self, tick: MarketTick) -> None:
        for signal in self._active_positions(tick.symbol):
            latest_price = self._current_option_ltp(tick, signal.option_symbol)
            if latest_price is None:
                continue
            signal.current_ltp = round(latest_price, 2)
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
            elif latest_price <= signal.stop_loss:
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
        self._persist_storage()

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
        if lead_engine == "hero_zero_engine":
            return atm + (100.0 if direction == "BUY_CE" else -100.0)
        if lead_engine == "smc_engine":
            return atm + (50.0 if direction == "BUY_CE" else -50.0)
        return atm

    @staticmethod
    def _strategy_name(lead_engine: str) -> str:
        if lead_engine == "smart_breakout_engine":
            return "smart_breakout"
        if lead_engine == "intrabar_engine":
            return "intrabar"
        return "scalping"

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
        return [row for row in self.active_signals.get(symbol, []) if row.status == SignalStatus.ACTIVE]

    def _on_trade_closed(self, signal: SignalRecord, *, sl_hit: bool) -> None:
        self.daily_realized_pnl = round(self.daily_realized_pnl + float(signal.pnl or 0.0), 2)
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

    def _persist_storage(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_signals": self.snapshot_active_signals(),
            "history": self.snapshot_history(),
            "rejected_signals": list(self.rejected_signals[-self.max_history_records :]),
        }
        self.storage_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_storage(self) -> None:
        try:
            if not self.storage_path.exists():
                self._persist_storage()
                return
            raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, JSONDecodeError, ValueError):
            self.logger.exception("Failed to load signals storage, starting clean")
            raw = {"active_signals": [], "history": []}
        self.active_signals = {"NIFTY": [], "SENSEX": []}
        self.history = []
        self.rejected_signals = []
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
            if sig.status == SignalStatus.ACTIVE:
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
            if sig.status != SignalStatus.ACTIVE:
                self.history.append(sig)
        self.history = self.history[-self.max_history_records :]
        for row in raw.get("rejected_signals", []):
            if isinstance(row, dict):
                self.rejected_signals.append(row)
        self.rejected_signals = self.rejected_signals[-self.max_history_records :]

    def snapshot_active_signals(self) -> List[Dict]:
        rows: List[Dict] = []
        for symbol in ("NIFTY", "SENSEX"):
            for sig in self._active_positions(symbol):
                rows.append(sig.model_dump(mode="json"))
        return rows

    def snapshot_history(self) -> List[Dict]:
        return [row.model_dump(mode="json") for row in self.history[-self.max_history_records :]]

    def snapshot_rejected(self) -> List[Dict]:
        return list(self.rejected_signals[-self.max_history_records :])

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
        if signal in self.active_signals.get(signal.symbol, []):
            self.active_signals[signal.symbol] = [x for x in self.active_signals.get(signal.symbol, []) if x.signal_id != signal.signal_id]
        self.history.append(signal)
        self.history = self.history[-self.max_history_records :]
        self._on_trade_closed(signal, sl_hit=sl_hit)
        self._persist_storage()

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
            "SIGNAL DECISION symbol=%s signal=%s confidence=%.2f breakdown=%s reason=rejected notes=%s",
            tick.symbol,
            consensus.signal,
            float(consensus.confidence),
            conf_breakdown,
            "|".join(note_rows),
        )
        self._persist_storage()

    def latest_decision_note(self, symbol: str) -> str:
        return self.last_decision_note.get(symbol, "")

    def latest_confidence_breakdown(self, symbol: str) -> Dict[str, float]:
        return dict(self.last_confidence_breakdown.get(symbol, {}))

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
            return -10.0
        return 0.0

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

