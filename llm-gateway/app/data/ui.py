"""Local management UI request and response contracts."""
from pydantic import BaseModel, Field


class RuntimeKeyUpdate(BaseModel):
    """本地页面提交的 DeepSeek Key；该对象绝不用于日志或持久化。"""

    api_key: str | None = Field(default=None, min_length=1)
    use_environment: bool = False


class RuntimeKeyStatus(BaseModel):
    """运行时 Key 状态；绝不包含 Key 本身或其长度。"""

    source: str
    configured: bool


class RuntimeKeyEnvironmentDisplay(RuntimeKeyStatus):
    """用户主动从本地环境读取后，才返回给本地调试页的只读 Key。"""

    value: str
