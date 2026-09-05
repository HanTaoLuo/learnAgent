"""SSE lifecycle orchestration independent of model protocols."""
import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Awaitable, Literal
from uuid import uuid4

from fastapi import Request

from app.config import StreamSettings
from app.data import GatewayMetrics, ModelStreamEvent, ResponseFormat
from app.errors import GatewayError, StructuredOutputError, UpstreamError
from app.services.metrics import build_gateway_metrics
from app.services.structured_output import validate_text


def encode_sse(event: ModelStreamEvent) -> bytes:
    """将统一事件编码为标准 SSE 帧。"""
    data = json.dumps(event.data, ensure_ascii=False, separators=(",", ":"))
    event_id = f"id: {event.event_id}\n" if event.event_id else ""
    return f"{event_id}event: {event.type}\ndata: {data}\n\n".encode()


@dataclass
class _StreamState:
    """单条流的内部状态；不会暴露给路由或 Adapter。"""

    request_id: str
    model: str
    started: float = field(default_factory=time.perf_counter)
    last_activity: float = field(init=False)
    output_chunks: list[str] = field(default_factory=list)
    first_token_latency_ms: float | None = None
    retries: int = 0
    emitted_text: bool = False
    sequence: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def __post_init__(self) -> None:
        self.last_activity = self.started

    def observe(self, event: ModelStreamEvent) -> None:
        """提取 usage 和首 Token 指标，并聚合结构化输出文本。"""
        self.last_activity = time.perf_counter()
        usage_source = event.data.get("response", event.data)
        usage = usage_source.get("usage", {}) if isinstance(usage_source, dict) else {}
        if isinstance(usage, dict):
            self.input_tokens = usage.get("input_tokens", self.input_tokens)
            self.output_tokens = usage.get("output_tokens", self.output_tokens)
            self.total_tokens = usage.get("total_tokens", self.total_tokens)
            if self.total_tokens is None and self.input_tokens is not None and self.output_tokens is not None:
                self.total_tokens = self.input_tokens + self.output_tokens
        delta = event.data.get("delta")
        if event.type == "response.output_text.delta" and isinstance(delta, str):
            self.output_chunks.append(delta)
            if not self.emitted_text:
                self.emitted_text = True
                self.first_token_latency_ms = round((time.perf_counter() - self.started) * 1000, 3)

    def metrics_object(self, status: Literal["completed", "failed", "cancelled"]) -> GatewayMetrics:
        """构造最终事件需要携带的网关指标。"""
        return build_gateway_metrics(
            request_id=self.request_id,
            model=self.model,
            status=status,
            started=self.started,
            first_token_latency_ms=self.first_token_latency_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            retry_count=self.retries,
        )

    def metrics(self, status: Literal["completed", "failed", "cancelled"]) -> dict[str, object]:
        """将最终指标序列化为 SSE data 可携带的 JSON 对象。"""
        return self.metrics_object(status).model_dump(mode="json")

    def prepare(self, event: ModelStreamEvent) -> ModelStreamEvent:
        """分配事件编号并为唯一终态注入指标。"""
        self.sequence += 1
        event.event_id = str(self.sequence)
        if event.type == "response.created":
            # 让客户端在首事件时就获得可查询的网关 request_id；这不是断线重放令牌。
            event.data = {**event.data, "gateway_request_id": self.request_id}
        if event.type in {"response.completed", "response.failed"}:
            status = "completed" if event.type == "response.completed" else "failed"
            event.data = {**event.data, "gateway_metrics": self.metrics(status)}
        return event


