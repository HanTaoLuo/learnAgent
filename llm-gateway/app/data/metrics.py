"""Gateway observability data contracts."""
from typing import Literal

from pydantic import BaseModel


class GatewayMetrics(BaseModel):
    """网关为单次调用计算并返回给调用方的指标。"""

    request_id: str
    model: str
    status: Literal["completed", "failed", "cancelled"]
    latency_ms: float
    first_token_latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    retry_count: int = 0
