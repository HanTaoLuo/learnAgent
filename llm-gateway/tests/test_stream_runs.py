import asyncio

import pytest
from fastapi.testclient import TestClient

from app.adapters import ModelRegistry
from app.config import StreamRunSettings, StreamSettings
from app.data import ModelCapability, ModelRequest, ModelResult, ModelStreamEvent, ResponseFormat
from app.errors import ResourceNotFoundError
from app.main import app
from app.services.rate_limit import InMemoryModelRateLimiter, RateLimitSettings
from app.services.stream_runs import InMemoryStreamRunManager


async def _wait_for_status(manager: InMemoryStreamRunManager, request_id: str, status: str) -> None:
    for _ in range(100):
        if manager.get(request_id, "caller").status == status:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"run {request_id} did not reach {status}")


async def _wait_until_removed(manager: InMemoryStreamRunManager, request_id: str) -> None:
    for _ in range(100):
        try:
            manager.get(request_id, "caller")
        except ResourceNotFoundError:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"run {request_id} was not removed")


def test_stream_run_settings_load_memory_retention_and_default_token_limit(monkeypatch) -> None:
    monkeypatch.setenv("STREAM_DISCONNECT_GRACE_SECONDS", "12")
    monkeypatch.setenv("STREAM_RUN_RETENTION_SECONDS", "34")
    monkeypatch.setenv("STREAM_DEFAULT_MAX_OUTPUT_TOKENS", "567")

    settings = StreamRunSettings.from_env()

    assert settings.disconnect_grace_seconds == 12
    assert settings.retention_seconds == 34
    assert settings.default_max_output_tokens == 567


async def test_reconnect_replays_only_events_after_cursor_without_restarting_upstream() -> None:
    release = asyncio.Event()
    first_delta_ready = asyncio.Event()
    upstream_calls = 0

    async def upstream():
        nonlocal upstream_calls
        upstream_calls += 1
        yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "前"})
        first_delta_ready.set()
        await release.wait()
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "后"})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    manager = InMemoryStreamRunManager(
        StreamRunSettings(disconnect_grace_seconds=1, retention_seconds=1, cleanup_interval_seconds=0.01)
    )
    try:
        manager.start(
            request_id="req-reconnect",
            owner_id="caller",
            model="test-model",
            events=upstream(),
            response_format=ResponseFormat(),
            stream_settings=StreamSettings(heartbeat_interval_seconds=1),
        )
        first_subscription = manager.subscribe("req-reconnect", "caller", after_event_id=0)
        first_frames = [await anext(first_subscription), await anext(first_subscription)]
        await first_delta_ready.wait()
        await first_subscription.aclose()
        assert manager.get("req-reconnect", "caller").status == "running"

        release.set()
        await _wait_for_status(manager, "req-reconnect", "completed")
        resumed_frames = [frame async for frame in manager.subscribe("req-reconnect", "caller", after_event_id=2)]

        assert upstream_calls == 1
        assert b'"delta":"\xe5\x89\x8d"' in first_frames[1]
        assert all(b'"delta":"\xe5\x89\x8d"' not in frame for frame in resumed_frames)
        assert b'"delta":"\xe5\x90\x8e"' in resumed_frames[0]
        assert b"response.completed" in resumed_frames[-1]
    finally:
        await manager.close()


async def test_run_is_cancelled_and_removed_after_disconnected_grace_period() -> None:
    closed = asyncio.Event()

    async def upstream():
        try:
            yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
            await asyncio.Event().wait()
        finally:
            closed.set()

    manager = InMemoryStreamRunManager(
        StreamRunSettings(disconnect_grace_seconds=0.02, retention_seconds=1, cleanup_interval_seconds=0.005)
    )
    try:
        manager.start(
            request_id="req-expire",
            owner_id="caller",
            model="test-model",
            events=upstream(),
            response_format=ResponseFormat(),
            stream_settings=StreamSettings(heartbeat_interval_seconds=1),
        )
        subscription = manager.subscribe("req-expire", "caller")
        await anext(subscription)
        await subscription.aclose()
        await asyncio.wait_for(closed.wait(), timeout=0.5)
        await _wait_until_removed(manager, "req-expire")
        with pytest.raises(ResourceNotFoundError, match="expired"):
            manager.get("req-expire", "caller")
    finally:
        await manager.close()


async def test_explicit_cancel_stops_background_upstream() -> None:
    closed = asyncio.Event()

    async def upstream():
        try:
            yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
            await asyncio.Event().wait()
        finally:
            closed.set()

    manager = InMemoryStreamRunManager(StreamRunSettings(disconnect_grace_seconds=1))
    try:
        manager.start(
            request_id="req-cancel",
            owner_id="caller",
            model="test-model",
            events=upstream(),
            response_format=ResponseFormat(),
            stream_settings=StreamSettings(heartbeat_interval_seconds=1),
        )
        subscription = manager.subscribe("req-cancel", "caller")
        await anext(subscription)
        await manager.cancel("req-cancel", "caller")
        await asyncio.wait_for(closed.wait(), timeout=0.5)
        assert manager.get("req-cancel", "caller").status == "cancelled"
        await subscription.aclose()
    finally:
        await manager.close()


async def test_unexpected_upstream_exception_finishes_run_as_failed() -> None:
    async def upstream():
        yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
        raise RuntimeError("unexpected provider failure")

    manager = InMemoryStreamRunManager(StreamRunSettings(disconnect_grace_seconds=1))
    try:
        manager.start(
            request_id="req-unexpected",
            owner_id="caller",
            model="test-model",
            events=upstream(),
            response_format=ResponseFormat(),
            stream_settings=StreamSettings(heartbeat_interval_seconds=0.01),
        )
        frames = await asyncio.wait_for(
            _collect(manager.subscribe("req-unexpected", "caller")), timeout=0.5
        )
        assert manager.get("req-unexpected", "caller").status == "failed"
        assert b"response.failed" in frames[-1]
    finally:
        await manager.close()


async def _collect(events) -> list[bytes]:
    return [event async for event in events]


def test_generate_exposes_request_id_and_http_reconnect_replays_from_cursor(tmp_path) -> None:
    class StreamingAdapter:
        calls = 0
        received_max_output_tokens: int | None = None

        async def generate(self, request: ModelRequest) -> ModelResult:
            raise AssertionError("non-stream generation should not run")

        async def generate_stream(self, request: ModelRequest):
            self.calls += 1
            self.received_max_output_tokens = request.max_output_tokens
            yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
            yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "前"})
            yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "后"})
            yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    with TestClient(app) as client:
        registry = ModelRegistry()
        adapter = StreamingAdapter()
        registry.register(
            "stream-model", adapter,
            ModelCapability(True, True, "prompt_only", False, True, True, streaming=True),  # type: ignore[arg-type]
        )
        client.app.state.model_registry = registry
        client.app.state.rate_limiter = InMemoryModelRateLimiter(RateLimitSettings(default_requests_per_minute=0))
        client.app.state.stream_run_manager.settings = StreamRunSettings(default_max_output_tokens=321)

        first = client.post("/v1/generate", json={"model": "stream-model", "user": "hello", "stream": True})
        request_id = first.headers["X-Request-ID"]
        status = client.get(f"/v1/streams/{request_id}")
        resumed = client.get(f"/v1/streams/{request_id}/events", params={"after_event_id": 2})

    assert first.status_code == 200
    assert adapter.calls == 1
    assert adapter.received_max_output_tokens == 321
    assert status.json()["status"] == "completed"
    assert '"delta":"前"' not in resumed.text
    assert '"delta":"后"' in resumed.text
    assert "response.completed" in resumed.text
