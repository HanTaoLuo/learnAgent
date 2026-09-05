"""In-memory background stream runs with cursor-based SSE replay."""
import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Awaitable

from fastapi import Request

from app.config import StreamRunSettings, StreamSettings
from app.data import GatewayMetrics, ModelStreamEvent, ResponseFormat, StreamRunSnapshot
from app.errors import GatewayError, ResourceNotFoundError
from app.services.streaming import sse_events


FinishedCallback = Callable[[GatewayMetrics, str | None, str | None], Awaitable[None] | None]


@dataclass
class _StreamRun:
    request_id: str
    owner_id: str
    model: str
    heartbeat_interval_seconds: float
    status: str = "running"
    frames: list[tuple[int, bytes]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    subscribers: int = 0
    disconnected_at: float | None = None
    terminal_at: float | None = None
    task: asyncio.Task[None] | None = None

    def snapshot(self) -> StreamRunSnapshot:
        return StreamRunSnapshot(
            request_id=self.request_id,
            model=self.model,
            status=self.status,
            last_event_id=self.frames[-1][0] if self.frames else 0,
        )


def _frame_value(frame: bytes, prefix: bytes) -> str | None:
    for line in frame.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].decode()
    return None


class InMemoryStreamRunManager:
    """隐藏后台生成、短期事件缓存、游标订阅和过期回收的深模块。

    事件只存在当前 Python 进程。重新订阅永远读取已有 Run，不会再次调用模型。
    """

    def __init__(self, settings: StreamRunSettings | None = None) -> None:
        self.settings = settings or StreamRunSettings.from_env()
        self._runs: dict[str, _StreamRun] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(
        self,
        *,
        request_id: str,
        owner_id: str,
        model: str,
        events: AsyncIterator[ModelStreamEvent],
        response_format: ResponseFormat,
        stream_settings: StreamSettings,
        event_factory: Callable[[], AsyncIterator[ModelStreamEvent]] | None = None,
        on_finished: FinishedCallback | None = None,
    ) -> StreamRunSnapshot:
        """创建唯一后台生成任务并立即返回稳定 request_id。"""
        if self._closed:
            raise RuntimeError("stream run manager is closed")
        if request_id in self._runs:
            raise GatewayError(409, "Stream run already exists", error_type="conflict_error", code="stream_exists")
        if len(self._runs) >= self.settings.max_cached_runs:
            raise GatewayError(
                503, "Too many active or cached stream runs", error_type="capacity_error", code="stream_capacity_exceeded"
            )
        run = _StreamRun(
            request_id=request_id,
            owner_id=owner_id,
            model=model,
            heartbeat_interval_seconds=stream_settings.heartbeat_interval_seconds,
        )
        self._runs[request_id] = run
        self._ensure_cleanup_task()
        run.task = asyncio.create_task(
            self._produce(
                run,
                events=events,
                response_format=response_format,
                stream_settings=stream_settings,
                event_factory=event_factory,
                on_finished=on_finished,
            )
        )
        return run.snapshot()

    def get(self, request_id: str, owner_id: str) -> StreamRunSnapshot:
        """读取调用方所属 Run 的非敏感状态。"""
        return self._owned_run(request_id, owner_id).snapshot()

    async def subscribe(
        self,
        request_id: str,
        owner_id: str,
        *,
        after_event_id: int = 0,
        client_request: Request | None = None,
    ) -> AsyncIterator[bytes]:
        """先重放游标后的内存事件，再等待该 Run 的实时事件。"""
        run = self._owned_run(request_id, owner_id)
        cursor = max(0, after_event_id)
        async with run.condition:
            run.subscribers += 1
            run.disconnected_at = None
        try:
            while True:
                if client_request is not None and await client_request.is_disconnected():
                    return
                async with run.condition:
                    pending = [(event_id, frame) for event_id, frame in run.frames if event_id > cursor]
                    terminal = run.status != "running"
                    if not pending and not terminal:
                        try:
                            await asyncio.wait_for(
                                run.condition.wait(), timeout=max(0.001, run.heartbeat_interval_seconds)
                            )
                        except TimeoutError:
                            pass
                        pending = [(event_id, frame) for event_id, frame in run.frames if event_id > cursor]
                        terminal = run.status != "running"
                if pending:
                    for event_id, frame in pending:
                        if client_request is not None and await client_request.is_disconnected():
                            return
                        cursor = event_id
                        yield frame
                    continue
                if terminal:
                    return
                yield b": ping\n\n"
        finally:
            async with run.condition:
                run.subscribers = max(0, run.subscribers - 1)
                if run.subscribers == 0 and run.status == "running":
                    run.disconnected_at = time.monotonic()

    async def cancel(self, request_id: str, owner_id: str) -> StreamRunSnapshot:
        """显式取消后台生成；区别于仅关闭某个订阅。"""
        run = self._owned_run(request_id, owner_id)
        if run.status != "running" or run.task is None:
            return run.snapshot()
        run.task.cancel()
        with suppress(asyncio.CancelledError):
            await run.task
        if run.status == "running":
            await self._set_terminal(run, "cancelled")
        return run.snapshot()

    async def close(self) -> None:
        """停止清理任务并关闭所有仍在运行的上游流。"""
        self._closed = True
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
        tasks = [run.task for run in self._runs.values() if run.task is not None and not run.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._runs.clear()

    def _owned_run(self, request_id: str, owner_id: str) -> _StreamRun:
        run = self._runs.get(request_id)
        # 对不存在和不属于当前调用方的 ID 使用相同响应，避免泄漏 Run 是否存在。
        if run is None or run.owner_id != owner_id:
            raise ResourceNotFoundError("Stream run expired or not found", code="stream_not_found")
        return run

    async def _produce(
        self,
        run: _StreamRun,
        *,
        events: AsyncIterator[ModelStreamEvent],
        response_format: ResponseFormat,
        stream_settings: StreamSettings,
        event_factory: Callable[[], AsyncIterator[ModelStreamEvent]] | None,
        on_finished: FinishedCallback | None,
    ) -> None:
        outcome: tuple[str, str | None, str | None] | None = None

        async def finished(metrics: GatewayMetrics, error_type: str | None, error_code: str | None) -> None:
            nonlocal outcome
            outcome = (metrics.status, error_type, error_code)
            if on_finished is not None:
                result = on_finished(metrics, error_type, error_code)
                if result is not None:
                    await result

        try:
            async for frame in sse_events(
                events,
                response_format,
                event_factory=event_factory,
                settings=stream_settings,
                request_id=run.request_id,
                model=run.model,
                on_finished=finished,
            ):
                # 上游等待产生的心跳属于连接而不是 Run 事件，不进入重放缓存。
                event_id_text = _frame_value(frame, b"id: ")
                if event_id_text is None:
                    continue
                event_id = int(event_id_text)
                event_type = _frame_value(frame, b"event: ")
                if event_type not in {
                    "response.created", "response.output_text.delta", "response.completed", "response.failed"
                }:
                    # 推理过程和供应商私有事件不属于当前统一输出契约，也不占用重连缓存。
                    continue
                async with run.condition:
                    run.frames.append((event_id, frame))
                    if event_type == "response.completed":
                        run.status = "completed"
                        run.terminal_at = time.monotonic()
                    elif event_type == "response.failed":
                        run.status = "failed"
                        run.terminal_at = time.monotonic()
                    run.condition.notify_all()
        except asyncio.CancelledError:
            if run.status == "running":
                await self._set_terminal(run, "cancelled")
            raise
        finally:
            if outcome is not None and outcome[0] == "cancelled" and run.status == "running":
                await self._set_terminal(run, "cancelled")

    async def _set_terminal(self, run: _StreamRun, status: str) -> None:
        async with run.condition:
            run.status = status
            run.terminal_at = time.monotonic()
            run.condition.notify_all()

    def _ensure_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(max(0.001, self.settings.cleanup_interval_seconds))
            now = time.monotonic()
            disconnected = [
                run
                for run in self._runs.values()
                if run.status == "running"
                and run.subscribers == 0
                and run.disconnected_at is not None
                and now - run.disconnected_at >= self.settings.disconnect_grace_seconds
            ]
            for run in disconnected:
                await self.cancel(run.request_id, run.owner_id)
                self._runs.pop(run.request_id, None)
            expired = [
                run.request_id
                for run in self._runs.values()
                if run.status != "running"
                and run.subscribers == 0
                and run.terminal_at is not None
                and now - run.terminal_at >= self.settings.retention_seconds
            ]
            for request_id in expired:
                self._runs.pop(request_id, None)
