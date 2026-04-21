from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from osi.api.routes import build_router
from osi.core.config import settings
from osi.core.logging import setup_logging
from osi.data.angel_ws import AngelWebSocketSource
from osi.infra.postgres_repo import PostgresRepository
from osi.infra.redis_store import RedisStateStore
from osi.services.metrics import MetricsService
from osi.services.pipeline import OSIPipeline

setup_logging()

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_store = RedisStateStore(settings.redis_url)
postgres_repo = PostgresRepository(settings.postgres_url)
metrics = MetricsService()
pipeline = OSIPipeline(redis_store=redis_store, postgres_repo=postgres_repo, metrics=metrics)
tick_source = AngelWebSocketSource()

app.include_router(build_router(pipeline))


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.app_name}


@app.on_event("startup")
async def startup_event():
    # Standalone default mode: keep runtime in-process, no external DB/cache required.
    await pipeline.start()
    await tick_source.start(pipeline.on_tick)


@app.on_event("shutdown")
async def shutdown_event():
    await tick_source.stop()
    await pipeline.stop()

