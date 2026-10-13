"""Agent 工具治理：将模型提出的调用转换为可授权、可审计的执行。

调用链路为：参数校验 → 权限与业务预检 → 一次性审批 → 限时执行 → 结果脱敏
→ 审计记录。模型只提交业务参数，身份、租户、模式与审批凭证由调用方提供；
handler 接收运行时注入的调用 ID，用于关联请求、工具结果与审计事件。

订单、账户、审批和审计使用内存状态，支持离线验证同一条执行链路。
CONFIRM 表示调用方需要取得审批后重新调用；本模块没有持久化暂停与恢复机制。

设计参考及对应边界：
- LangChain Tools：业务 Schema 与运行时上下文分离，并关联工具调用 ID。
  https://docs.langchain.com/oss/python/langchain/tools
- LangGraph Interrupts：在产生副作用前取得人工确认，并考虑重复执行的幂等性。
  https://docs.langchain.com/oss/python/langgraph/interrupts
- Pydantic：通过 strict 与 extra 配置约束不可信参数的类型和字段集合。
  https://docs.pydantic.dev/latest/concepts/strict_mode/
  https://docs.pydantic.dev/latest/api/config/#pydantic.config.ConfigDict.extra
- Tenacity：将可重试异常、次数上限与等待策略分开定义。
  https://tenacity.readthedocs.io/en/stable/index.html
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class PermissionMode(StrEnum):
    """调用方指定的执行模式；模型参数不能自行切换权限模式。"""

    DEFAULT = "default"
    PLAN = "plan"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dontAsk"


class Effect(StrEnum):
    """描述工具副作用，用于 plan 限制、重试资格和超时结果分类。"""

    READ = "read"
    WRITE = "write"
    SHELL = "shell"


class Risk(StrEnum):
    """高风险会触发审批要求，避免仅靠工具可见性决定是否执行。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionAction(StrEnum):
    """授权结果：执行、拒绝，或交由调用方收集确认后再次调用。"""

    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"


Permission = Literal["order:read", "refund:create", "shell:run", "transfer:execute"]


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """由认证层或可信调用方构造的身份与权限快照，不属于模型参数 Schema。"""

    trace_id: str
    user_id: str
    tenant_id: str
    mode: PermissionMode
    permissions: frozenset[Permission]
    allowed_tools: frozenset[str]
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """服务端执行策略；幂等标记用于控制重试资格，本身不实现业务去重。"""

    effect: Effect
    risk: Risk
    permission: Permission
    requires_approval: bool
    timeout_seconds: float
    max_retries: int
    idempotent: bool


class StrictArgs(BaseModel):
    """拒绝未声明字段与宽松类型转换；参数合法性和业务授权分别校验。"""

    # extra 拦截 approved、user_id 等额外键；strict 避免将金额字符串自动转为数字。
    model_config = ConfigDict(extra="forbid", strict=True)


class GetOrderArgs(StrictArgs):
    """限制订单号格式；订单是否归属当前租户仍由业务查询判断。"""

    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")


class CreateRefundArgs(StrictArgs):
    """声明退款候选参数；订单状态与可退余额在预检中另行核验。"""

    order_id: str = Field(pattern=r"^ord_[0-9]{4}$")
    amount: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=4, max_length=200)


class RunShellArgs(StrictArgs):
    """限制命令文本长度；格式校验不代表命令已获得执行权限。"""

    command: str = Field(min_length=1, max_length=200)


class TransferArgs(StrictArgs):
    """转账候选参数：约束账户格式和金额范围，继承额外字段拒绝规则。"""

    from_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    to_account: str = Field(pattern=r"^ACC-[A-Z]-[0-9]{6}$")
    amount: float = Field(gt=0, le=100000)


