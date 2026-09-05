"""Gateway metric construction shared by streaming and non-streaming calls."""
import time
from typing import Literal

from app.data import GatewayMetrics


def build_gateway_metrics(
    *,
    request_id: str,
    model: str,
    status: Literal["completed", "failed", "cancelled"],
    started: float,
    first_token_latency_ms: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    retry_count: int = 0,
) -> GatewayMetrics:
    """根据调用状态和 monotonic 起始时间生成统一指标。"""
    return GatewayMetrics(
        request_id=request_id,
        model=model,
        status=status,
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
        first_token_latency_ms=first_token_latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        retry_count=retry_count,
    )
