"""Prompt-template contracts used by the gateway request layer."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


PromptPosition = Literal["system", "prepend_messages", "append_messages"]
PromptRole = Literal["system", "developer", "user", "assistant"]


class PromptReference(BaseModel):
    """调用模型时引用的提示词版本与模板变量。"""

    id: str = Field(min_length=1, max_length=128)
    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)
    position: PromptPosition = "system"


class PromptVersionCreate(BaseModel):
    """创建一个新提示词，或为已有提示词创建递增版本。"""

    id: str | None = Field(default=None, min_length=1, max_length=128)
    template: str = Field(min_length=1)
    role: PromptRole = "system"
    activate: bool = True


class PromptActivation(BaseModel):
    """切换指定提示词的活动版本。"""

    version: int = Field(ge=1)


class PromptRenderRequest(BaseModel):
    """提示词预览所需的版本与 Jinja2 变量。"""

    version: int | None = Field(default=None, ge=1)
    variables: dict[str, Any] = Field(default_factory=dict)


class PromptTemplate(BaseModel):
    """已持久化的一条不可变提示词版本记录。"""

    id: str
    version: int
    template: str
    role: PromptRole
    active: bool
    created_at: datetime


class PromptUse(BaseModel):
    """一次模型调用实际使用的提示词元数据，不包含敏感模板或变量。"""

    id: str
    version: int
