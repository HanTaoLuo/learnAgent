import pytest
import asyncio
from fastapi.testclient import TestClient

from app.adapters import AnthropicAdapter, ModelRegistry, OpenAIAdapter, StructuredOutputError, UpstreamError
from app.errors import RateLimitError
from app.config import DEEPSEEK_API_KEY_ENV, StreamSettings, resolve_api_key
from app.data import ModelCapability, ModelMessage, ModelRequest, ModelResult, ModelStreamEvent, ResponseFormat
from app.main import app, encode_sse, sse_events
from app.services.retry import RetrySettings, generate_with_retry
from app.services.rate_limit import InMemoryModelRateLimiter, RateLimitSettings, client_identity
from app.services.structured_output import validate_result
from app.data.metrics import GatewayMetrics as DirectGatewayMetrics
from app.data.stream import ModelStreamEvent as DirectModelStreamEvent


def test_data_package_exports_split_metrics_and_stream_contracts() -> None:
    from app.data import GatewayMetrics, ModelStreamEvent

    assert GatewayMetrics is DirectGatewayMetrics
    assert ModelStreamEvent is DirectModelStreamEvent


def test_model_message_keeps_role_and_content_for_future_multi_turn_support() -> None:
    message = ModelMessage(role="user", content="Hello")
    assert message.role == "user"
    assert message.content == "Hello"


def test_model_request_uses_system_and_user() -> None:
    request = ModelRequest(
        model="deepseek-v4-flash",
        system="Answer concisely.",
        user="Hello",
    )
    assert request.system == "Answer concisely."
    assert request.user == "Hello"


def test_model_request_normalizes_multi_turn_history_and_latest_user() -> None:
    request = ModelRequest(
        model="deepseek-v4-flash",
        messages=[ModelMessage(role="user", content="第一问"), ModelMessage(role="assistant", content="第一答")],
        user="第二问",
    )
    assert [(message.role, message.content) for message in request.conversation()] == [
        ("user", "第一问"),
        ("assistant", "第一答"),
        ("user", "第二问"),
    ]


def test_model_request_requires_user_or_messages() -> None:
    with pytest.raises(ValueError, match="provide a user message"):
        ModelRequest(model="deepseek-v4-flash")
    with pytest.raises(ValueError, match="provide a user message"):
        ModelRequest(model="deepseek-v4-flash", messages=[ModelMessage(role="system", content="仅系统消息")])


def test_openai_and_anthropic_keys_prefer_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    assert resolve_api_key("OPENAI_API_KEY") == "openai-key"
    assert resolve_api_key("ANTHROPIC_API_KEY") == "anthropic-key"


def test_openai_and_anthropic_keys_fallback_to_deepseek_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert resolve_api_key("OPENAI_API_KEY") == "deepseek-key"
    assert resolve_api_key("ANTHROPIC_API_KEY") == "deepseek-key"


def test_response_format_requires_name_and_schema_for_json_schema() -> None:
    response_format = ResponseFormat(
        type="json_schema",
        name="person",
        json_schema={"type": "object"},
    )
    assert response_format.strict is True
    with pytest.raises(ValueError, match="requires both name and json_schema"):
        ResponseFormat(type="json_schema")
    with pytest.raises(ValueError, match="invalid JSON Schema"):
        ResponseFormat(type="json_schema", name="broken", json_schema={"type": "not-a-type"})


def test_openai_adapter_maps_multi_turn_and_native_response_format() -> None:
    adapter = OpenAIAdapter()
    adapter.api_key = "test-key"
    request = ModelRequest(
        model="gpt-test",
        system="保持简洁",
        messages=[ModelMessage(role="user", content="第一问"), ModelMessage(role="assistant", content="第一答")],
        user="第二问",
        response_format=ResponseFormat(type="json_schema", name="answer", json_schema={"type": "object"}),
    )
    payload = adapter._request_payload(request)
    assert payload["instructions"] == "保持简洁"
    assert payload["input"] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "user", "content": "第二问"},
    ]
    assert payload["text"]["format"]["schema"] == {"type": "object"}


def test_anthropic_adapter_maps_multi_turn_and_native_response_format() -> None:
    adapter = AnthropicAdapter()
    adapter.api_key = "test-key"
    request = ModelRequest(
        model="claude-test",
        system="保持简洁",
        messages=[ModelMessage(role="developer", content="不要使用 Markdown"), ModelMessage(role="user", content="第一问")],
        user="第二问",
        response_format=ResponseFormat(type="json_schema", name="answer", json_schema={"type": "object"}),
    )
    payload = adapter._request_payload(request)
    assert payload["system"] == "保持简洁\n\n不要使用 Markdown"
    assert payload["messages"] == [
        {"role": "user", "content": "第一问"},
        {"role": "user", "content": "第二问"},
    ]
    assert payload["output_config"]["format"]["schema"] == {"type": "object"}


