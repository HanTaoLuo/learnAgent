import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.adapters import ModelRegistry
from app.config import StreamSettings
from app.data import GatewayMetrics, ModelCapability, ModelRequest, ModelResult, ModelStreamEvent, ResponseFormat, UsageQuery, UsageRecord
from app.errors import UpstreamError
from app.main import app
from app.services.rate_limit import InMemoryModelRateLimiter, RateLimitSettings
from app.services.retry import RetrySettings
from app.services.streaming import sse_events
from app.services.usage import UsageRepository
from app.services.prompts import PromptService
from app.data import PromptVersionCreate


def _record(request_id: str, *, model: str = "model-a", status: str = "completed") -> UsageRecord:
    now = datetime.now(UTC)
    return UsageRecord(
        request_id=request_id, caller_id_hash="safe-hash", model=model, status=status,
        input_tokens=None, output_tokens=3, total_tokens=None, latency_ms=12.5,
        first_token_latency_ms=None, retry_count=1, created_at=now, updated_at=now,
    )


def test_usage_repository_persists_nulls_and_filters_after_reopen(tmp_path) -> None:
    path = str(tmp_path / "gateway.db")
    repository = UsageRepository(path)
    repository.record(_record("req-one", model="model-a"))
    repository.record(_record("req-two", model="model-b", status="failed"))

    reopened = UsageRepository(path)
    records = reopened.list(UsageQuery(model="model-a"))
    failed = reopened.list(UsageQuery(status="failed", request_id="req-two"))

    assert len(records) == 1
    assert records[0].request_id == "req-one"
    assert records[0].input_tokens is None
    assert records[0].total_tokens is None
    assert len(failed) == 1
    assert failed[0].model == "model-b"


async def test_streaming_callback_records_completed_failed_and_cancelled_states() -> None:
    outcomes: list[tuple[str, str | None, str | None, GatewayMetrics]] = []

    async def callback(metrics: GatewayMetrics, error_type: str | None, error_code: str | None) -> None:
        outcomes.append((metrics.status, error_type, error_code, metrics))

    async def completed_stream():
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "ok"})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    async def failed_stream():
        raise UpstreamError(502, "offline")
        yield  # pragma: no cover

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    async def waiting_stream():
        await asyncio.sleep(1)
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    _ = [frame async for frame in sse_events(completed_stream(), ResponseFormat(), on_finished=callback, model="m")]
    _ = [
        frame async for frame in sse_events(
            failed_stream(), ResponseFormat(), on_finished=callback, model="m", settings=StreamSettings(max_retries=0)
        )
    ]
    _ = [
        frame async for frame in sse_events(
            waiting_stream(), ResponseFormat(), on_finished=callback, model="m", client_request=DisconnectedRequest()
        )
    ]

    assert [outcome[0] for outcome in outcomes] == ["completed", "failed", "cancelled"]
    assert outcomes[0][3].first_token_latency_ms is not None
    assert outcomes[1][1:] == ("upstream_error", "upstream_error", outcomes[1][3])


def test_generate_persists_completed_and_failed_non_stream_calls(tmp_path) -> None:
    class SuccessfulAdapter:
        async def generate(self, request: ModelRequest) -> ModelResult:
            return ModelResult(
                id="success", model=request.model,
                output=[{"content": [{"type": "output_text", "text": "ok"}]}],
                usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            )

        async def generate_stream(self, request: ModelRequest):
            yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    class FailedAdapter(SuccessfulAdapter):
        async def generate(self, request: ModelRequest) -> ModelResult:
            raise UpstreamError(502, "offline")

    with TestClient(app) as client:
        registry = ModelRegistry()
        capabilities = ModelCapability(True, True, "prompt_only", False, True, True, streaming=True)
        registry.register("success-model", SuccessfulAdapter(), capabilities)  # type: ignore[arg-type]
        registry.register("failed-model", FailedAdapter(), capabilities)  # type: ignore[arg-type]
        client.app.state.model_registry = registry
        client.app.state.usage_repository = UsageRepository(str(tmp_path / "gateway.db"))
        prompt_service = PromptService(str(tmp_path / "gateway.db"))
        prompt_service.create_version(PromptVersionCreate(id="audit-prompt", template="You are {{ role }}."))
        client.app.state.prompt_service = prompt_service
        client.app.state.rate_limiter = InMemoryModelRateLimiter(RateLimitSettings(default_requests_per_minute=0))
        client.app.state.generate_retry_settings = RetrySettings(max_retries=0, initial_delay_seconds=0)

        success = client.post(
            "/v1/generate",
            json={
                "model": "success-model", "user": "hello",
                "prompt": {"id": "audit-prompt", "variables": {"role": "reviewer"}},
            },
        )
        failure = client.post("/v1/generate", json={"model": "failed-model", "user": "hello"})
        records = client.get("/admin/usage", params={"limit": 10}).json()

    assert success.status_code == 200
    assert failure.status_code == 502
    assert failure.json()["error"]["type"] == "upstream_error"
    assert {record["status"] for record in records} == {"completed", "failed"}
    failed_record = next(record for record in records if record["status"] == "failed")
    completed_record = next(record for record in records if record["status"] == "completed")
    assert failed_record["retry_count"] == 0
    assert failed_record["error_code"] == "upstream_error"
    assert completed_record["prompt_id"] == "audit-prompt"
    assert completed_record["prompt_version"] == 1


def test_usage_repository_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    repository = UsageRepository(str(tmp_path / "gateway.db"))

    def fail(_: UsageRecord) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(repository, "record", fail)
    repository.record_safely(_record("req-safe"))
