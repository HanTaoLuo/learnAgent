"""Retry orchestration shared by non-streaming gateway requests."""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.errors import UpstreamError


Result = TypeVar("Result")


@dataclass(frozen=True)
class RetrySettings:
    """非流式调用的重试策略；重试次数始终不超过三次。"""

    max_retries: int = 3
    initial_delay_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not 0 <= self.max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")


def is_retryable(error: UpstreamError) -> bool:
    """只重试临时网络/服务端故障，避免重复无效请求。"""
    return error.code != "api_key_missing" and (error.status_code in {408, 429} or error.status_code >= 500)


async def generate_with_retry(
    operation: Callable[[], Awaitable[Result]], *, settings: RetrySettings
) -> tuple[Result, int]:
    """以指数退避执行非流式上游调用，返回结果和实际重试次数。"""
    retry_count = 0
    while True:
        try:
            return await operation(), retry_count
        except UpstreamError as exc:
            if not is_retryable(exc) or retry_count >= settings.max_retries:
                exc.retry_count = retry_count
                raise
            retry_count += 1
            await asyncio.sleep(settings.initial_delay_seconds * (2 ** (retry_count - 1)))