def test_prompt_only_adapter_adds_response_format_instruction() -> None:
    adapter = OpenAIAdapter(api_key_env=DEEPSEEK_API_KEY_ENV, fallback_api_key_env=None, structured_output="prompt_only")
    adapter.api_key = "test-key"
    payload = adapter._request_payload(
        ModelRequest(model="deepseek-v4-pro", user="提取姓名", response_format=ResponseFormat(type="json_object"))
    )
    assert "valid JSON object" in payload["instructions"]
    assert "text" not in payload


def test_anthropic_adapter_normalizes_response() -> None:
    output = AnthropicAdapter._to_model_result({"id": "msg_1", "stop_reason": "end_turn", "content": [{"type": "text", "text": "你好"}], "usage": {"input_tokens": 3, "output_tokens": 2}}, "deepseek-v4-flash")
    assert output.output[0]["content"][0]["text"] == "你好"
    assert output.usage["total_tokens"] == 5


def test_anthropic_adapter_normalizes_text_delta_event() -> None:
    event = AnthropicAdapter._to_stream_event(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}},
        "deepseek-v4-flash",
    )
    assert event is not None
    assert event.type == "response.output_text.delta"
    assert event.data["delta"] == "你好"


def test_structured_output_result_is_validated_locally() -> None:
    result = ModelResult(
        id="resp_1",
        model="test",
        output=[{"content": [{"type": "output_text", "text": '{"name":"Ada"}'}]}],
    )
    assert validate_result(result, ResponseFormat(type="json_schema", name="person", json_schema={"type": "object", "required": ["name"]})) == {"name": "Ada"}
    with pytest.raises(StructuredOutputError, match="does not match JSON Schema"):
        validate_result(result, ResponseFormat(type="json_schema", name="person", json_schema={"type": "object", "required": ["age"]}))


def test_model_registry_registers_capability_and_rejects_unsupported_top_p() -> None:
    adapter = OpenAIAdapter()
    registry = ModelRegistry()
    capabilities = ModelCapability(True, True, "json_mode", True, True, False)
    registry.register("example-model", adapter, capabilities)
    registered = registry.get("example-model")
    assert registered is not None
    assert registered.adapter is adapter
    assert registered.capabilities == capabilities
    with pytest.raises(ValueError, match="does not support top_p"):
        registry.validate_request(ModelRequest(model="example-model", user="Hi", top_p=0.5), capabilities)


def test_model_registry_filters_unsupported_structured_and_streaming_parameters() -> None:
    capabilities = ModelCapability(True, True, "unsupported", False, True, True, streaming=False)
    with pytest.raises(ValueError, match="does not support structured output"):
        ModelRegistry.validate_request(
            ModelRequest(model="example-model", user="Hi", response_format=ResponseFormat(type="json_object")), capabilities
        )
    with pytest.raises(ValueError, match="does not support streaming"):
        ModelRegistry.validate_request(ModelRequest(model="example-model", user="Hi", stream=True), capabilities)


def test_encode_sse_uses_event_and_json_data_lines() -> None:
    encoded = encode_sse(ModelStreamEvent(type="response.output_text.delta", data={"delta": "你好"}))
    assert encoded == 'event: response.output_text.delta\ndata: {"delta":"你好"}\n\n'.encode()