ArgsModel = GetOrderArgs | CreateRefundArgs | RunShellArgs | TransferArgs
# 调用 ID 和上下文由运行时传递，模型只能填写各个参数模型声明的业务字段。
Handler = Callable[[str, ArgsModel, ExecutionContext], Awaitable[Mapping[str, Any]]]
# 预检只判断业务条件；规则目标用于前缀匹配，审批参数绑定由 _approval_digest 完成。
Precheck = Callable[[ArgsModel, ExecutionContext], Awaitable[None]]
CanonicalTarget = Callable[[ArgsModel], str]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """集中绑定参数契约、策略与处理器，供发现阶段和执行阶段使用同一份注册信息。"""

    name: str
    description: str
    parameters_model: type[StrictArgs]
    policy: ToolPolicy
    handler: Handler
    canonical_target: CanonicalTarget
    precheck: Precheck | None = None

    def to_model_tool(self) -> dict[str, Any]:
        """导出模型可见的名称、描述和 Schema；权限与审批策略保留在执行侧。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_model.model_json_schema(),
            },
        }


@dataclass(frozen=True, slots=True)
class PermissionRule:
    """匹配工具名及可选目标前缀；规则效果由固定优先级的权限引擎解释。"""

    effect: Literal["allow", "deny"]
    tool_name: str
    target_prefix: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """同时记录动作、错误码与决策来源，便于调用方交互及审计定位。"""

    action: DecisionAction
    code: str
    reason: str
    source: Literal[
        "rule", "mode", "whitelist", "rbac", "business", "approval", "risk", "default"
    ]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """保留 Provider 或 Harness 的原始调用标识，关联后续执行与工具消息。"""

    tool_call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """统一表达执行结果；retryable 是结果元数据，不会自行触发重试。"""

    tool_call_id: str
    tool_name: str
    ok: bool
    action: DecisionAction
    code: str
    content: Any
    retryable: bool = False

    def to_tool_message(self) -> dict[str, Any]:
        """沿用原调用 ID 回填 tool 消息，使模型能将结果对应到提出的调用。"""

        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": json.dumps(
                {
                    "ok": self.ok,
                    "code": self.code,
                    "action": self.action,
                    "content": self.content,
                },
                ensure_ascii=False,
            ),
        }


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """分开记录授权与执行阶段；只保存参数键名，避免把业务参数值写入审计。"""

    trace_id: str
    tool_call_id: str
    tool_name: str
    user_id: str
    tenant_id: str
    phase: Literal["decision", "execution"]
    decision: str
    code: str
    argument_keys: tuple[str, ...]
    latency_ms: int | None = None


@dataclass(slots=True)
class ApprovalRecord:
    """一次性审批记录：绑定主体、租户、工具和参数摘要，并限制有效期。"""

    approval_id: str
    user_id: str
    tenant_id: str
    tool_name: str
    digest: str
    expires_at: float
    used: bool = False


class PolicyDenied(RuntimeError):
    """预期的策略或业务拒绝，携带稳定错误码供运行时转换为工具结果。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TransientToolError(RuntimeError):
    """标记可恢复的依赖故障；只有具备重试资格的工具才会再次执行。"""

    pass


