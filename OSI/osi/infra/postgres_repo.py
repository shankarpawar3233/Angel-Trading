from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List

from sqlalchemy import JSON, Column, DateTime, Float, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)


metadata = MetaData()

signals_table = Table(
    "osi_signals",
    metadata,
    Column("signal_id", String(64), primary_key=True),
    Column("symbol", String(16), nullable=False),
    Column("signal", String(16), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("status", String(16), nullable=False),
    Column("entry_price", Float, nullable=False),
    Column("stop_loss", Float, nullable=False),
    Column("target_price", Float, nullable=False),
    Column("option_symbol", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("pnl", Float, nullable=True),
    Column("lifecycle_events", JSON, nullable=False),
)

engine_outputs_table = Table(
    "osi_engine_outputs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("symbol", String(16), nullable=False),
    Column("ts", DateTime(timezone=True), nullable=False),
    Column("payload", JSON, nullable=False),
)


class PostgresRepository:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.engine: AsyncEngine | None = None

    async def connect(self) -> None:
        self.engine = create_async_engine(self.dsn, echo=False, pool_pre_ping=True)
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
            logger.info("PostgreSQL connected and schema ready")
        except Exception as exc:
            logger.warning("PostgreSQL unavailable, persistence disabled: %s", exc)
            self.engine = None

    async def close(self) -> None:
        if self.engine:
            await self.engine.dispose()

    async def store_signal(self, payload: Dict) -> None:
        if self.engine is None:
            return
        async with self.engine.begin() as conn:
            await conn.execute(insert(signals_table).values(**payload))

    async def store_engine_snapshot(self, snapshot_id: str, symbol: str, ts: datetime, payload: Dict) -> None:
        if self.engine is None:
            return
        async with self.engine.begin() as conn:
            await conn.execute(
                insert(engine_outputs_table).values(id=snapshot_id, symbol=symbol, ts=ts, payload=payload)
            )

    async def fetch_history(self, limit: int = 200) -> List[Dict]:
        if self.engine is None:
            return []
        async with self.engine.connect() as conn:
            rows = await conn.execute(select(signals_table).order_by(signals_table.c.created_at.desc()).limit(limit))
            return [dict(row._mapping) for row in rows]

