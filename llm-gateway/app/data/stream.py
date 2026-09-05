"""Provider-neutral streaming data contracts."""
from typing import Any, Literal

from pydantic import BaseModel


class ModelStreamEvent(BaseModel):
    """协议适配器输出的统一 SSE 事件。"""

    type: str
    data: dict[str, Any]
    event_id: str | None = None


class StreamRunSnapshot(BaseModel):
    """可公开查询的内存流状态，不包含请求正文或模型输出。"""

    request_id: str
    model: str
    status: Literal["running", "completed", "failed", "cancelled"]
    last_event_id: int