def test_http_errors_use_the_gateway_error_envelope() -> None:
    with TestClient(app) as client:
        unknown_model = client.post("/v1/generate", json={"model": "missing-model", "user": "hello"})
        invalid_body = client.post("/v1/generate", json={"model": "deepseek-v4-pro"})
        missing_path = client.get("/missing-path")

    assert unknown_model.status_code == 400
    assert unknown_model.json() == {
        "error": {
            "message": "Unsupported model: missing-model",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    assert invalid_body.status_code == 400
    assert invalid_body.json()["error"]["type"] == "invalid_request_error"
    assert invalid_body.json()["error"]["code"] == "validation_error"
    assert missing_path.status_code == 404
    assert missing_path.json()["error"]["code"] == "http_error"


def test_http_model_rate_limit_returns_429_and_retry_after() -> None:
    with TestClient(app) as client:
        limiter = InMemoryModelRateLimiter(RateLimitSettings(default_requests_per_minute=1))
        client.app.state.rate_limiter = limiter
        identity = client_identity("Bearer caller-a", None, None)
        asyncio.run(limiter.check(identity, "deepseek-v4-pro"))
        response = client.post(
            "/v1/generate",
            json={"model": "deepseek-v4-pro", "user": "hello"},
            headers={"Authorization": "Bearer caller-a"},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json()["error"]["type"] == "rate_limit_error"
    assert response.json()["error"]["code"] == "model_rate_limit_exceeded"


async def test_rate_limit_is_isolated_by_caller_and_model() -> None:
    limiter = InMemoryModelRateLimiter(RateLimitSettings(default_requests_per_minute=1))
    await limiter.check("caller-a", "deepseek-v4-pro")
    await limiter.check("caller-a", "deepseek-v4-flash")
    await limiter.check("caller-b", "deepseek-v4-pro")
    with pytest.raises(RateLimitError, match="Rate limit exceeded"):
        await limiter.check("caller-a", "deepseek-v4-pro")


async def test_non_stream_generate_retries_transient_upstream_errors() -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise UpstreamError(502, "temporary failure")
        return "ok"

    result, retry_count = await generate_with_retry(
        operation, settings=RetrySettings(max_retries=3, initial_delay_seconds=0)
    )
    assert result == "ok"
    assert attempts == 3
    assert retry_count == 2


async def test_non_stream_generate_does_not_retry_invalid_or_structured_errors() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise StructuredOutputError("not valid JSON")

    with pytest.raises(StructuredOutputError):
        await generate_with_retry(operation, settings=RetrySettings(max_retries=3, initial_delay_seconds=0))
    assert attempts == 1


async def test_sse_structured_output_sends_completed_only_after_validation() -> None:
    async def upstream_events():
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": '{"name":"Ada"}'})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    frames = [
        frame
        async for frame in sse_events(
            upstream_events(),
            ResponseFormat(type="json_schema", name="person", json_schema={"type": "object", "required": ["name"]}),
        )
    ]
    assert b"response.output_text.delta" in frames[0]
    assert b"response.completed" in frames[1]


async def test_sse_created_event_exposes_gateway_request_id_for_history_query() -> None:
    async def upstream_events():
        yield ModelStreamEvent(type="response.created", data={"type": "response.created"})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    frames = [frame async for frame in sse_events(upstream_events(), ResponseFormat(), request_id="req_history", model="test")]
    assert b'"gateway_request_id":"req_history"' in frames[0]


async def test_sse_structured_output_sends_failed_when_final_json_is_invalid() -> None:
    async def upstream_events():
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "not json"})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    frames = [frame async for frame in sse_events(upstream_events(), ResponseFormat(type="json_object"))]
    assert b"response.output_text.delta" in frames[0]
    assert b"response.failed" in frames[1]
    assert b"response.completed" not in frames[1]
    assert b"gateway_metrics" in frames[1]


async def test_sse_retries_only_before_first_text_delta() -> None:
    attempts = 0

    async def failed_before_output():
        raise UpstreamError(502, "temporary upstream failure")
        yield  # pragma: no cover

    async def successful_stream():
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "ok"})
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    def factory():
        nonlocal attempts
        attempts += 1
        return successful_stream() if attempts > 1 else failed_before_output()

    frames = [
        frame async for frame in sse_events(
            factory(), ResponseFormat(), event_factory=factory,
            settings=StreamSettings(max_retries=1, heartbeat_interval_seconds=1), model="test",
        )
    ]
    assert attempts == 2
    assert b"response.completed" in frames[-1]
    assert b"gateway_metrics" in frames[-1]


async def test_sse_does_not_retry_after_text_has_been_emitted() -> None:
    attempts = 0

    async def interrupted_stream():
        yield ModelStreamEvent(type="response.output_text.delta", data={"delta": "partial"})
        raise UpstreamError(502, "connection lost")

    def factory():
        nonlocal attempts
        attempts += 1
        return interrupted_stream()

    frames = [frame async for frame in sse_events(factory(), ResponseFormat(), event_factory=factory, model="test")]
    assert attempts == 1
    assert b"response.output_text.delta" in frames[0]
    assert b"response.failed" in frames[-1]
    assert b"gateway_metrics" in frames[-1]


async def test_sse_sends_heartbeat_while_waiting_for_upstream_event() -> None:
    async def slow_stream():
        await asyncio.sleep(0.02)
        yield ModelStreamEvent(type="response.completed", data={"type": "response.completed"})

    frames = [
        frame async for frame in sse_events(
            slow_stream(), ResponseFormat(),
            settings=StreamSettings(first_token_timeout_seconds=1, idle_timeout_seconds=1, heartbeat_interval_seconds=0.005),
            model="test",
        )
    ]
    assert b": ping" in frames[0]
    assert b"response.completed" in frames[-1]


async def test_sse_disconnect_closes_upstream_iterator_without_terminal_event() -> None:
    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    class ClosableUpstream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> ModelStreamEvent:
            return ModelStreamEvent(type="response.created", data={"type": "response.created"})

        async def aclose(self) -> None:
            self.closed = True

    upstream = ClosableUpstream()
    frames = [
        frame async for frame in sse_events(
            upstream, ResponseFormat(), client_request=DisconnectedRequest(), model="test"  # type: ignore[arg-type]
        )
    ]
    assert frames == []
    assert upstream.closed is True
