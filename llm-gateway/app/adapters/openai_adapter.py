"""Adapter for any provider exposing the OpenAI Responses API."""
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.adapters.base.model_adapter import ModelAdapter
from app.config import DEEPSEEK_API_KEY_ENV, OPENAI_API_KEY_ENV
from app.data import ModelRequest, ModelResult, ModelStreamEvent, ResponseFormat, StructuredOutputMode
from app.services.structured_output import prompt_instruction, validate_result


class OpenAIAdapter(ModelAdapter):
    """调用兼容 OpenAI Responses API 的模型服务。"""

    def __init__(
        self,
        *,
        api_key_env: str = OPENAI_API_KEY_ENV,
        fallback_api_key_env: str | None = DEEPSEEK_API_KEY_ENV,
        base_url_env: str = "OPENAI_BASE_URL",
        default_base_url: str = "https://api.openai.com/v1",
        structured_output: StructuredOutputMode = "native_schema",
    ) -> None:
        """按注册模型的服务商配置创建 OpenAI 协议客户端。

        OpenAI 模型优先使用 ``OPENAI_API_KEY``，未设置时回退到
        ``DEEPSEEK_API_KEY``。DeepSeek 模型注册时会显式指定自己的 Key 与端点。
        """
        super().__init__(
            api_key_env=api_key_env, fallback_api_key_env=fallback_api_key_env,
            base_url_env=base_url_env, default_base_url=default_base_url,
            structured_output=structured_output,
        )
        self.refresh_client()

    def refresh_client(self) -> None:
        """使用当前运行时 Key 重建 OpenAI SDK client。"""
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    async def generate(self, request: ModelRequest) -> ModelResult:
        """把统一请求透传为 Responses 请求，并校验标准化响应。"""
        payload = self._request_payload(request)
        with self.translate_sdk_errors("Responses API request"):
            response = await self.client.responses.create(**payload)
        result = ModelResult.model_validate(response.model_dump(mode="json"))
        validate_result(result, request.response_format)
        return result

    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """调用 Responses SSE，并将每个 SDK 事件转换为统一流事件。"""
        with self.translate_sdk_errors("Responses stream"):
            stream = await self.client.responses.create(**self._request_payload(request), stream=True)
            async for event in stream:
                yield ModelStreamEvent(type=event.type, data=event.model_dump(mode="json"))

    def _request_payload(self, request: ModelRequest) -> dict[str, object]:
        """将统一对话及结构化输出要求映射为 Responses 请求。"""
        self.require_api_key()
        payload: dict[str, object] = {
            "model": request.model,
            "input": [message.model_dump(mode="json") for message in request.conversation()],
        }
        instructions = [request.system] if request.system else []
        if self.structured_output == "prompt_only":
            instruction = prompt_instruction(request.response_format)
            if instruction:
                instructions.append(instruction)
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        payload.update(self._response_format_payload(request.response_format))
        return payload

    def _response_format_payload(self, response_format: ResponseFormat) -> dict[str, object]:
        """将统一 response_format 映射为 Responses 的 ``text.format``。"""
        if response_format.type == "text" or self.structured_output == "prompt_only":
            return {}
        if self.structured_output == "unsupported":
            return {}
        if response_format.type == "json_object":
            return {"text": {"format": {"type": "json_object"}}}
        if response_format.type == "json_schema" and self.structured_output == "native_schema":
            return {
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": response_format.name,
                        "schema": response_format.json_schema,
                        "strict": response_format.strict,
                    }
                }
            }
        return {}