def _stable_value(value: Any) -> Any:
    """递归排序映射键并序列化模型，避免参数字段顺序改变审批摘要。"""

    if isinstance(value, BaseModel):
        return _stable_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {key: _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    return value


def _approval_digest(tool_name: str, arguments: ArgsModel | Mapping[str, Any]) -> str:
    """将工具名和完整参数绑定；摘要用于一致性校验，授权来自服务端审批记录。"""

    canonical = json.dumps(_stable_value(arguments), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{tool_name}:{canonical}".encode()).hexdigest()


class ApprovalStore:
    """进程内审批存储；签发与核销均由可信调用链完成，不接受模型自报已批准。"""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def approve(
        self,
        approval_id: str,
        context: ExecutionContext,
        tool_name: str,
        arguments: ArgsModel | Mapping[str, Any],
        *,
        ttl_seconds: float = 300,
    ) -> None:
        """签发与具体动作绑定的限时凭证，参数变化后必须取得新的审批。"""

        self._records[approval_id] = ApprovalRecord(
            approval_id=approval_id,
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            tool_name=tool_name,
            digest=_approval_digest(tool_name, arguments),
            expires_at=time.time() + ttl_seconds,
        )

    def consume(
        self,
        approval_id: str | None,
        context: ExecutionContext,
        tool_name: str,
        arguments: ArgsModel,
    ) -> bool:
        """校验主体、动作与有效期后立即核销，阻止同一凭证重复执行。"""

        record = self._records.get(approval_id or "")
        valid = bool(
            record
            and not record.used
            and record.expires_at >= time.time()
            and record.user_id == context.user_id
            and record.tenant_id == context.tenant_id
            and record.tool_name == tool_name
            and record.digest == _approval_digest(tool_name, arguments)
        )
        if valid and record:
            record.used = True
        return valid


class AuditSink:
    """内存审计端口，将事件收集与执行逻辑分离，便于检查整个调用链路。"""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        """按发生顺序记录事件，保留审批拦截与实际执行之间的区别。"""

        self.records.append(record)


# 命令特征仅用于风险识别；实际匹配效果取决于权限引擎中的规则优先级。
DANGEROUS_SHELL_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bgit\s+push\s+--force\b", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r">\s*/dev/", re.I),
)


def _is_dangerous_shell(command: str) -> bool:
    """识别已列出的危险命令特征；正则匹配不能证明任意命令安全。"""

    return any(pattern.search(command) for pattern in DANGEROUS_SHELL_PATTERNS)


def _redact(value: Any) -> Any:
    """递归处理成功返回值，减少凭证、邮箱和完整账号进入模型上下文。"""

    if isinstance(value, Mapping):
        return {
            key: "***" if re.search(r"token|secret|password|authorization", key, re.I) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***", value)
        # 保留账号前缀与末四位以便核对；只改返回文本，不改账户存储中的键。
        value = re.sub(r"\b(ACC-[A-Z]-)[0-9]{2}([0-9]{4})\b", r"\1****\2", value)
        return value
    return value


class PermissionEngine:
    """集中执行权限、业务预检和审批；decide 的固定优先级构成执行边界。"""

    def __init__(self, rules: Sequence[PermissionRule], approvals: ApprovalStore) -> None:
        self._rules = tuple(rules)
        self._approvals = approvals

    def _rule_matches(self, rule: PermissionRule, tool: ToolDefinition, arguments: ArgsModel) -> bool:
        """将业务参数映射为规则目标；该目标不参与审批摘要计算。"""

        if rule.tool_name != tool.name:
            return False
        if rule.target_prefix is None:
            return True
        return tool.canonical_target(arguments).startswith(rule.target_prefix)

    async def decide(
        self,
        tool: ToolDefinition,
        arguments: ArgsModel,
        context: ExecutionContext,
    ) -> PermissionDecision:
        # 1. deny-first：硬拒绝不能被 allow 或 bypass 覆盖。
        if any(
            rule.effect == "deny" and self._rule_matches(rule, tool, arguments)
            for rule in self._rules
        ):
            return PermissionDecision(DecisionAction.DENY, "DENY_RULE", "命中 deny 规则", "rule")

        # 2. plan 是执行层只读契约，而不是一句系统提示词。
        if context.mode is PermissionMode.PLAN and tool.policy.effect is not Effect.READ:
            return PermissionDecision(
                DecisionAction.DENY,
                "PLAN_MODE_DENIED",
                "plan 模式禁止写操作和 Shell",
                "mode",
            )

        # 3. 发现阶段过滤后，执行阶段仍然要重新检查白名单。
        if tool.name not in context.allowed_tools:
            return PermissionDecision(
                DecisionAction.DENY,
                "TOOL_NOT_ALLOWED",
                "工具不在本轮执行白名单",
                "whitelist",
            )

        # 4. 只相信认证层生成的 ExecutionContext。
        if tool.policy.permission not in context.permissions:
            return PermissionDecision(
                DecisionAction.DENY,
                "PERMISSION_DENIED",
                f"缺少业务权限 {tool.policy.permission}",
                "rbac",
            )

        # 5. 资源归属、状态和额度在 handler 之前验证。
        try:
            if tool.precheck:
                await tool.precheck(arguments, context)
        except PolicyDenied as error:
            return PermissionDecision(DecisionAction.DENY, error.code, str(error), "business")

        # 6. 高风险业务写操作必须使用一次性、参数绑定审批。
        if tool.policy.requires_approval or tool.policy.risk is Risk.HIGH:
            if self._approvals.consume(context.approval_id, context, tool.name, arguments):
                return PermissionDecision(
                    DecisionAction.ALLOW,
                    "APPROVED",
                    "审批与当前用户、租户、工具和参数完全匹配",
                    "approval",
                )
            if context.mode is PermissionMode.DONT_ASK:
                return PermissionDecision(
                    DecisionAction.DENY,
                    "APPROVAL_REQUIRED",
                    "非交互模式无法完成高风险确认",
                    "approval",
                )
            return PermissionDecision(
                DecisionAction.CONFIRM,
                "APPROVAL_REQUIRED",
                "需要确认本次具体动作",
                "approval",
            )

        # 7. bypass 只能跳过普通确认，不能跳过前面的硬边界。
        if context.mode is PermissionMode.BYPASS_PERMISSIONS:
            return PermissionDecision(
                DecisionAction.ALLOW,
                "BYPASS_ALLOWED",
                "跳过普通确认，但硬边界已经全部通过",
                "mode",
            )

        # 8. allow 规则只在 deny、plan、白名单、RBAC 和审批以后生效。
        if any(
            rule.effect == "allow" and self._rule_matches(rule, tool, arguments)
            for rule in self._rules
        ):
            return PermissionDecision(DecisionAction.ALLOW, "ALLOW_RULE", "命中 allow 规则", "rule")

        # 9. 正则只是教学兜底，生产中必须配合窄工具、AST 与沙箱。
        if tool.policy.effect is Effect.SHELL and _is_dangerous_shell(
            str(getattr(arguments, "command", ""))
        ):
            if context.mode is PermissionMode.DONT_ASK:
                return PermissionDecision(
                    DecisionAction.DENY,
                    "DANGEROUS_OPERATION",
                    "危险 Shell 在非交互模式下被拒绝",
                    "risk",
                )
            return PermissionDecision(
                DecisionAction.CONFIRM,
                "DANGEROUS_OPERATION",
                "危险 Shell 需要用户确认",
                "risk",
            )

        return PermissionDecision(
            DecisionAction.ALLOW,
            "DEFAULT_ALLOWED",
            "所有确定性检查均已通过",
            "default",
        )


class ToolRuntime:
    """Agent、离线入口和测试共用的执行边界，集中完成校验、授权、执行与审计。"""

    def __init__(
        self,
        tools: Sequence[ToolDefinition],
        permission_engine: PermissionEngine,
        audit_sink: AuditSink,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._permission_engine = permission_engine
        self._audit = audit_sink

    def model_tools(self, context: ExecutionContext) -> list[dict[str, Any]]:
        """按白名单缩小模型可见工具集；权限、业务和审批仍在执行时重新检查。"""

        return [
            tool.to_model_tool()
            for tool in self._tools.values()
            if tool.name in context.allowed_tools
        ]

    async def invoke(self, call: ToolCall, context: ExecutionContext) -> ToolResult:
        """先授权再执行；CONFIRM 会返回调用方，不在当前协程内等待人工审批。"""

        started = time.perf_counter()
        tool = self._tools.get(call.name)
        if tool is None:
            return self._rejected(call, context, "TOOL_NOT_FOUND", "工具不存在")

        # 不可信参数先转换为业务对象，非法字段和类型不会进入预检或处理器。
        try:
            arguments = tool.parameters_model.model_validate(call.arguments)
        except ValidationError as error:
            details = [
                {"path": ".".join(map(str, item["loc"])), "message": item["msg"]}
                for item in error.errors(include_url=False)
            ]
            return self._rejected(call, context, "INVALID_ARGUMENT", details)

        # 工具被模型看见不代表获得执行权，每次调用都重新做完整决策。
        decision = await self._permission_engine.decide(tool, arguments, context)
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="decision",
                decision=decision.action,
                code=decision.code,
                argument_keys=tuple(sorted(call.arguments)),
            )
        )
        if decision.action is not DecisionAction.ALLOW:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                ok=False,
                action=decision.action,
                code=decision.code,
                content=decision.reason,
            )

        # 只有 ALLOW 能进入副作用阶段；原调用 ID 与可信上下文独立于模型参数传递。
        try:
            raw = await self._execute_with_recovery(tool, call.tool_call_id, arguments, context)
        except TimeoutError:
            # 非幂等写操作超时不能等同于未执行，避免调用方据此重复提交扣款。
            code = "TIMEOUT" if tool.policy.effect is Effect.READ or tool.policy.idempotent else "TIMEOUT_UNKNOWN"
            return self._failed(call, context, started, code, "工具执行超时")
        except PolicyDenied as error:
            return self._failed(call, context, started, error.code, str(error))
        except Exception as error:  # 统一错误码；当前返回异常消息，不序列化调用堆栈。
            return self._failed(call, context, started, "TOOL_ERROR", str(error))

        # 成功结果先脱敏，再封装为模型可读结果；审计保留关联信息而非原始参数值。
        safe_content = _redact(dict(raw))
        latency_ms = round((time.perf_counter() - started) * 1_000)
        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="execution",
                decision="executed",
                code="OK",
                argument_keys=tuple(sorted(call.arguments)),
                latency_ms=latency_ms,
            )
        )
        return ToolResult(call.tool_call_id, call.name, True, DecisionAction.ALLOW, "OK", safe_content)

    async def _execute_with_recovery(
        self,
        tool: ToolDefinition,
        tool_call_id: str,
        arguments: ArgsModel,
        context: ExecutionContext,
    ) -> Mapping[str, Any]:
        """按副作用语义限制重试，每次尝试分别应用超时与有上限的退避。"""

        # 与 Tenacity 的异常筛选、停止条件和等待策略分离思路一致。
        # 读取或显式幂等工具才允许重试；非幂等转账不能因依赖故障再次扣款。
        retries = tool.policy.max_retries if tool.policy.effect is Effect.READ or tool.policy.idempotent else 0
        for attempt in range(retries + 1):
            try:
                async with asyncio.timeout(tool.policy.timeout_seconds):
                    # 运行时直接传递 ID，使 handler 无需从业务参数中提取或信任调用标识。
                    return await tool.handler(tool_call_id, arguments, context)
            except TransientToolError:
                if attempt == retries:
                    raise
                await asyncio.sleep(min(0.05 * (2**attempt), 0.2))
        raise AssertionError("unreachable")

    def _rejected(
        self,
        call: ToolCall,
        context: ExecutionContext,
        code: str,
        content: Any,
    ) -> ToolResult:
        """记录尚未进入执行阶段的拒绝，参数格式错误也具有可追踪的审计事件。"""

        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="decision",
                decision="deny",
                code=code,
                argument_keys=tuple(sorted(call.arguments)),
            )
        )
        return ToolResult(call.tool_call_id, call.name, False, DecisionAction.DENY, code, content)

    def _failed(
        self,
        call: ToolCall,
        context: ExecutionContext,
        started: float,
        code: str,
        content: Any,
    ) -> ToolResult:
        """记录已获准调用的执行失败或超时，与授权阶段的拒绝分开统计。"""

        self._audit.append(
            AuditRecord(
                trace_id=context.trace_id,
                tool_call_id=call.tool_call_id,
                tool_name=call.name,
                user_id=context.user_id,
                tenant_id=context.tenant_id,
                phase="execution",
                decision="failed",
                code=code,
                argument_keys=tuple(sorted(call.arguments)),
                latency_ms=round((time.perf_counter() - started) * 1_000),
            )
        )
        return ToolResult(call.tool_call_id, call.name, False, DecisionAction.DENY, code, content)


