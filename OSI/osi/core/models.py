from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


SignalType = Literal["BUY_CE", "BUY_PE", "NONE"]


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    ACTIVE = "ACTIVE"
    TARGET_HIT = "TARGET_HIT"
    SL_HIT = "SL_HIT"
    CLOSED = "CLOSED"


class MarketTick(BaseModel):
    symbol: Literal["NIFTY", "SENSEX"]
    index_price: float
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    exchange_timestamp: Optional[datetime] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # expiry -> strike -> leg(CE/PE) -> market fields
    option_chain: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class Candle(BaseModel):
    symbol: Literal["NIFTY", "SENSEX"]
    timeframe: Literal["1m", "3m", "5m"]
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    start: datetime
    end: datetime
    option_chain: Dict[str, Dict[str, Dict[str, Dict[str, Any]]]] = Field(default_factory=dict)
    meta: Dict[str, Any] = Field(default_factory=dict)


class EngineOutput(BaseModel):
    engine: str
    signal: SignalType
    strength: float
    confidence: Optional[float] = None
    reason: Optional[str] = None


class ConsensusOutput(BaseModel):
    signal: SignalType
    confidence: float
    weighted_score: float
    engine_outputs: List[EngineOutput] = Field(default_factory=list)
    market_phase: str = "OFF"
    engine_weights: Dict[str, float] = Field(default_factory=dict)
    selected_engine: str = ""


class SignalRecord(BaseModel):
    signal_id: str
    symbol: str
    signal: SignalType
    engine_signal: SignalType
    strategy: str = "scalping"
    trigger_engine: str = ""
    confidence: float
    strength: float = 0.0
    status: SignalStatus = SignalStatus.ACTIVE
    signal_tag: Literal["INTRABAR", "ZERO_HERO", "STANDARD"] = "STANDARD"
    quality_tag: Literal["LOW", "NORMAL", "STRONG"] = "NORMAL"
    trade_state: Literal["ACTIVE", "RECOVERED"] = "ACTIVE"
    expiry: str = ""
    entry_price: float
    current_ltp: Optional[float] = None
    stop_loss: float
    target_1: float = 0.0
    target_2: float = 0.0
    target_3: Optional[float] = None
    target_price: float
    option_symbol: str
    strike: Optional[float] = None
    option_type: Optional[str] = None
    option_token: Optional[str] = None
    reason: str = ""
    exit_price: Optional[float] = None
    stop_reference_index: Optional[float] = None
    partial_booked: bool = False
    quantity: float = 1.0
    remaining_qty: float = 1.0
    realized_pnl: float = 0.0
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    # Lifecycle (frozen at entry for momentum exit; optional raw watermark)
    entry_confidence: float = 0.0
    max_favorable_price: Optional[float] = None
    tick_count_since_last_move: int = 0
    confidence_breakdown: Dict[str, float] = Field(default_factory=dict)
    entry_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recovered_at: Optional[datetime] = None
    last_ltp_update_at: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
    pnl: Optional[float] = None
    lifecycle_events: List[Dict[str, Any]] = Field(default_factory=list)


class DashboardSignalCard(BaseModel):
    signal_id: str
    symbol: str
    signal: SignalType
    confidence: float
    status: SignalStatus
    entry_price: float
    stop_loss: float
    target_price: float
    target_1: float = 0.0
    target_2: float = 0.0
    target_3: Optional[float] = None
    option_symbol: str
    strike: Optional[float] = None
    current_ltp: Optional[float] = None
    exit_price: Optional[float] = None
    stop_reference_index: Optional[float] = None
    partial_booked: bool = False
    remaining_qty: float = 1.0
    realized_pnl: float = 0.0
    timestamp: datetime


@dataclass
class RuntimeMetrics:
    ticks_received: int = 0
    ticks_dropped: int = 0
    ticks_processed: int = 0
    pipeline_errors: int = 0
    signals_generated: int = 0
    signals_executed: int = 0
    signals_rejected: int = 0
    rejected_by_reason: Dict[str, int] = field(default_factory=dict)
    average_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    last_tick_latency_ms: float = 0.0
    average_tick_to_process_latency_ms: float = 0.0
    average_process_to_signal_latency_ms: float = 0.0
    average_total_signal_latency_ms: float = 0.0
    last_tick_to_process_latency_ms: float = 0.0
    last_process_to_signal_latency_ms: float = 0.0
    last_total_signal_latency_ms: float = 0.0
    option_data_latency_ms: float = 0.0
    engine_exec_ms: Dict[str, float] = field(default_factory=dict)
    confidence_distribution: Dict[str, int] = field(default_factory=lambda: {"0_59": 0, "60_79": 0, "80_100": 0})
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    current_day_pnl: float = 0.0
    last_5_trades: List[Dict[str, Any]] = field(default_factory=list)

