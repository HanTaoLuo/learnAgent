"""Persistent, non-sensitive observability contracts."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


UsageStatus = Literal["completed", "failed", "cancelled"]


class UsageRecord(BaseModel):
    """一条已经结束的模型调用审计记录，不包含请求或输出正文。"""

    request_id: str
    caller_id_hash: str
    model: str
    status: UsageStatus
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float
    first_token_latency_ms: float | None = None
    retry_count: int = 0
    error_type: str | None = None
    error_code: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    created_at: datetime
    updated_at: datetime


class UsageQuery(BaseModel):
    """历史调用记录的安全、有限查询条件。"""

    limit: int = Field(default=100, ge=1, le=1000)
    model: str | None = None
    status: UsageStatus | None = None
    request_id: str | None = None
