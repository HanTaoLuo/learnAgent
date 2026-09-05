"""Provider-neutral request, response, and capability contracts."""
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.data.metrics import GatewayMetrics
from app.data.prompt import PromptReference

StructuredOutputMode = Literal["native_schema", "json_mode", "prompt_only", "unsupported"]


class ModelMessage(BaseModel):
    """统一对话上下文中的单条消息。"""

    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    content: str | list[dict[str, Any]]


class ResponseFormat(BaseModel):
    """调用方期望的模型输出格式与可选 JSON Schema 约束。"""

    type: Literal["text", "json_object", "json_schema"] = "text"
    name: str | None = Field(default=None, description="JSON Schema 的名称")
    json_schema: dict[str, Any] | None = Field(default=None, description="JSON Schema 定义")
    strict: bool = True

    @model_validator(mode="after")
    def validate_json_schema(self) -> "ResponseFormat":
        """确保 JSON Schema 模式包含名称和 Schema 内容。"""
        if self.type == "json_schema" and (not self.name or self.json_schema is None):
            raise ValueError("json_schema response format requires both name and json_schema")
        if self.type == "json_schema":
            try:
                Draft202012Validator.check_schema(self.json_schema)
            except SchemaError as exc:
                raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return self


class ModelRequest(BaseModel):
    """网关与所有协议适配器之间使用的统一模型请求。

    ``user`` 用于简单的单轮调用；``messages`` 用于传入已有对话历史。两者可以同时
    提供，此时 ``user`` 作为最后一条用户消息追加到 ``messages`` 后。
    """

    model_config = ConfigDict(extra="forbid")
    system: str | None = Field(default=None, description="系统指令、角色设定或输出要求")
    user: str | None = Field(default=None, description="本次用户输入；会追加到 messages 末尾")
    messages: list[ModelMessage] = Field(default_factory=list, description="多轮对话历史")
    model: str = Field(description="Gateway model alias")
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=1)
    top_p: float | None = Field(default=None, ge=0, le=1)
    response_format: ResponseFormat = Field(default_factory=ResponseFormat)
    prompt: PromptReference | None = Field(default=None, description="可选的已版本化提示词引用")
    stream: bool = False

    @model_validator(mode="after")
    def validate_conversation(self) -> "ModelRequest":
        """确保请求携带用户输入，并限制系统消息为文本以兼容 Anthropic。"""
        if self.user is None and not any(message.role == "user" for message in self.messages):
            raise ValueError("provide a user message through user or messages")
        for message in self.messages:
            if message.role in {"system", "developer"} and not isinstance(message.content, str):
                raise ValueError("system and developer message content must be text")
        return self

    def conversation(self) -> list[ModelMessage]:
        """返回完整对话；简单 user 输入会标准化为最后一条 user 消息。"""
        messages = list(self.messages)
        if self.user is not None:
            messages.append(ModelMessage(role="user", content=self.user))
        return messages


class ModelResult(BaseModel):
    """所有协议适配器都必须返回的统一 Responses 风格结果。"""

    model_config = ConfigDict(extra="allow")
    id: str
    object: str = "response"
    status: str = "completed"
    model: str
    output: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    metrics: GatewayMetrics | None = None


@dataclass(frozen=True)
class ModelCapability:
    """Immutable declaration of a model's supported gateway features."""

    chat_completions: bool
    responses: bool
    structured_output: StructuredOutputMode
    tool_calling: bool
    supports_temperature: bool
    supports_top_p: bool
    streaming: bool = False
