"""The provider-neutral contract used by the gateway."""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import contextmanager
import os
from collections.abc import Iterator

from app.config import resolve_api_key
from app.data import ModelRequest, ModelResult, ModelStreamEvent, StructuredOutputMode
from app.errors import UpstreamError


class ModelAdapter(ABC):
    """定义所有协议适配器都必须实现的统一调用接口。"""

    def __init__(
        self,
        *,
        api_key_env: str,
        fallback_api_key_env: str | None,
        base_url_env: str,
        default_base_url: str,
        structured_output: StructuredOutputMode,
    ) -> None:
        """解析所有 SDK Adapter 共用的鉴权、地址、超时和能力配置。"""
        self.api_key_env = api_key_env
        self.fallback_api_key_env = fallback_api_key_env
        self.api_key = resolve_api_key(api_key_env, fallback_api_key_env)
        self.base_url = os.getenv(base_url_env, default_base_url)
        self.timeout = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "60"))
        self.structured_output = structured_output

    def require_api_key(self) -> None:
        """在发起上游调用前统一校验可用 Key。"""
        if self.api_key:
            return
        key_names = ", ".join(name for name in (self.api_key_env, self.fallback_api_key_env) if name)
        raise UpstreamError(500, f"No API key configured; set one of: {key_names}", code="api_key_missing")

    def set_runtime_api_key(self, api_key: str) -> None:
        """仅更新当前进程的 Key，并重建由子类管理的 SDK client。"""
        self.api_key = api_key
        self.refresh_client()

    @abstractmethod
    def refresh_client(self) -> None:
        """使用当前 Key、地址和超时设置重建 SDK client。"""

    @contextmanager
    def translate_sdk_errors(self, operation: str) -> Iterator[None]:
        """将不同 SDK 异常统一为网关 UpstreamError。"""
        try:
            yield
        except UpstreamError:
            raise
        except Exception as exc:
            raise UpstreamError(502, f"{operation} failed: {exc}") from exc

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResult:
        """将统一请求发送给上游，并返回标准化的模型结果。"""

    @abstractmethod
    async def generate_stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """将统一请求发送给上游，并逐个产出标准化 SSE 事件。"""