async def _close_iterator(iterator: AsyncIterator[ModelStreamEvent]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        await close()


def _failed_event(state: _StreamState, error: Exception) -> ModelStreamEvent:
    if isinstance(error, GatewayError):
        error_type, error_code = error.error_type, error.code
        message = str(error)
    elif isinstance(error, TimeoutError):
        error_type, error_code = "upstream_error", "stream_timeout"
        message = str(error)
    else:
        error_type, error_code = "upstream_error", "upstream_error"
        message = "Unexpected upstream stream failure"
    return state.prepare(
        ModelStreamEvent(
            type="response.failed",
            data={
                "type": "response.failed",
                "error": {"message": message, "type": error_type, "code": error_code},
            },
        )
    )


def _event_error_details(event: ModelStreamEvent) -> tuple[str | None, str | None]:
    """从上游已规范化的 failed 事件中提取可审计错误元数据。"""
    error = event.data.get("error", {})
    if not isinstance(error, dict):
        return "upstream_error", "upstream_error"
    return str(error.get("type", "upstream_error")), str(error.get("code", "upstream_error"))


async def sse_events(
    events: AsyncIterator[ModelStreamEvent],
    response_format: ResponseFormat,
    *,
    event_factory: Callable[[], AsyncIterator[ModelStreamEvent]] | None = None,
    client_request: Request | None = None,
    settings: StreamSettings | None = None,
    request_id: str | None = None,
    model: str = "",
    on_finished: Callable[[GatewayMetrics, str | None, str | None], Awaitable[None] | None] | None = None,
) -> AsyncIterator[bytes]:
    """输出具有断开、心跳、超时、重试、校验与唯一终态语义的 SSE。"""
    settings = settings or StreamSettings.from_env()
    state = _StreamState(request_id=request_id or f"req_{uuid4().hex}", model=model)
    finished = False

    async def finish(
        status: Literal["completed", "failed", "cancelled"],
        error_type: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """调用一次可选终态钩子；用于持久化但不影响 SSE 协议。"""
        nonlocal finished
        if finished:
            return
        finished = True
        if on_finished is None:
            return
        result = on_finished(state.metrics_object(status), error_type, error_code)
        if inspect.isawaitable(result):
            await result

    while True:
        iterator = events
        next_event = asyncio.ensure_future(anext(iterator))
        try:
            while True:
                if client_request is not None and await client_request.is_disconnected():
                    next_event.cancel()
                    await finish("cancelled")
                    return
                timeout_limit = (
                    settings.first_token_timeout_seconds if not state.emitted_text else settings.idle_timeout_seconds
                )
                elapsed = time.perf_counter() - (state.started if not state.emitted_text else state.last_activity)
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(next_event),
                        timeout=min(settings.heartbeat_interval_seconds, max(0.0, timeout_limit - elapsed)),
                    )
                except TimeoutError:
                    elapsed = time.perf_counter() - (state.started if not state.emitted_text else state.last_activity)
                    if elapsed >= timeout_limit:
                        raise TimeoutError("first token timeout" if not state.emitted_text else "stream idle timeout")
                    yield b": ping\n\n"
                    continue

                next_event = asyncio.ensure_future(anext(iterator))
                state.observe(event)
                if event.type == "response.completed" and response_format.type != "text":
                    validate_text("".join(state.output_chunks), response_format)
                prepared = state.prepare(event)
                if event.type == "response.completed":
                    await finish("completed")
                elif event.type == "response.failed":
                    error_type, error_code = _event_error_details(event)
                    await finish("failed", error_type, error_code)
                yield encode_sse(prepared)
                if event.type in {"response.completed", "response.failed"}:
                    return
        except StopAsyncIteration:
            error: Exception = UpstreamError(502, "upstream stream ended without a terminal event")
        except asyncio.CancelledError:
            await finish("cancelled")
            raise
        except (TimeoutError, UpstreamError, StructuredOutputError) as exc:
            error = exc
        except Exception as exc:
            error = exc
        finally:
            if not next_event.done():
                next_event.cancel()
                try:
                    await next_event
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            await _close_iterator(iterator)

        if not state.emitted_text and event_factory is not None and state.retries < settings.max_retries:
            state.retries += 1
            await asyncio.sleep(0.25 * (2 ** (state.retries - 1)))
            events = event_factory()
            continue
        failed_event = _failed_event(state, error)
        error_type, error_code = _event_error_details(failed_event)
        await finish("failed", error_type, error_code)
        yield encode_sse(failed_event)
        return
