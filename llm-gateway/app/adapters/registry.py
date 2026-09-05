"""Registry that binds gateway model aliases to adapters and capabilities."""
from dataclasses import dataclass

from app.adapters.base.model_adapter import ModelAdapter
from app.data import ModelCapability, ModelRequest


@dataclass(frozen=True)
class RegisteredModel:
    """一个模型别名对应的协议适配器与能力声明。"""

    adapter: ModelAdapter
    capabilities: ModelCapability


class ModelRegistry:
    """维护模型别名到适配器和能力配置的运行时映射。"""

    def __init__(self) -> None:
        """创建空注册表；应用启动阶段负责填充模型配置。"""
        self._models: dict[str, RegisteredModel] = {}

    def register(self, model: str, adapter: ModelAdapter, capabilities: ModelCapability) -> None:
        """注册一个模型别名；重复注册会抛出 ValueError，防止配置被覆盖。"""
        if model in self._models:
            raise ValueError(f"Model already registered: {model}")
        self._models[model] = RegisteredModel(adapter=adapter, capabilities=capabilities)

    def get(self, model: str) -> RegisteredModel | None:
        """根据调用方传入的模型别名返回注册信息；未注册时返回 None。"""
        return self._models.get(model)

    def items(self) -> list[tuple[str, RegisteredModel]]:
        """返回已注册模型快照，供本地管理界面只读展示。"""
        return list(self._models.items())

    @staticmethod
    def validate_request(request: ModelRequest, capabilities: ModelCapability) -> None:
        """拒绝目标模型不支持的统一请求字段，避免无声丢弃参数。"""
        if request.messages and not capabilities.chat_completions:
            raise ValueError(f"{request.model} does not support multi-turn chat")
        if request.temperature is not None and not capabilities.supports_temperature:
            raise ValueError(f"{request.model} does not support temperature")
        if request.top_p is not None and not capabilities.supports_top_p:
            raise ValueError(f"{request.model} does not support top_p")
        if request.stream and not capabilities.streaming:
            raise ValueError(f"{request.model} does not support streaming")
        if request.response_format.type != "text":
            if capabilities.structured_output == "unsupported":
                raise ValueError(f"{request.model} does not support structured output")
            if request.response_format.type == "json_schema" and capabilities.structured_output == "json_mode":
                raise ValueError(f"{request.model} supports JSON mode but not JSON Schema output")
