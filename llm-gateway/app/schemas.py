from pydantic import BaseModel


class ErrorBody(BaseModel):
    """对外暴露的错误详情。"""

    message: str
    type: str
    code: str | None = None


class ErrorResponse(BaseModel):
    """网关错误响应的统一包装格式。"""

    error: ErrorBody
