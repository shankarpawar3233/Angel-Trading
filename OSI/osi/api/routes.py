from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from osi.services.pipeline import OSIPipeline


def build_router(pipeline: OSIPipeline):
    router = APIRouter()
    dashboard_file = Path(__file__).resolve().parents[1] / "ui" / "dashboard.html"

    @router.get("/signals")
    async def get_signals() -> Dict:
        cards = [card.model_dump(mode="json") for card in pipeline.signal_manager.active_cards()]
        return {
            "cards": cards,
            "count": len(cards),
            "ui_schema": "dashboard_card_v1",
        }

    @router.get("/history")
    async def get_history(limit: int = 100) -> Dict:
        db_rows = await pipeline.postgres_repo.fetch_history(limit=min(limit, 500))
        if db_rows:
            non_active = [row for row in db_rows if str(row.get("status") or "").upper() != "ACTIVE"]
            return {"history": db_rows, "non_active_history": non_active, "count": len(db_rows)}
        mem_rows = [row.model_dump(mode="json") for row in pipeline.signal_manager.history[-min(limit, 500):]]
        if mem_rows:
            rows = list(reversed(mem_rows))
            non_active = [row for row in rows if str(row.get("status") or "").upper() != "ACTIVE"]
            return {"history": rows, "non_active_history": non_active, "count": len(rows)}
        cand_rows = list(reversed(pipeline.decision_history[-min(limit, 500):]))
        non_active = [row for row in cand_rows if str(row.get("status") or "").upper() != "ACTIVE"]
        return {"history": cand_rows, "non_active_history": non_active, "count": len(cand_rows)}

    @router.get("/metrics")
    async def get_metrics() -> Dict:
        data = pipeline.metrics.snapshot()
        data["target_latency_ms"] = 500
        data["within_latency_budget"] = data["average_latency_ms"] < 500
        return data

    @router.websocket("/signals/live")
    async def ws_signals_live(ws: WebSocket):
        await ws.accept()
        queue = pipeline.subscribe()
        try:
            while True:
                payload = await queue.get()
                await ws.send_json(payload)
        except WebSocketDisconnect:
            pipeline.unsubscribe(queue)
        except asyncio.CancelledError:
            pipeline.unsubscribe(queue)
            raise

    @router.get("/dashboard")
    async def get_dashboard():
        return FileResponse(dashboard_file)

    return router

