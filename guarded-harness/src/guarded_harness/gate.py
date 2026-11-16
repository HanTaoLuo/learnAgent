"""统一分发工具调用，管理参数、权限、重复动作、审批和持久化执行意图。"""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .sandbox import SandboxViolation
from .state_store import RunState


@dataclass(frozen=True)
class ToolSpec:
    """工具契约，指定精确参数类型、边界检查和风险级别。

    write 风险必须要求审批。preflight 仅校验，不执行副作用。
    handler 在放行后调用；写入工具还要求配置持久化与工作区指纹回调。
    """

    parameters: dict[str, type]
    handler: Callable[..., Any]
    preflight: Callable[..., Any] | None = None
    risk: str = "read"
    requires_approval: bool = False
    affects_workspace: bool = True


class Gate:
    """顺序分发工具请求，审批只能由宿主接口提交，不能由模型工具调用生成。

    写入执行前持久化 IN_FLIGHT，成功后记录结果。两次提交之间退出的操作必须
    在恢复时转为 UNKNOWN_RESULT，禁止自动重放。
    """

    def __init__(
        self,
        tools: dict[str, ToolSpec],
        state: RunState,
        audit: AuditLog,
        persist: Callable[[], None] | None = None,
        fingerprint: Callable[[], str] | None = None,
    ):
        """绑定注册表、运行状态、审计和宿主提供的持久化及版本计算接口。"""
        self.tools = tools
        self.state = state
        self.audit = audit
        self.persist = persist
        self.fingerprint = fingerprint
        self._call_index = None

    def reject(self, call_id: str, code: str, message: str) -> dict:
        """保存拒绝结果与计数后返回，保留原成功动作及原始调用 ID。"""
        outcome = {"ok": False, "tool_call_id": call_id, "error": {"code": code, "message": message}}
        action = self.state.actions.get(call_id) if isinstance(call_id, str) else None
        if action is not None and (
            action["status"] in {"RECEIVED", "READY"}
            or (action["status"] == "IN_FLIGHT" and action["risk"] == "read" and code == "SANDBOX_DENIED")
        ):
            # 只读执行期间的边界拒绝可确认结果；写入或测试仍保留 UNKNOWN_RESULT。
            action.update(status="REJECTED", result=outcome)
        self.state.rejections.append({
            "tool_call_id": call_id, "step_number": self.state.steps_used,
            "call_index": self._call_index, "result": outcome,
        })
        self.audit.record("rejected", call_id, rule=code, decision="deny", actor="gate", rejection_index=len(self.state.rejections) - 1)
        self._save()
        return outcome

    def _register_call(self, call_id: str, name: Any, arguments: Any) -> None:
        """登记新调用的原始意图；无效参数也必须能与持久化拒绝结果关联。"""
        try:
            json.dumps(arguments)
            captured = deepcopy(arguments)
        except (TypeError, ValueError):
            captured = {"invalid_type": type(arguments).__name__}
        spec = self.tools.get(name) if isinstance(name, str) else None
        self.state.seen_call_ids.append(call_id)
        self.state.actions[call_id] = {
            "tool_call_id": call_id, "tool": name if isinstance(name, str) else None,
            "arguments": captured, "risk": spec.risk if spec else "unknown", "status": "RECEIVED",
        }

    def _save(self) -> None:
        """在配置了持久化回调时同步完整状态，错误直接向上传播。"""
        if self.persist is not None:
            self.persist()

    @staticmethod
    def _signature(name: str, arguments: dict) -> str:
        """将工具名和排序后的参数编码成审批与重复检查使用的稳定签名。"""
        payload = json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _preflight(self, spec: ToolSpec, arguments: dict, call_id: str) -> None:
        """执行无副作用边界校验，并按原调用标识记录放行或拒绝。"""
        if spec.preflight is not None:
            try:
                spec.preflight(**arguments)
            except (SandboxViolation, OSError, ValueError):
                self.audit.record("preflight_checked", call_id, rule="execution_boundary", decision="deny", actor="sandbox")
                raise
            self.audit.record("preflight_checked", call_id, rule="execution_boundary", decision="allow", actor="sandbox")

    def _track_action(self, name: Any, arguments: Any) -> str | None:
        """按请求顺序计算连续动作；不同请求即使无效，也会打断先前连续序列。"""
        try:
            signature = self._signature(name, arguments)
        except (TypeError, ValueError):
            signature = None
        if signature is not None and signature == self.state.last_action:
            self.state.repeated_actions += 1
        else:
            self.state.last_action = signature
            self.state.repeated_actions = 1 if signature is not None else 0
        return signature

    def invalidate_workspace(self, call_id: str | None = None) -> None:
        """工作区变化时撤销旧审批和证据，保留待处理动作以供宿主核对。"""
        for approval in self.state.approvals.values():
            if isinstance(approval, dict):
                approval["status"] = "INVALIDATED"
        self.state.evidence.clear()
        for result in self.state.tests:
            result["valid"] = False
        if self.state.report_evidence is not None:
            self.state.report_evidence["valid"] = False
        self.state.status = "WAITING_REVIEW"
        self.state.stop_reason = "WORKSPACE_CHANGED"
        self.audit.record("workspace_changed", call_id, rule="workspace_version", decision="review", actor="gate")
        self._save()

    def dispatch(self, call_id: str, name: str, arguments: Any, *, batch_tools: tuple[str, ...] = (), call_index: int | None = None) -> dict:
        """校验并执行新调用；未批准的写操作返回 APPROVAL_REQUIRED 并暂停运行。

        被拒绝的调用 ID 同样登记。同一 ID 只能由 execute_pending 在显式批准后
        继续一次，模型再次提交同一调用始终得到 DUPLICATE_CALL。
        batch_tools/call_index 由宿主适配器传入，批量策略与记账均在本入口执行。
        工具计数在上限处停止增加；超额及终态请求仍保存拒绝结果，不进入 handler。
        """
        self._call_index = call_index
        valid_id = type(call_id) is str and bool(call_id.strip())
        duplicate = valid_id and call_id in self.state.seen_call_ids
        if valid_id and not duplicate:
            self._register_call(call_id, name, arguments)
        if self.state.status in {"WAITING_APPROVAL", "WAITING_REVIEW", "SUCCEEDED", "FAILED", "STOPPED"}:
            if duplicate:
                return self.reject(call_id, "DUPLICATE_CALL", "This call ID was already handled")
            return self.reject(call_id, "RUN_NOT_ACTIVE", "The run is paused or stopped")
        if self.state.max_tool_calls is not None and self.state.tool_calls_used >= self.state.max_tool_calls:
            self.state.status, self.state.stop_reason = "STOPPED", "MAX_TOOL_CALLS"
            return self.reject(call_id, "MAX_TOOL_CALLS", "Tool-call budget exhausted")
        self.state.tool_calls_used += 1
        signature = self._track_action(name, arguments)
        if not valid_id:
            return self.reject(call_id, "INVALID_CALL_ID", "A non-empty original call ID is required")
        if duplicate:
            return self.reject(call_id, "DUPLICATE_CALL", "This call ID was already handled")
        if type(name) is not str:
            return self.reject(call_id, "TOOL_NOT_ALLOWED", "Tool name must be a string")
        spec = self.tools.get(name)
        if spec is None:
            return self.reject(call_id, "TOOL_NOT_ALLOWED", f"Tool is not enabled: {name}")
        if not isinstance(arguments, dict) or set(arguments) != set(spec.parameters):
            return self.reject(call_id, "INVALID_ARGUMENTS", "Required parameter names do not match")
        if any(type(arguments[key]) is not expected for key, expected in spec.parameters.items()):
            return self.reject(call_id, "INVALID_ARGUMENTS", "Parameter types do not match")
        if spec.risk not in {"read", "write", "test", "final"} or (spec.risk == "write" and not spec.requires_approval):
            return self.reject(call_id, "RISK_NOT_ALLOWED", "Unsupported risk or unguarded write tool")

        if self.state.repeated_actions >= 3:
            return self.reject(call_id, "REPEATED_ACTION", "Third consecutive identical action blocked")
        try:
            self._preflight(spec, arguments, call_id)
        except (SandboxViolation, OSError, ValueError) as error:
            return self.reject(call_id, "SANDBOX_DENIED", str(error))
        if len(batch_tools) > 1 and any(
            isinstance(tool, str) and self.tools.get(tool)
            and (self.tools[tool].requires_approval or self.tools[tool].risk in {"test", "final"})
            for tool in batch_tools
        ):
            return self.reject(call_id, "PROTECTED_BATCH", "Approved writes, tests and final answers must each occupy a separate step")
        if spec.requires_approval and (self.persist is None or self.fingerprint is None):
            return self.reject(call_id, "DURABILITY_REQUIRED", "Write approval requires durable state and workspace versioning")

        action = {
            "tool_call_id": call_id,
            "tool": name,
            "arguments": deepcopy(arguments),
            "signature": signature,
            "risk": spec.risk,
            "status": "READY",
        }
        self.state.actions[call_id] = action
        if spec.requires_approval:
            current = self.fingerprint()
            if self.state.workspace_fingerprint is not None and current != self.state.workspace_fingerprint:
                self.invalidate_workspace(call_id)
                return self.reject(call_id, "WORKSPACE_CHANGED", "Workspace changed before approval")
            action["fingerprint"] = current
            action["status"] = "WAITING_APPROVAL"
            self.state.pending_action = deepcopy(action)
            self.state.approvals[call_id] = {
                "status": "PENDING", "signature": signature, "fingerprint": current, "actor": None
            }
            self.state.status = "WAITING_APPROVAL"
            self.state.stop_reason = "APPROVAL_REQUIRED"
            self.audit.record("approval_requested", call_id, rule="write_requires_approval", decision="wait", actor="gate", tool=name)
            self._save()
            return {"ok": False, "tool_call_id": call_id, "error": {"code": "APPROVAL_REQUIRED", "message": "Host approval is required"}}
        return self._execute(action, spec)

    def decide(self, call_id: str, approved: bool, actor: str) -> None:
        """宿主审批接口，绑定调用签名和工作区版本；不直接执行写入。

        模型工具注册表不暴露此接口。审批人必须由宿主提供，不能从模型消息推断。
        """
        if type(approved) is not bool or not isinstance(actor, str) or not actor.strip():
            raise ValueError("Approval requires an explicit boolean decision and actor")
        pending = self.state.pending_action
        if self.state.status != "WAITING_APPROVAL" or pending is None or pending["tool_call_id"] != call_id:
            raise ValueError("No matching pending approval")
        action = self.state.actions[call_id]
        approval = self.state.approvals[call_id]
        if approval["status"] != "PENDING":
            raise ValueError("Approval was already decided")
        if self._signature(action["tool"], action["arguments"]) != approval["signature"]:
            raise ValueError("Pending action parameters changed")
        if self.fingerprint() != approval["fingerprint"]:
            self.invalidate_workspace(call_id)
            raise ValueError("Workspace changed; approval is invalid")
        approval.update(status="APPROVED" if approved else "DENIED", actor=actor)
        self.audit.record("approval_decided", call_id, rule="host_approval", decision=approval["status"], actor=actor)
        if not approved:
            action["status"] = "DENIED"
            self.state.pending_action = None
            self.state.status = "STOPPED"
            self.state.stop_reason = "APPROVAL_DENIED"
        self._save()

    def execute_pending(self) -> dict:
        """继续执行已批准的原始调用，不增加模型工具请求计数或绕过去重规则。"""
        pending = self.state.pending_action
        if pending is None or self.state.status != "WAITING_APPROVAL":
            raise ValueError("No pending write to execute")
        call_id = pending["tool_call_id"]
        action = self.state.actions[call_id]
        approval = self.state.approvals[call_id]
        if approval["status"] != "APPROVED" or action["status"] != "WAITING_APPROVAL":
            raise ValueError("The pending write has not been approved")
        if self._signature(action["tool"], action["arguments"]) != approval["signature"]:
            raise ValueError("Approved parameters no longer match")
        if self.fingerprint() != approval["fingerprint"]:
            self.invalidate_workspace(call_id)
            return self.reject(call_id, "WORKSPACE_CHANGED", "Workspace changed after approval")
        spec = self.tools[action["tool"]]
        try:
            self._preflight(spec, action["arguments"], call_id)
        except (SandboxViolation, OSError, ValueError) as error:
            self.invalidate_workspace(call_id)
            return self.reject(call_id, "SANDBOX_DENIED", str(error))
        self.state.status = "RUNNING"
        self.state.stop_reason = None
        return self._execute(action, spec)

    def request_report_revision(self, call_id: str, feedback: str, actor: str) -> None:
        """宿主退回待审批报告并提供修订意见；源码写入不允许使用该继续路径。

        先明确拒绝原报告，保留其调用 ID 去重记录，再持久化修订反馈。
        不写报告、不增加模型预算，不自动允许新的报告请求。
        """
        pending = self.state.pending_action
        if pending is None or pending["tool_call_id"] != call_id or pending["tool"] != "write_report":
            raise ValueError("Only a pending report may be returned for revision")
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback.encode("utf-8")) > 2048:
            raise ValueError("Report feedback must contain 1 to 2048 UTF-8 bytes")
        self.decide(call_id, False, actor)
        self.state.approvals[call_id]["status"] = "REVISION_REQUESTED"
        self.state.actions[call_id]["result"] = {
            "ok": False, "tool_call_id": call_id,
            "error": {"code": "REPORT_REVISION_REQUIRED", "message": feedback},
        }
        self.state.status, self.state.stop_reason = "STOPPED", "REPORT_REVISION_REQUIRED"
        self.audit.record("report_revision_requested", call_id, rule="host_report_review", decision="revise", actor=actor)
        self._save()

    def _execute(self, action: dict, spec: ToolSpec) -> dict:
        """先提交执行意图，再调用 handler；写入异常一律保留未知结果供人工核对。"""
        call_id = action["tool_call_id"]
        action["status"] = "IN_FLIGHT"
        if spec.requires_approval:
            self.state.approvals[call_id]["status"] = "CONSUMED"
        self.audit.record("allowed", call_id, rule="registered_tool_and_schema", decision="allow", actor="gate", tool=action["tool"])
        self._save()
        try:
            result = spec.handler(**deepcopy(action["arguments"]))
        except Exception as error:
            if isinstance(error, SandboxViolation):
                self.audit.record("execution_boundary_denied", call_id, rule="execution_boundary", decision="deny", actor="sandbox", error_type=type(error).__name__)
                if spec.risk == "read":
                    return self.reject(call_id, "SANDBOX_DENIED", str(error))
            if spec.risk in {"write", "test"}:
                action["status"] = "UNKNOWN_RESULT"
                self.state.status = "WAITING_REVIEW"
                self.state.stop_reason = "UNKNOWN_RESULT"
                outcome = self.reject(call_id, "UNKNOWN_RESULT", "Write outcome needs host reconciliation")
            elif isinstance(error, (OSError, UnicodeError, ValueError)):
                code = "SANDBOX_DENIED" if isinstance(error, SandboxViolation) else "EXECUTION_FAILED"
                action["status"] = "FAILED"
                outcome = {"ok": False, "tool_call_id": call_id, "error": {"code": code, "message": str(error)}}
            else:
                raise
            action["result"] = outcome
            self.audit.record("execution_failed", call_id, rule="handler_result", decision=action["status"], actor="gate", error_type=type(error).__name__)
            self._save()
            return outcome
        if spec.risk == "write":
            # 只有已确认成功的受控写入才能更新恢复时比较的工作区版本。
            self.state.workspace_fingerprint = self.fingerprint()
            self.state.pending_action = None
            if spec.affects_workspace:
                self.state.evidence.clear()
                for test in self.state.tests:
                    test["valid"] = False
        outcome = {"ok": True, "tool_call_id": call_id, "result": result}
        action.update(status="SUCCEEDED", result=outcome)
        self._save()
        self.audit.record("executed", call_id, rule="handler_result", decision="success", actor="gate", tool=action["tool"])
        return outcome
