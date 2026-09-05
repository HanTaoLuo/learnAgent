"""Adapter for any provider exposing the Anthropic Messages API."""
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from anthropic import AsyncAnthropic

from app.adapters.base.model_adapter import ModelAdapter
from app.config import ANTHROPIC_API_KEY_ENV, DEEPSEEK_API_KEY_ENV
from app.data import ModelRequest, ModelResult, ModelStreamEvent, ResponseFormat, StructuredOutputMode
from app.services.structured_output import prompt_instruction, validate_result


class AnthropicAdapter(ModelAdapter):
    """调用兼容 Anthropic Messages API 的模型服务。"""

    def __init__(
        self,
        *,
        api_key_env: str = ANTHROPIC_API_KEY_ENV,
        fallback_api_key_env: str | None = DEEPSEEK_API_KEY_ENV,
        base_url_env: str = "ANTHROPIC_BASE_URL",
        default_base_url: str = "https://api.anthropic.com",
        structured_output: StructuredOutputMode = "native_schema",
    ) -> None:
        """按注册模型的服务商配置创建 Anthropic 协议客户端。

        Anthropic 模型优先使用 ``ANTHROPIC_API_KEY``，未设置时回退到
        ``DEEPSEEK_API_KEY``。DeepSeek 模型注册时会显式指定自己的 Key 与端点。
        """
        super().__init__(
            api_key_env=api_key_env, fallback_api_key_env=fallback_api_key_env,
            base_url_env=base_url_env, default_base_url=default_base_url,
            structured_output=structured_output,
        )
        self.refresh_client()

    def refresh_client(self) -> None:
        """使用当前运行时 Key 重建 Anthropic SDK client。"""
        self.client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    async def generate(self, request: ModelRequest) -> ModelResult:
        """将统一请求转换为 Messages 请求，再规范化为 ModelResult。"""
        payload = self._request_payload(request)
        with self.translate_sdk_errors("Messages API request"):
            response = await self.client.messages.create(**payload)
        result = self._to_model_result(response.model_dump(mode="json"), request.model)
        validate_result(result, request.response_format)
        return result

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """调用 Messages SSE，并转换文本增量与生命周期事件。"""
        with self.translate_sdk_errors("Messages stream"):
            stream = await self.client.messages.create(**self._request_payload(request), stream=True)
            async for event in stream:
                normalized = self._to_stream_event(event.model_dump(mode="json"), request.model)
                if normalized is not None:
                    yield normalized

    def _request_payload(self, request: ModelRequest) -> dict[str, Any]:
        """将统一对话及结构化输出要求映射为 Anthropic Messages 请求。"""
        self.require_api_key()
        system, messages = self._split_conversation(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or 1024,
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        payload.update(self._response_format_payload(request.response_format))
        return payload

    def _split_conversation(self, request: ModelRequest) -> tuple[str | None, list[dict[str, Any]]]:
        """将统一角色拆成 Anthropic 的 system 与 user/assistant messages。"""
        system_parts = [request.system] if request.system else []
        messages: list[dict[str, Any]] = []
        for message in request.conversation():
            if message.role in {"system", "developer"}:
                system_parts.append(message.content)
            else:
                messages.append({"role": message.role, "content": message.content})
        if self.structured_output == "prompt_only":
            instruction = prompt_instruction(request.response_format)
            if instruction:
                system_parts.append(instruction)
        return "\n\n".join(system_parts) or None, messages

    def _response_format_payload(self, response_format: ResponseFormat) -> dict[str, Any]:
        """将统一 response_format 映射为 Anthropic 的 ``output_config.format``。"""
        if response_format.type == "text" or self.structured_output == "prompt_only":
            return {}
        if self.structured_output == "unsupported":
            return {}
        if response_format.type == "json_object":
            return {"output_config": {"format": {"type": "json_schema", "schema": {"type": "object"}}}}
        if response_format.type == "json_schema" and self.structured_output == "native_schema":
            return {"output_config": {"format": {"type": "json_schema", "schema": response_format.json_schema}}}
        return {}

    @staticmethod
    def _to_stream_event(data: dict[str, Any], model: str) -> ModelStreamEvent | None:
        """将 Anthropic 的关键事件映射为 Responses 风格的统一 SSE 事件。"""
        event_type = data.get("type")
        if event_type == "message_start":
            return ModelStreamEvent(type="response.created", data={"type": "response.created", "model": model, "usage": data.get("message", {}).get("usage", {})})
        if event_type == "content_block_delta" and data.get("delta", {}).get("type") == "text_delta":
            return ModelStreamEvent(
                type="response.output_text.delta",
                data={"type": "response.output_text.delta", "delta": data["delta"].get("text", "")},
            )
        if event_type == "message_delta":
            return ModelStreamEvent(
                type="response.metadata",
                data={"type": "response.metadata", "usage": data.get("usage", {}), "stop_reason": data.get("delta", {}).get("stop_reason")},
            )
        if event_type == "message_stop":
            return ModelStreamEvent(type="response.completed", data={"type": "response.completed", "model": model})
        # 暂未定义跨协议语义的事件不丢弃，保留给客户端和后续工具/推理扩展使用。
        return ModelStreamEvent(type="response.metadata", data={"type": "response.metadata", "upstream_type": event_type, "data": data})

    @staticmethod
    def _to_model_result(data: dict[str, Any], model: str) -> ModelResult:
        """将 Anthropic 的 content、usage 转换为统一 Responses 风格结果。"""
        text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        usage = data.get("usage", {})
        return ModelResult(
            id=data.get("id", f"resp_{uuid4().hex}"),
            object="response",
            status="completed" if data.get("stop_reason") else "in_progress",
            model=model,
            output=[{"id": f"msg_{uuid4().hex}", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            usage={"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)},
        )
