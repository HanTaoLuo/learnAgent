"""Gateway-wide errors shared by routes, services, and adapters."""


class GatewayError(Exception):
    """带有稳定 HTTP 状态、类型和业务码的网关异常。"""

    def __init__(self, status_code: int, message: str, *, error_type: str, code: str) -> None:
        self.status_code = status_code
        self.message = message
        self.error_type = error_type
        self.code = code
        super().__init__(message)


class InvalidRequestError(GatewayError):
    """调用方请求格式、模型或能力不符合网关契约。"""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(400, message, error_type="invalid_request_error", code=code)


class ResourceNotFoundError(GatewayError):
    """网关管理资源不存在。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(404, message, error_type="not_found_error", code=code)


class RateLimitError(GatewayError):
    """调用方在指定模型上的额度已耗尽。"""

    def __init__(self, model: str, retry_after_seconds: int) -> None:
        super().__init__(
            429,
            f"Rate limit exceeded for model: {model}",
            error_type="rate_limit_error",
            code="model_rate_limit_exceeded",
        )
        self.headers = {"Retry-After": str(retry_after_seconds)}


class UpstreamError(GatewayError):
    """上游 SDK、协议或返回内容产生的统一网关错误。"""

    def __init__(self, status_code: int, message: str, *, code: str = "upstream_error") -> None:
        super().__init__(status_code, message, error_type="upstream_error", code=code)


class StructuredOutputError(UpstreamError):
    """上游响应无法满足调用方要求的 JSON 输出格式。"""

    def __init__(self, message: str) -> None:
        super().__init__(422, message, code="structured_output_invalid")
        self.error_type = "structured_output_error"
