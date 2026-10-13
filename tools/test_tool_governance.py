"""转账工具的治理链路回归测试。

所有工具调用经过 ToolRuntime.invoke，覆盖参数、权限、预检、审批、执行、脱敏
和审计的真实组合。直接调用 handler 无法证明这些边界按预期生效。

用例观察错误码、授权动作、余额及审计事件，固定外部行为而非内部实现步骤。
账户快照在每个用例结束后还原，避免成功扣款影响其他场景的余额判断。
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

import pytest

import tool_governance as demo

TRANSFER = "transfer"
APPROVAL_ID = "approval_transfer"
FROM_ACCOUNT = "ACC-A-123456"  # tenant_a，余额 100000.0
TO_ACCOUNT = "ACC-A-654321"  # tenant_a，余额 5000.0
SMALL_ACCOUNT = "ACC-A-888888"  # tenant_a，余额 20000.0
SLEEP_SECONDS = 3.0  # 故障注入延迟；执行超时必须早于此时刻，才能阻止后续扣款。


def async_test(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """为每个用例创建独立事件循环；保留函数签名，使 pytest 能注入夹具。"""

    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return run


def transfer_context(**overrides: Any) -> demo.ExecutionContext:
    """构造可信基准上下文，并允许用例独立覆盖权限、白名单或模式。"""

    defaults: dict[str, Any] = {
        "permissions": frozenset({"order:read", "refund:create", "shell:run", "transfer:execute"}),
        "allowed_tools": frozenset({"get_order", "create_refund", "run_shell", "transfer"}),
    }
    return demo.base_context(**{**defaults, **overrides})


def accounts() -> dict[tuple[str, str], float]:
    """检查账户存储是否初始化，将配置缺失定位为明确的断言失败。"""

    store = getattr(demo, "ACCOUNTS", None)
    assert isinstance(store, dict), "任务 1 未完成：tool_governance_demo.ACCOUNTS 不存在"
    return store


def balance(account: str, tenant_id: str = "tenant_a") -> float:
    """按租户读取业务余额，检查真实状态而非仅相信处理器返回的成功标志。"""

    store = accounts()
    assert (tenant_id, account) in store, f"任务 1 未完成：ACCOUNTS 缺少 {(tenant_id, account)}"
    return store[(tenant_id, account)]


def approve(approvals: demo.ApprovalStore, arguments: Mapping[str, Any]) -> None:
    """对具体业务参数签发一次性审批；修改账户或金额后原凭证应失效。"""

    approvals.approve(APPROVAL_ID, transfer_context(), TRANSFER, dict(arguments))


async def transfer(
    runtime: demo.ToolRuntime,
    tool_call_id: str,
    arguments: Mapping[str, Any],
    **context_overrides: Any,
) -> demo.ToolResult:
    """统一走运行时入口，确保调用经过参数校验、授权、审批与审计。"""

    return await runtime.invoke(
        demo.ToolCall(tool_call_id, TRANSFER, arguments),
        transfer_context(**context_overrides),
    )


@pytest.fixture(autouse=True)
def isolated_accounts() -> Iterator[None]:
    """快照并原地还原共享账户字典，既隔离用例又保留原有字典引用。"""

    store = getattr(demo, "ACCOUNTS", None)
    snapshot = dict(store) if isinstance(store, dict) else None
    demo.reset_side_effects()
    yield
    if snapshot is not None:
        store.clear()
        store.update(snapshot)


@async_test
async def test_transfer_rejects_injected_arguments_and_invalid_amount() -> None:
    """验证额外身份字段、非法账号和金额均在业务执行之前被 Schema 拒绝。"""

    runtime, _approvals, _audit = demo.build_runtime()
    valid = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 100.0}

    injected = await transfer(runtime, "call_tr_inject", {**valid, "approved": True, "user_id": "u_admin"})
    assert injected.ok is False
    assert injected.code == "INVALID_ARGUMENT"

    malformed = await transfer(runtime, "call_tr_format", {**valid, "from_account": "ACC-123456"})
    assert malformed.code == "INVALID_ARGUMENT"

    zero = await transfer(runtime, "call_tr_zero", {**valid, "amount": 0.0})
    assert zero.code == "INVALID_ARGUMENT"

    over_schema = await transfer(runtime, "call_tr_schema", {**valid, "amount": 100_000.1})
    assert over_schema.code == "INVALID_ARGUMENT"

    # 错误码之外还检查余额，防止先产生副作用再返回参数错误。
    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_precheck_blocks_over_limit_and_insufficient_balance() -> None:
    """验证禁止区间和余额不足均先于副作用执行，审批不能覆盖业务拒绝。"""

    runtime, approvals, _audit = demo.build_runtime()

    over_limit = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 60_000.0}
    approve(approvals, over_limit)
    rejected = await transfer(runtime, "call_tr_limit", over_limit, approval_id=APPROVAL_ID)
    assert rejected.ok is False
    assert rejected.action is demo.DecisionAction.DENY
    assert rejected.code == "EXCEED_LIMIT"

    short = {"from_account": SMALL_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 30_000.0}
    approve(approvals, short)
    rejected = await transfer(runtime, "call_tr_balance", short, approval_id=APPROVAL_ID)
    assert rejected.action is demo.DecisionAction.DENY
    assert rejected.code == "INSUFFICIENT_BALANCE"

    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(SMALL_ACCOUNT) == 20_000.0
    assert balance(TO_ACCOUNT) == 5_000.0


@async_test
async def test_transfer_is_denied_without_permission_or_whitelist_and_in_plan_mode() -> None:
    """验证审批仅允许具体动作，不能替代业务权限、工具白名单或只读模式。"""

    runtime, approvals, _audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_000.0}
    approve(approvals, arguments)
    approved: dict[str, Any] = {"approval_id": APPROVAL_ID}

    no_permission = await transfer(
        runtime,
        "call_tr_rbac",
        arguments,
        permissions=frozenset({"order:read", "refund:create", "shell:run"}),
        **approved,
    )
    assert no_permission.action is demo.DecisionAction.DENY
    assert no_permission.code == "PERMISSION_DENIED"

    not_whitelisted = await transfer(
        runtime,
        "call_tr_whitelist",
        arguments,
        allowed_tools=frozenset({"get_order", "create_refund", "run_shell"}),
        **approved,
    )
    assert not_whitelisted.code == "TOOL_NOT_ALLOWED"

    plan_mode = await transfer(
        runtime,
        "call_tr_plan",
        arguments,
        mode=demo.PermissionMode.PLAN,
        **approved,
    )
    assert plan_mode.code == "PLAN_MODE_DENIED"

    assert balance(FROM_ACCOUNT) == 100_000.0


@async_test
async def test_transfer_requires_approval_bound_to_arguments_and_masks_accounts() -> None:
    """验证审批参数绑定、一次性核销、余额变更，以及模型结果中的账号脱敏。"""

    runtime, approvals, audit = demo.build_runtime()
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 1_200.0}

    # 未取得审批时只返回 CONFIRM，账户余额保持不变。
    pending = await transfer(runtime, "call_tr_000001", arguments)
    assert pending.ok is False
    assert pending.action is demo.DecisionAction.CONFIRM
    assert pending.code == "APPROVAL_REQUIRED"
    assert balance(FROM_ACCOUNT) == 100_000.0

    approve(approvals, arguments)
    # 改变金额必须取得新审批；不匹配的调用也不能消耗原先的有效凭证。
    tampered = await transfer(
        runtime, "call_tr_000002", {**arguments, "amount": 9_000.0}, approval_id=APPROVAL_ID
    )
    assert tampered.action is demo.DecisionAction.CONFIRM
    assert tampered.code == "APPROVAL_REQUIRED"

    # 原参数仍可执行，返回账号用于人工核对，但不暴露完整账号。
    executed = await transfer(runtime, "call_tr_000003", arguments, approval_id=APPROVAL_ID)
    assert executed.ok is True
    assert executed.action is demo.DecisionAction.ALLOW
    assert executed.code == "OK"
    assert executed.content["txn_id"] == "000003"
    assert executed.content["amount"] == 1_200.0
    assert executed.content["status"] == "accepted"
    assert executed.content["from"] == "ACC-A-****3456"
    assert executed.content["to"] == "ACC-A-****4321"
    assert balance(FROM_ACCOUNT) == 98_800.0
    assert balance(TO_ACCOUNT) == 6_200.0

    # 决策和执行分别记录，确认两次审批拦截之后仅发生一次真实执行。
    assert [(record.phase, record.code) for record in audit.records] == [
        ("decision", "APPROVAL_REQUIRED"),
        ("decision", "APPROVAL_REQUIRED"),
        ("decision", "APPROVED"),
        ("execution", "OK"),
    ]

    # 已核销审批不能重放，即使参数与先前成功调用完全一致。
    replay = await transfer(runtime, "call_tr_000004", arguments, approval_id=APPROVAL_ID)
    assert replay.action is demo.DecisionAction.CONFIRM


@async_test
async def test_transfer_timeout_is_reported_as_unknown_and_leaves_balances_untouched() -> None:
    """验证非幂等写超时返回未知结果；当前故障注入点位于扣款之前。"""

    runtime, approvals, audit = demo.build_runtime()
    # 大于 80000 的金额避开禁止区间，经余额检查与审批后进入延迟分支。
    arguments = {"from_account": FROM_ACCOUNT, "to_account": TO_ACCOUNT, "amount": 90_000.0}
    approve(approvals, arguments)

    started = time.perf_counter()
    result = await transfer(runtime, "call_tr_timeout", arguments, approval_id=APPROVAL_ID)
    elapsed = time.perf_counter() - started

    # 同时检查耗时与余额：等待被超时中断，并且尚未执行扣款。
    assert result.ok is False
    assert result.code == "TIMEOUT_UNKNOWN"
    assert elapsed < SLEEP_SECONDS
    assert balance(FROM_ACCOUNT) == 100_000.0
    assert balance(TO_ACCOUNT) == 5_000.0
    assert audit.records[-1].phase == "execution"
    assert audit.records[-1].code == "TIMEOUT_UNKNOWN"
