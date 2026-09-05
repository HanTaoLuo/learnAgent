"""读取网关的环境变量配置。"""
import os
from dataclasses import dataclass

from app.services.retry import RetrySettings
from app.services.rate_limit import RateLimitSettings


DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


def resolve_api_key(primary_env: str, fallback_env: str | None = DEEPSEEK_API_KEY_ENV) -> str:
    """读取首选 Key；首选 Key 缺失时可回退到 DeepSeek Key。"""
    primary_key = os.getenv(primary_env, "")
    if primary_key:
        return primary_key
    if fallback_env:
        return os.getenv(fallback_env, "")
    return ""


@dataclass(frozen=True)
class StreamSettings:
    """SSE 生命周期配置，均可由环境变量覆盖。"""

    first_token_timeout_seconds: float = 30.0
    idle_timeout_seconds: float = 60.0
    max_retries: int = 3
    heartbeat_interval_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "StreamSettings":
        """读取 SSE 配置。"""
        return cls(
            first_token_timeout_seconds=float(os.getenv("STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", "30")),
            idle_timeout_seconds=float(os.getenv("STREAM_IDLE_TIMEOUT_SECONDS", "60")),
            max_retries=int(os.getenv("STREAM_MAX_RETRIES", "3")),
            heartbeat_interval_seconds=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "15")),
        )


@dataclass(frozen=True)
class StreamRunSettings:
    """断线可重连的内存 Run 配置。"""

    disconnect_grace_seconds: float = 60.0
    retention_seconds: float = 600.0
    cleanup_interval_seconds: float = 1.0
    max_cached_runs: int = 100
    default_max_output_tokens: int = 1024

    @classmethod
    def from_env(cls) -> "StreamRunSettings":
        """读取后台流、缓存回收和默认输出上限配置。"""
        return cls(
            disconnect_grace_seconds=float(os.getenv("STREAM_DISCONNECT_GRACE_SECONDS", "60")),
            retention_seconds=float(os.getenv("STREAM_RUN_RETENTION_SECONDS", "600")),
            cleanup_interval_seconds=float(os.getenv("STREAM_RUN_CLEANUP_INTERVAL_SECONDS", "1")),
            max_cached_runs=int(os.getenv("STREAM_RUN_MAX_CACHED_RUNS", "100")),
            default_max_output_tokens=int(os.getenv("STREAM_DEFAULT_MAX_OUTPUT_TOKENS", "1024")),
        )


def generate_retry_settings_from_env() -> RetrySettings:
    """读取非流式生成重试配置，并限制最大重试次数为三次。"""
    return RetrySettings(
        max_retries=min(3, max(0, int(os.getenv("GENERATE_MAX_RETRIES", "3")))),
        initial_delay_seconds=float(os.getenv("GENERATE_RETRY_INITIAL_DELAY_SECONDS", "0.25")),
    )


def rate_limit_settings_from_env() -> RateLimitSettings:
    """读取默认与模型专属 RPM 配置，0 表示该模型不限制。"""
    configured_models: dict[str, int] = {}
    for item in os.getenv("MODEL_RATE_LIMITS_PER_MINUTE", "").split(","):
        if not item.strip():
            continue
        model, separator, limit = item.partition(":")
        if not separator or not model.strip():
            raise ValueError("MODEL_RATE_LIMITS_PER_MINUTE must use model:requests_per_minute entries")
        configured_models[model.strip()] = int(limit)
    return RateLimitSettings(
        default_requests_per_minute=int(os.getenv("MODEL_RATE_LIMIT_REQUESTS_PER_MINUTE", "60")),
        model_requests_per_minute=configured_models,
    )


def gateway_database_path_from_env() -> str:
    """返回网关管理数据（当前为提示词版本）使用的 SQLite 文件路径。"""
    return os.getenv("GATEWAY_DATABASE_PATH", "./llm_gateway.db")


@dataclass(frozen=True)
class UiSettings:
    """本地调试页面与浏览器自动打开配置。"""

    auto_open: bool = True
    url: str = "http://127.0.0.1:8000/"
    typewriter_interval_ms: int = 35
    typewriter_chars_per_tick: int = 1

    @classmethod
    def from_env(cls) -> "UiSettings":
        return cls(
            auto_open=os.getenv("GATEWAY_UI_AUTO_OPEN", "true").lower() in {"1", "true", "yes"},
            url=os.getenv("GATEWAY_UI_URL", "http://127.0.0.1:8000/"),
            typewriter_interval_ms=max(1, int(os.getenv("GATEWAY_UI_TYPEWRITER_INTERVAL_MS", "35"))),
            typewriter_chars_per_tick=max(1, int(os.getenv("GATEWAY_UI_TYPEWRITER_CHARS_PER_TICK", "1"))),
        )