# 数据查询始终携带 context.tenant_id，模型提供的业务标识不能独立决定租户范围。
ORDERS = {
    ("tenant_a", "ord_1001"): {
        "status": "paid",
        "refundable": 399.0,
        "customer_email": "alice@example.com",
    }
}
# 租户和账号共同索引内存余额；测试在用例之间快照还原，隔离可变状态。
ACCOUNTS: dict[tuple[str, str], float] = {
    ("tenant_a", "ACC-A-123456"): 100000.0,
    ("tenant_a", "ACC-A-654321"): 5000.0,
    ("tenant_a", "ACC-A-888888"): 20000.0,
    ("tenant_b", "ACC-B-111111"): 50000.0,
}
SIDE_EFFECTS = {"refund_executions": 0, "shell_executions": 0}


def reset_side_effects() -> None:
    """重置执行次数计数；账户余额由测试夹具单独还原。"""

    SIDE_EFFECTS.update(refund_executions=0, shell_executions=0)


async def get_order_handler(
    _tool_call_id: str,
    raw_arguments: ArgsModel,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    """按可信租户查询订单；返回值中的邮箱和凭证交由运行时统一脱敏。"""

    arguments = raw_arguments
    assert isinstance(arguments, GetOrderArgs)
    order = ORDERS.get((context.tenant_id, arguments.order_id))
    if not order:
        raise PolicyDenied("ORDER_NOT_FOUND", "当前租户下不存在该订单")
    return {**order, "access_token": "tok_demo_should_not_leak"}


async def refund_precheck(raw_arguments: ArgsModel, context: ExecutionContext) -> None:
    """在副作用前确认订单归属、支付状态与可退金额，不修改订单或余额。"""

    arguments = raw_arguments
    assert isinstance(arguments, CreateRefundArgs)
    order = ORDERS.get((context.tenant_id, arguments.order_id))
    if not order or order["status"] != "paid":
        raise PolicyDenied("BUSINESS_RULE_DENIED", "订单不存在或状态不可退款")
    if arguments.amount > float(order["refundable"]):
        raise PolicyDenied("BUSINESS_RULE_DENIED", "退款金额超过可退金额")


async def transfer_precheck(raw_arguments: ArgsModel, context: ExecutionContext) -> None:
    """先检查禁止金额区间，再检查租户内余额；预检失败时尚未核销审批或扣款。"""

    arguments = raw_arguments
    assert isinstance(arguments, TransferArgs)
    # 这是区间规则而非金额上限；超过 80000 的请求仍需通过余额检查与审批。
    if 50000 < arguments.amount <= 80000:
        raise PolicyDenied("EXCEED_LIMIT", "转账金额处在禁止区间")
    balance = ACCOUNTS.get((context.tenant_id, arguments.from_account), 0.0)
    if balance < arguments.amount:
        raise PolicyDenied("INSUFFICIENT_BALANCE", "转出账户余额不足")


async def create_refund_handler(
    tool_call_id: str,
    raw_arguments: ArgsModel,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    """记录退款执行并返回受理结果；调用 ID 用于关联响应，本实现不做持久化去重。"""

    arguments = raw_arguments
    assert isinstance(arguments, CreateRefundArgs)
    SIDE_EFFECTS["refund_executions"] += 1
    return {
        "refund_id": "ref_9001",
        "idempotency_key": tool_call_id,
        "tenant_id": context.tenant_id,
        "order_id": arguments.order_id,
        "amount": arguments.amount,
        "status": "accepted",
    }


async def transfer_handler(
    tool_call_id: str,
    raw_arguments: ArgsModel,
    context: ExecutionContext,
) -> Mapping[str, Any]:
    """在预检与审批通过后修改租户内余额，返回关联原调用的交易结果。"""

    arguments = raw_arguments
    assert isinstance(arguments, TransferArgs)
    if arguments.amount > 80000:
        # 故障注入发生在扣款前，2 秒执行超时会在此 await 处取消 3 秒延迟。
        await asyncio.sleep(3.0)

    from_key = (context.tenant_id, arguments.from_account)
    to_key = (context.tenant_id, arguments.to_account)
    if to_key not in ACCOUNTS:
        raise PolicyDenied("ACCOUNT_NOT_FOUND", "转入账户不存在")
    # 先验证收款账户，再修改双方余额，避免账户缺失时只完成扣款。
    # 两次内存更新之间没有 await；这里没有数据库事务或跨进程并发控制。
    ACCOUNTS[from_key] -= arguments.amount
    ACCOUNTS[to_key] += arguments.amount
    return {
        "txn_id": tool_call_id[-6:],
        "from": arguments.from_account,
        "to": arguments.to_account,
        "amount": arguments.amount,
        "status": "accepted",
    }


async def simulated_shell_handler(
    _tool_call_id: str,
    raw_arguments: ArgsModel,
    _context: ExecutionContext,
) -> Mapping[str, Any]:
    """只返回模拟命令结果并记录执行次数，权限链路验证不会创建真实子进程。"""

    arguments = raw_arguments
    assert isinstance(arguments, RunShellArgs)
    SIDE_EFFECTS["shell_executions"] += 1
    return {
        "simulated": True,
        "command": arguments.command,
        "stdout": "教学模拟：没有创建真实子进程",
    }


def build_tools() -> list[ToolDefinition]:
    """集中注册工具能力及执行策略，业务处理器通过元数据接入统一运行时。"""

    return [
        ToolDefinition(
            name="get_order",
            description="查询当前租户订单状态和可退金额",
            parameters_model=GetOrderArgs,
            policy=ToolPolicy(Effect.READ, Risk.MEDIUM, "order:read", False, 1.0, 2, True),
            handler=get_order_handler,
            canonical_target=lambda args: str(getattr(args, "order_id")),
        ),
        ToolDefinition(
            name="create_refund",
            description="为当前租户的已支付订单创建退款",
            parameters_model=CreateRefundArgs,
            policy=ToolPolicy(Effect.WRITE, Risk.HIGH, "refund:create", True, 2.0, 0, False),
            handler=create_refund_handler,
            precheck=refund_precheck,
            canonical_target=lambda args: f"{getattr(args, 'order_id')}:{getattr(args, 'amount')}",
        ),
        ToolDefinition(
            name="run_shell",
            description="教学用模拟 Shell，不执行真实系统命令",
            parameters_model=RunShellArgs,
            policy=ToolPolicy(Effect.SHELL, Risk.MEDIUM, "shell:run", False, 1.0, 0, False),
            handler=simulated_shell_handler,
            canonical_target=lambda args: str(getattr(args, "command")),
        ),
        # 转账是非幂等写操作：审批绑定具体参数，超时后不能自动重新扣款。
        ToolDefinition(
            name="transfer",
            description="在当前租户内进行账户转账",
            parameters_model=TransferArgs,
            policy=ToolPolicy(Effect.WRITE, Risk.HIGH, "transfer:execute", True, 2.0, 0, False),
            handler=transfer_handler,
            precheck=transfer_precheck,
            # 规则匹配目标区分账户与金额；审批摘要另外绑定工具名与完整参数。
            canonical_target=lambda args: (
                f"{getattr(args, 'from_account')}:{getattr(args, 'to_account')}:"
                f"{getattr(args, 'amount')}"
            ),
        ),
    ]


# 保留针对具体命令的规则，使 deny 优先级与普通 allow 行为可以独立验证。
DEFAULT_RULES = (
    PermissionRule("deny", "run_shell", "rm -rf"),
    PermissionRule("deny", "run_shell", "git push --force"),
    PermissionRule("allow", "run_shell", "pytest"),
)


def base_context(**overrides: Any) -> ExecutionContext:
    """提供离线基准身份；可信调用方可覆盖配置，工具参数不能修改这些字段。"""

    context = ExecutionContext(
        trace_id="trace_demo",
        user_id="u_100",
        tenant_id="tenant_a",
        mode=PermissionMode.DEFAULT,
        permissions=frozenset({"order:read", "refund:create", "shell:run"}),
        allowed_tools=frozenset({"get_order", "create_refund", "run_shell"}),
    )
    return replace(context, **overrides)


def build_runtime(
    *,
    approvals: ApprovalStore | None = None,
    audit: AuditSink | None = None,
    rules: Sequence[PermissionRule] = DEFAULT_RULES,
) -> tuple[ToolRuntime, ApprovalStore, AuditSink]:
    """装配注册表、审批、审计和权限引擎；依赖可注入，便于隔离测试状态。"""

    approval_store = approvals or ApprovalStore()
    audit_sink = audit or AuditSink()
    engine = PermissionEngine(rules, approval_store)
    return ToolRuntime(build_tools(), engine, audit_sink), approval_store, audit_sink


async def run_offline_demo() -> None:
    """用同一运行时串联查询、审批、参数拒绝和模式限制，验证实际治理链路。"""

    reset_side_effects()
    runtime, approvals, audit = build_runtime()
    context = base_context()
    refund_arguments = {"order_id": "ord_1001", "amount": 399.0, "reason": "商品存在质量问题"}

    results = [
        await runtime.invoke(ToolCall("call_01", "get_order", {"order_id": "ord_1001"}), context),
        await runtime.invoke(ToolCall("call_02", "create_refund", refund_arguments), context),
    ]
    approvals.approve("approval_01", context, "create_refund", refund_arguments)
    results.append(
        await runtime.invoke(
            ToolCall("call_03", "create_refund", refund_arguments),
            replace(context, approval_id="approval_01"),
        )
    )
    results.extend(
        [
            await runtime.invoke(
                ToolCall(
                    "call_04",
                    "create_refund",
                    {**refund_arguments, "user_id": "admin", "approved": True},
                ),
                context,
            ),
            await runtime.invoke(
                ToolCall("call_05", "run_shell", {"command": "rm -rf /tmp/demo"}),
                replace(context, mode=PermissionMode.BYPASS_PERMISSIONS),
            ),
            await runtime.invoke(
                ToolCall("call_06", "create_refund", refund_arguments),
                replace(context, mode=PermissionMode.PLAN, approval_id="approval_01"),
            ),
        ]
    )

    for result in results:
        print(json.dumps(result.__dict__ if hasattr(result, "__dict__") else {
            "tool_call_id": result.tool_call_id,
            "tool_name": result.tool_name,
            "ok": result.ok,
            "action": result.action,
            "code": result.code,
            "content": result.content,
        }, ensure_ascii=False, default=str))
    print(json.dumps({"side_effects": SIDE_EFFECTS, "audit_records": len(audit.records)}, ensure_ascii=False))


async def run_deepseek_agent(user_input: str) -> None:
    """串联流式模型响应、受控工具执行和结果回填，最多进行八轮调用。"""

    from openai import AsyncOpenAI

    # 凭证通过环境注入，不进入模型消息或工具参数。
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")

    runtime, _, _ = build_runtime()
    # 当前对话只开放订单查询，减少模型可选择的能力；执行阶段仍会重新授权。
    context = base_context(allowed_tools=frozenset({"get_order"}))
    client = AsyncOpenAI(api_key=api_key, base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "你是订单助手。只根据工具结果回答，不得伪造订单事实。",
        },
        {"role": "user", "content": user_input},
    ]

    for _round in range(8):
        # 显式轮数预算约束 Agent Loop，避免工具调用反复循环而无限消耗资源。
        stream = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=runtime.model_tools(context),
            stream=True,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text_parts: list[str] = []
        pending_calls: dict[int, dict[str, Any]] = {}

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                print(delta.content, end="", flush=True)
            for delta_call in delta.tool_calls or []:
                # 流式调用可能分片且相互交错，按 index 收集后才能解析完整 JSON。
                current = pending_calls.setdefault(
                    delta_call.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if delta_call.id:
                    current["id"] = delta_call.id
                if delta_call.function:
                    if delta_call.function.name:
                        current["function"]["name"] += delta_call.function.name
                    if delta_call.function.arguments:
                        current["function"]["arguments"] += delta_call.function.arguments

        provider_calls = [pending_calls[index] for index in sorted(pending_calls)]
        assistant_message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if provider_calls:
            # 先记录 assistant 提出的调用，再按原 ID 追加对应的 tool 结果消息。
            assistant_message["tool_calls"] = provider_calls
        messages.append(assistant_message)

        if not provider_calls:
            print()
            return

        if text_parts:
            print()
        for provider_call in provider_calls:
            try:
                raw_arguments = json.loads(provider_call["function"]["arguments"])
            except json.JSONDecodeError:
                # 转换为会被 Schema 拒绝的参数，使解析失败也进入统一审计入口。
                raw_arguments = {"_invalid_json": provider_call["function"]["arguments"]}
            result = await runtime.invoke(
                ToolCall(provider_call["id"], provider_call["function"]["name"], raw_arguments),
                context,
            )
            print(f"[tool_result] {result.tool_name} {result.code}")
            messages.append(result.to_tool_message())

    raise RuntimeError("Agent Loop 超过最大轮数 8")


def parse_args() -> argparse.Namespace:
    """选择离线验证或模型闭环入口；两种入口共用工具注册与治理策略。"""

    parser = argparse.ArgumentParser(description="Python 工具治理与权限状态机演示")
    parser.add_argument("--agent", action="store_true", help="使用 DeepSeek 运行真实 Agent Loop")
    parser.add_argument("--input", default="请查询订单 ord_1001 的状态和可退金额")
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(run_deepseek_agent(cli_args.input) if cli_args.agent else run_offline_demo())
