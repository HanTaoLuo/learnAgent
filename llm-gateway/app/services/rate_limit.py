"""In-memory per-caller, per-model token-bucket rate limiting."""
import asyncio
import hashlib
import math
import time
from dataclasses import dataclass, field
from typing import Callable

from app.errors import RateLimitError


@dataclass(frozen=True)
class RateLimitSettings:
    """每分钟请求数配置；模型专属值优先于默认值。"""

    default_requests_per_minute: int = 60
    model_requests_per_minute: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.default_requests_per_minute < 0 or any(limit < 0 for limit in self.model_requests_per_minute.values()):
            raise ValueError("rate-limit requests_per_minute values must be non-negative")

    def requests_per_minute_for(self, model: str) -> int:
        """获取一个模型的桶容量与每分钟补充速率。"""
        return self.model_requests_per_minute.get(model, self.default_requests_per_minute)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryModelRateLimiter:
    """单进程令牌桶；每个 (调用方, model) 组合互不影响。"""

    def __init__(self, settings: RateLimitSettings, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, caller_id: str, model: str) -> None:
        """消耗一个请求令牌；不足时抛出携带 Retry-After 的 429 错误。"""
        requests_per_minute = self._settings.requests_per_minute_for(model)
        if requests_per_minute <= 0:
            return

        now = self._clock()
        key = (caller_id, model)
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(requests_per_minute), updated_at=now)
                self._buckets[key] = bucket

            refill_per_second = requests_per_minute / 60
            bucket.tokens = min(
                float(requests_per_minute), bucket.tokens + max(0.0, now - bucket.updated_at) * refill_per_second
            )
            bucket.updated_at = now
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return

            retry_after = max(1, math.ceil((1 - bucket.tokens) / refill_per_second))
            raise RateLimitError(model, retry_after)


def client_identity(authorization: str | None, api_key: str | None, client_host: str | None) -> str:
    """提取限流调用方标识；密钥只以不可逆摘要形式参与内存分桶。"""
    if authorization and authorization.lower().startswith("bearer "):
        value = authorization[7:].strip()
        if value:
            return f"bearer:{hashlib.sha256(value.encode()).hexdigest()}"
    if api_key:
        return f"api_key:{hashlib.sha256(api_key.encode()).hexdigest()}"
    return f"ip:{client_host or 'unknown'}"
