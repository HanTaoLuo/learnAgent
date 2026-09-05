from contextlib import asynccontextmanager
from datetime import UTC, datetime
import asyncio
import hashlib
import time
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.adapters import AnthropicAdapter, ModelRegistry, OpenAIAdapter
from app.config import (
    DEEPSEEK_API_KEY_ENV,
    StreamRunSettings,
    StreamSettings,
    UiSettings,
    gateway_database_path_from_env,
    generate_retry_settings_from_env,
    rate_limit_settings_from_env,
)
from app.data import (
    ModelCapability,
    GatewayMetrics,
    ModelRequest,
    ModelResult,
    PromptActivation,
    PromptReference,
    PromptRenderRequest,
    PromptTemplate,
    PromptVersionCreate,
    PromptUse,
    RuntimeKeyEnvironmentDisplay,
    RuntimeKeyStatus,
    RuntimeKeyUpdate,
    StreamRunSnapshot,
    UsageQuery,
    UsageRecord,
    UsageStatus,
)
from app.errors import GatewayError, InvalidRequestError
from app.schemas import ErrorResponse
from app.services.metrics import build_gateway_metrics
from app.services.retry import generate_with_retry
from app.services.rate_limit import InMemoryModelRateLimiter, client_identity
from app.services.prompts import PromptService
from app.services.streaming import encode_sse, sse_events
from app.services.stream_runs import InMemoryStreamRunManager
from app.services.usage import UsageRepository
from app.services.runtime_config import RuntimeKeyService
from app.services.ui import is_local_or_secure_request, open_browser_safely


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时注册模型、协议适配器及对应能力配置。"""
    registry = ModelRegistry()
    registry.register(
        model="deepseek-v4-pro",
        adapter=OpenAIAdapter(
            api_key_env=DEEPSEEK_API_KEY_ENV,
            fallback_api_key_env=None,
            base_url_env="DEEPSEEK_BASE_URL",
            default_base_url="https://api.deepseek.com",
            structured_output="prompt_only",
        ),
        capabilities=ModelCapability(True, True, "prompt_only", False, True, True, streaming=True),
    )
    registry.register(
        model="deepseek-v4-flash",
        adapter=AnthropicAdapter(
            api_key_env=DEEPSEEK_API_KEY_ENV,
            fallback_api_key_env=None,
            base_url_env="DEEPSEEK_ANTHROPIC_BASE_URL",
            default_base_url="https://api.deepseek.com/anthropic",
            structured_output="prompt_only",
        ),
        capabilities=ModelCapability(True, False, "prompt_only", False, True, True, streaming=True),
    )
    app.state.model_registry = registry
    app.state.stream_settings = StreamSettings.from_env()
    app.state.stream_run_manager = InMemoryStreamRunManager(StreamRunSettings.from_env())
    app.state.generate_retry_settings = generate_retry_settings_from_env()
    app.state.rate_limiter = InMemoryModelRateLimiter(rate_limit_settings_from_env())
    app.state.prompt_service = PromptService(gateway_database_path_from_env())
    app.state.usage_repository = UsageRepository(gateway_database_path_from_env())
    app.state.runtime_key_service = RuntimeKeyService(registry)
    app.state.ui_settings = UiSettings.from_env()
    browser_task = asyncio.create_task(open_browser_safely(app.state.ui_settings))
    try:
        yield
    finally:
        browser_task.cancel()
        await app.state.stream_run_manager.close()


app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def _error_response(error: GatewayError) -> JSONResponse:
    """将所有已知网关错误编码为唯一 HTTP 响应契约。"""
    return JSONResponse(
        status_code=error.status_code,
        content=ErrorResponse(error={"message": error.message, "type": error.error_type, "code": error.code}).model_dump(
            exclude_none=True
        ),
        headers=getattr(error, "headers", None),
    )


def _caller_id_hash(caller_id: str) -> str:
    """只保留调用方标识的不可逆摘要，避免将 Key 或 IP 写入 SQLite。"""
    return hashlib.sha256(caller_id.encode()).hexdigest()


def _request_owner_id(request: Request) -> str:
    """计算内存 Run 的调用方摘要，避免 request_id 成为查看流内容的唯一凭证。"""
    caller_id = client_identity(
        request.headers.get("authorization"),
        request.headers.get("x-api-key"),
        request.client.host if request.client else None,
    )
    return _caller_id_hash(caller_id)


def _usage_record(
    *,
    metrics: GatewayMetrics,
    caller_id: str,
    created_at: datetime,
    prompt_use: PromptUse | None,
    error: GatewayError | None = None,
) -> UsageRecord:
    """将一次调用终态转换为可持久化且不含敏感正文的使用记录。"""
    return UsageRecord(
        request_id=metrics.request_id,
        caller_id_hash=_caller_id_hash(caller_id),
        model=metrics.model,
        status=metrics.status,
        input_tokens=metrics.input_tokens,
        output_tokens=metrics.output_tokens,
        total_tokens=metrics.total_tokens,
        latency_ms=metrics.latency_ms,
        first_token_latency_ms=metrics.first_token_latency_ms,
        retry_count=metrics.retry_count,
        error_type=error.error_type if error else None,
        error_code=error.code if error else None,
        prompt_id=prompt_use.id if prompt_use else None,
        prompt_version=prompt_use.version if prompt_use else None,
        created_at=created_at,
        updated_at=datetime.now(UTC),
    )


@app.exception_handler(GatewayError)
async def handle_gateway_error(_: Request, error: GatewayError) -> JSONResponse:
    """统一处理路由、服务和 Adapter 抛出的已知网关异常。"""
    return _error_response(error)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(_: Request, error: RequestValidationError) -> JSONResponse:
    """将 FastAPI/Pydantic 参数校验错误转换为网关错误契约。"""
    first_error = error.errors()[0] if error.errors() else {}
    return _error_response(InvalidRequestError(first_error.get("msg", "Invalid request"), code="validation_error"))


@app.exception_handler(HTTPException)
async def handle_http_exception(_: Request, error: HTTPException) -> JSONResponse:
    """兜底统一框架 HTTP 异常，避免出现 FastAPI 的 detail 包装。"""
    detail = error.detail
    if isinstance(detail, dict):
        return _error_response(
            GatewayError(
                error.status_code,
                str(detail.get("message", "Request failed")),
                error_type=str(detail.get("type", "http_error")),
                code=str(detail.get("code", "http_error")),
            )
        )
    return _error_response(GatewayError(error.status_code, str(detail), error_type="http_error", code="http_error"))


@app.exception_handler(StarletteHTTPException)
async def handle_starlette_http_exception(_: Request, error: StarletteHTTPException) -> JSONResponse:
    """统一 404 等由 Starlette 直接产生的 HTTP 错误。"""
    return _error_response(GatewayError(error.status_code, str(error.detail), error_type="http_error", code="http_error"))


@app.exception_handler(Exception)
async def handle_unexpected_exception(_: Request, __: Exception) -> JSONResponse:
    """不泄露内部异常细节，并保持所有 HTTP 失败响应的统一形状。"""
    return _error_response(GatewayError(500, "Internal server error", error_type="internal_error", code="internal_error"))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """返回进程存活状态，供负载均衡器或监控系统探测。"""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def local_management_page() -> FileResponse:
    """返回本地管理与调试单页。"""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/admin/ui/config")
async def local_ui_config(request: Request) -> dict[str, object]:
    """提供本地控制台状态；首次加载永不返回 Key 明文。"""
    if not is_local_or_secure_request(request):
        raise InvalidRequestError("Local UI configuration requires a local or HTTPS request", code="ui_config_not_allowed")
    status = request.app.state.runtime_key_service.status()
    return {
        "health": "ok",
        "database_path": gateway_database_path_from_env(),
        "deepseek_key": status.model_dump(),
        "typewriter": {
            "interval_ms": request.app.state.ui_settings.typewriter_interval_ms,
            "chars_per_tick": request.app.state.ui_settings.typewriter_chars_per_tick,
        },
    }


@app.get("/admin/ui/models")
async def local_ui_models(request: Request) -> list[dict[str, object]]:
    """返回 UI 下拉框与能力卡片所需的已注册模型快照。"""
    result: list[dict[str, object]] = []
    for model, registered in request.app.state.model_registry.items():
        adapter_name = type(registered.adapter).__name__
        protocol = "OpenAI Responses API" if adapter_name == "OpenAIAdapter" else "Anthropic Messages API"
        capabilities = registered.capabilities
        result.append(
            {
                "model": model,
                "protocol": protocol,
                "capabilities": {
                    "chat_completions": capabilities.chat_completions,
                    "responses": capabilities.responses,
                    "structured_output": capabilities.structured_output,
                    "streaming": capabilities.streaming,
                    "supports_temperature": capabilities.supports_temperature,
                    "supports_top_p": capabilities.supports_top_p,
                },
            }
        )
    return result


@app.post("/admin/ui/runtime-key", response_model=RuntimeKeyStatus)
async def update_runtime_key(payload: RuntimeKeyUpdate, request: Request) -> RuntimeKeyStatus:
    """仅在本地或 HTTPS 调试会话中更新进程内 DeepSeek Key。"""
    if not is_local_or_secure_request(request):
        raise InvalidRequestError("Runtime API key updates require a local or HTTPS request", code="runtime_key_not_allowed")
    return request.app.state.runtime_key_service.apply(payload.api_key, use_environment=payload.use_environment)


@app.post("/admin/ui/runtime-key/load-environment", response_model=RuntimeKeyEnvironmentDisplay)
async def display_environment_runtime_key(request: Request) -> RuntimeKeyEnvironmentDisplay:
    """由用户显式触发，读取环境 Key 并仅回给本地页面作只读展示。"""
    if not is_local_or_secure_request(request):
        raise InvalidRequestError("Environment API key reads require a local or HTTPS request", code="runtime_key_not_allowed")
    status = request.app.state.runtime_key_service.apply(None, use_environment=True)
    return RuntimeKeyEnvironmentDisplay(**status.model_dump(), value=request.app.state.runtime_key_service.display_key_for_local_ui())


@app.post("/v1/prompts", response_model=PromptTemplate, status_code=201)
async def create_prompt(payload: PromptVersionCreate, request: Request) -> PromptTemplate:
    """创建提示词或为指定 ID 创建新的不可变版本。"""
    return request.app.state.prompt_service.create_version(payload)


@app.get("/v1/prompts", response_model=list[PromptTemplate])
async def list_prompts(request: Request, prompt_id: str | None = None) -> list[PromptTemplate]:
    """列出所有提示词版本，或指定提示词的版本历史。"""
    return request.app.state.prompt_service.list(prompt_id)


@app.get("/v1/prompts/{prompt_id}", response_model=PromptTemplate)
async def get_prompt(prompt_id: str, request: Request, version: int | None = None) -> PromptTemplate:
    """读取指定版本；未提供 version 时读取 active version。"""
    return request.app.state.prompt_service.get(prompt_id, version)


@app.post("/v1/prompts/{prompt_id}/activate", response_model=PromptTemplate)
async def activate_prompt(prompt_id: str, payload: PromptActivation, request: Request) -> PromptTemplate:
    """切换指定提示词的活动版本。"""
    return request.app.state.prompt_service.activate(prompt_id, payload.version)


@app.post("/v1/prompts/{prompt_id}/render")
async def render_prompt(prompt_id: str, payload: PromptRenderRequest, request: Request) -> dict[str, object]:
    """预览指定提示词版本的安全 Jinja2 渲染结果。"""
    template, rendered = request.app.state.prompt_service.render(
        PromptReference(id=prompt_id, version=payload.version, variables=payload.variables)
    )
    return {"id": template.id, "version": template.version, "role": template.role, "content": rendered}


@app.get("/admin/usage", response_model=list[UsageRecord])
async def list_usage(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    model: str | None = None,
    status: UsageStatus | None = None,
    request_id: str | None = None,
) -> list[UsageRecord]:
    """查询已持久化的非敏感调用指标；生产环境应由网关鉴权保护。"""
    return request.app.state.usage_repository.list(
        UsageQuery(limit=limit, model=model, status=status, request_id=request_id)
    )


@app.get("/v1/streams/{request_id}", response_model=StreamRunSnapshot)
async def get_stream_run(request_id: str, request: Request) -> StreamRunSnapshot:
    """查询当前进程内的流状态；不返回请求正文或模型输出。"""
    return request.app.state.stream_run_manager.get(request_id, _request_owner_id(request))


@app.get("/v1/streams/{request_id}/events")
async def reconnect_stream_run(
    request_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """重放游标后的内存事件，并继续订阅尚未结束的同一后台 Run。"""
    cursor = after_event_id
    if last_event_id is not None:
        try:
            cursor = max(0, int(last_event_id))
        except ValueError as exc:
            raise InvalidRequestError("Last-Event-ID must be an integer", code="invalid_event_id") from exc
    owner_id = _request_owner_id(request)
    # 在响应启动前完成归属检查，确保不存在的 Run 使用统一 HTTP 错误体。
    request.app.state.stream_run_manager.get(request_id, owner_id)
    return StreamingResponse(
        request.app.state.stream_run_manager.subscribe(
            request_id, owner_id, after_event_id=cursor, client_request=request
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id},
    )


@app.post("/v1/streams/{request_id}/cancel", response_model=StreamRunSnapshot)
async def cancel_stream_run(request_id: str, request: Request) -> StreamRunSnapshot:
    """显式关闭后台上游生成；仅断开 SSE 订阅不会调用此接口。"""
    return await request.app.state.stream_run_manager.cancel(request_id, _request_owner_id(request))


@app.post(
    "/v1/generate",
    response_model=None,
    responses={400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
)
async def generate(body: ModelRequest, request: Request) -> ModelResult | StreamingResponse:
    """按 model 路由请求、校验模型能力并返回统一模型结果。"""
    request_id = f"req_{uuid4().hex}"
    started = time.perf_counter()
    created_at = datetime.now(UTC)
    caller_id = client_identity(
        request.headers.get("authorization"), request.headers.get("x-api-key"),
        request.client.host if request.client else None,
    )
    owner_id = _caller_id_hash(caller_id)
    prompt_use: PromptUse | None = None
    retry_count = 0
    registered = request.app.state.model_registry.get(body.model)
    try:
        if registered is None:
            raise InvalidRequestError(f"Unsupported model: {body.model}", code="model_not_found")
        body, prompt_use = request.app.state.prompt_service.apply(body)
        if body.stream and body.max_output_tokens is None:
            body = body.model_copy(
                update={"max_output_tokens": request.app.state.stream_run_manager.settings.default_max_output_tokens}
            )
        request.app.state.model_registry.validate_request(body, registered.capabilities)
        await request.app.state.rate_limiter.check(caller_id, body.model)
        if body.stream:
            async def persist_stream_usage(
                metrics: GatewayMetrics, error_type: str | None, error_code: str | None
            ) -> None:
                error = (
                    GatewayError(502, "Stream failed", error_type=error_type, code=error_code)
                    if error_type is not None and error_code is not None
                    else None
                )
                request.app.state.usage_repository.record_safely(
                    _usage_record(
                        metrics=metrics, caller_id=caller_id, created_at=created_at,
                        prompt_use=prompt_use, error=error,
                    )
                )

            request.app.state.stream_run_manager.start(
                request_id=request_id,
                owner_id=owner_id,
                model=body.model,
                events=registered.adapter.generate_stream(body),
                response_format=body.response_format,
                event_factory=lambda: registered.adapter.generate_stream(body),
                stream_settings=request.app.state.stream_settings,
                on_finished=persist_stream_usage,
            )
            return StreamingResponse(
                request.app.state.stream_run_manager.subscribe(
                    request_id, owner_id, client_request=request
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-ID": request_id},
            )
        result, retry_count = await generate_with_retry(
            lambda: registered.adapter.generate(body), settings=request.app.state.generate_retry_settings
        )
        result.metrics = build_gateway_metrics(
            request_id=request_id, model=body.model, status="completed", started=started,
            input_tokens=result.usage.get("input_tokens"), output_tokens=result.usage.get("output_tokens"),
            total_tokens=result.usage.get("total_tokens"), retry_count=retry_count,
        )
        request.app.state.usage_repository.record_safely(
            _usage_record(metrics=result.metrics, caller_id=caller_id, created_at=created_at, prompt_use=prompt_use)
        )
        return result
    except GatewayError as exc:
        retry_count = getattr(exc, "retry_count", retry_count)
        metrics = build_gateway_metrics(
            request_id=request_id, model=body.model, status="failed", started=started, retry_count=retry_count
        )
        request.app.state.usage_repository.record_safely(
            _usage_record(metrics=metrics, caller_id=caller_id, created_at=created_at, prompt_use=prompt_use, error=exc)
        )
        raise
    except ValueError as exc:
        error = InvalidRequestError(str(exc), code="capability_not_supported")
        metrics = build_gateway_metrics(request_id=request_id, model=body.model, status="failed", started=started)
        request.app.state.usage_repository.record_safely(
            _usage_record(metrics=metrics, caller_id=caller_id, created_at=created_at, prompt_use=prompt_use, error=error)
        )
        raise error from exc
    except Exception:
        error = GatewayError(500, "Internal server error", error_type="internal_error", code="internal_error")
        metrics = build_gateway_metrics(request_id=request_id, model=body.model, status="failed", started=started)
        request.app.state.usage_repository.record_safely(
            _usage_record(metrics=metrics, caller_id=caller_id, created_at=created_at, prompt_use=prompt_use, error=error)
        )
        raise
