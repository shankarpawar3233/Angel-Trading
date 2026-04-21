from __future__ import annotations

import json
import logging
from typing import Any, Dict

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisStateStore:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self.client: Redis | None = None
        self._memory_fallback: Dict[str, Any] = {}

    async def connect(self) -> None:
        try:
            self.client = Redis.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info("Redis connected: %s", self.redis_url)
        except Exception as exc:
            self.client = None
            logger.warning("Redis not available, using in-memory fallback: %s", exc)

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def set_json(self, key: str, value: Dict[str, Any]) -> None:
        if self.client is None:
            self._memory_fallback[key] = value
            return
        await self.client.set(key, json.dumps(value, default=str))

    async def get_json(self, key: str) -> Dict[str, Any] | None:
        if self.client is None:
            v = self._memory_fallback.get(key)
            return v if isinstance(v, dict) else None
        row = await self.client.get(key)
        if row is None:
            return None
        return json.loads(row)

