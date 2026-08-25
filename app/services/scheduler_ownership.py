"""Single scheduler ownership across replicas when Redis is configured."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class SchedulerOwnership:
    def __init__(self, redis_url: str, *, ttl_seconds: int = 120) -> None:
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds
        self.token = uuid.uuid4().hex
        self.key = "xauusd:scheduler:owner"
        self.client: Any = None
        self.task: asyncio.Task | None = None
        self.owned = False

    async def acquire(self) -> bool:
        if not self.redis_url:
            self.owned = True
            return True
        try:
            from redis.asyncio import Redis
            self.client = Redis.from_url(self.redis_url, decode_responses=True)
            self.owned = bool(await self.client.set(
                self.key, self.token, nx=True, ex=self.ttl_seconds))
            if self.owned:
                self.task = asyncio.create_task(self._renew())
            return self.owned
        except Exception as exc:  # noqa: BLE001
            # Production with configured Redis fails closed: two schedulers are
            # more dangerous than an API-only replica.
            logger.error("scheduler ownership unavailable: %s", type(exc).__name__)
            self.owned = False
            return False

    async def _renew(self) -> None:
        while self.owned:
            await asyncio.sleep(self.ttl_seconds / 3)
            try:
                script = (
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end")
                self.owned = bool(await self.client.eval(
                    script, 1, self.key, self.token, self.ttl_seconds))
            except Exception as exc:  # noqa: BLE001
                logger.error("scheduler ownership renewal failed: %s", type(exc).__name__)
                self.owned = False

    async def release(self) -> None:
        self.owned = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        if self.client:
            try:
                script = (
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end")
                await self.client.eval(script, 1, self.key, self.token)
            finally:
                await self.client.aclose()
                self.client = None
