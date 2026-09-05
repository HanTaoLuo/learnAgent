"""Ephemeral local-debug configuration that never persists secrets."""
import os

from app.adapters import ModelRegistry
from app.config import DEEPSEEK_API_KEY_ENV
from app.data import RuntimeKeyStatus
from app.errors import InvalidRequestError


class RuntimeKeyService:
    """为使用 DeepSeek Key 的已注册 Adapter 提供进程内覆盖。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry
        self._source = "environment"

    def apply(self, api_key: str | None, *, use_environment: bool) -> RuntimeKeyStatus:
        """应用页面 Key 或环境 Key；不写磁盘、不回传 Key。"""
        if use_environment or not api_key:
            api_key = os.getenv(DEEPSEEK_API_KEY_ENV, "")
            source = "environment"
        else:
            source = "runtime"
        if not api_key:
            raise InvalidRequestError("DeepSeek API key is not configured", code="api_key_missing")
        for _, registered in self._registry.items():
            if registered.adapter.api_key_env == DEEPSEEK_API_KEY_ENV:
                registered.adapter.set_runtime_api_key(api_key)
        self._source = source
        return RuntimeKeyStatus(source=source, configured=True)

    def status(self) -> RuntimeKeyStatus:
        """返回安全状态，不暴露当前环境或内存中的 Key。"""
        # 初始页面要回答的是“环境变量是否已配置”，而不是 Adapter 是否已经
        # 创建过 SDK client。这样既不暴露 Key，也不会因延迟初始化而误报未配置。
        if self._source == "environment":
            return RuntimeKeyStatus(source=self._source, configured=bool(os.getenv(DEEPSEEK_API_KEY_ENV)))
        configured = any(
            bool(registered.adapter.api_key)
            for _, registered in self._registry.items()
            if registered.adapter.api_key_env == DEEPSEEK_API_KEY_ENV
        )
        return RuntimeKeyStatus(source=self._source, configured=configured)

    def display_key_for_local_ui(self) -> str:
        """返回本地控制台需展示的 Key；调用方必须先完成本地/HTTPS 校验。

        此方法刻意不写入日志或持久化层。它只服务于用户明确要求的本地只读明文展示，
        不应被普通业务接口调用。
        """
        for _, registered in self._registry.items():
            if registered.adapter.api_key_env == DEEPSEEK_API_KEY_ENV:
                return registered.adapter.api_key
        return ""
