"""验证工具分发拒绝条件及 handler 的实际执行次数。"""

import pytest
from guarded_harness.audit import AuditLog
from guarded_harness.gate import Gate, ToolSpec
from guarded_harness.sandbox import Sandbox
from guarded_harness.state_store import RunState
from unittest.mock import Mock


def make_gate(tmp_path, handler, preflight=None):
    """以可观测 handler 创建独立 Gate，审计记录保存在用例临时目录。"""
    state = RunState("run-1", "read source", str(tmp_path), 4)
    return Gate(
        {"read_file": ToolSpec({"path": str}, handler, preflight)},
        state,
        AuditLog(tmp_path / "audit.jsonl", state.run_id),
    )


@pytest.mark.parametrize("arguments", [{}, {"path": 42}, {"path": "a", "extra": "b"}, None, "a"])
def test_invalid_arguments_never_enter_handler(tmp_path, arguments):
    """参数缺失、额外字段或类型错误时保留调用 ID，并阻止 handler 执行。"""
    handler = Mock()
    result = make_gate(tmp_path, handler).dispatch("original-id", "read_file", arguments)
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert result["tool_call_id"] == "original-id"
    handler.assert_not_called()


def test_third_identical_action_is_blocked(tmp_path):
    """使用不同调用 ID 重复相同动作，第三次应在 handler 执行前被拒绝。"""
    handler = Mock(return_value="source")
    gate = make_gate(tmp_path, handler)
    results = [gate.dispatch(f"call-{i}", "read_file", {"path": "a"}) for i in range(3)]
    assert [result["ok"] for result in results] == [True, True, False]
    assert results[-1]["error"]["code"] == "REPEATED_ACTION"
    assert handler.call_count == 2


def test_same_call_id_is_not_executed_twice(tmp_path):
    """同一调用 ID 再次提交时不重复执行，独立于动作签名的连续计数规则。"""
    handler = Mock(return_value="source")
    gate = make_gate(tmp_path, handler)
    assert gate.dispatch("same-id", "read_file", {"path": "a"})["ok"]
    result = gate.dispatch("same-id", "read_file", {"path": "a"})
    assert result["error"]["code"] == "DUPLICATE_CALL"
    handler.assert_called_once()


@pytest.mark.parametrize("name,arguments", [("unknown", {}), ("read_file", {}), ("read_file", {"path": 42})])
def test_different_invalid_request_breaks_consecutive_sequence(tmp_path, name, arguments):
    """G2：A、A、无效 B、A 不是连续三次 A，后续第三次 A 仍应被拦截。"""
    handler = Mock(return_value='source')
    gate = make_gate(tmp_path, handler)
    assert gate.dispatch('a1', 'read_file', {'path': 'a'})['ok']
    assert gate.dispatch('a2', 'read_file', {'path': 'a'})['ok']
    assert not gate.dispatch('b', name, arguments)['ok']
    assert gate.dispatch('a3', 'read_file', {'path': 'a'})['ok']
    assert gate.dispatch('a4', 'read_file', {'path': 'a'})['ok']
    assert gate.dispatch('a5', 'read_file', {'path': 'a'})['error']['code'] == 'REPEATED_ACTION'
    assert handler.call_count == 4


def test_escape_is_blocked_before_handler(tmp_path, workspace):
    """在工作区外创建真实文件，验证 preflight 阻止越界请求进入 handler。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    handler = Mock()
    gate = make_gate(tmp_path, handler, Sandbox(workspace).resolve_read)
    result = gate.dispatch("escape", "read_file", {"path": "../outside.txt"})
    assert result["error"]["code"] == "SANDBOX_DENIED"
    handler.assert_not_called()


@pytest.mark.parametrize("name,arguments", [
    ("list_files", {"path": "../"}),
    ("list_files", {"path": "artifacts/../../.env"}),
    ("list_files", {"path": ".env"}),
    ("list_files", {"path": "outside-link"}),
    ("list_files", {"path": "missing-directory"}),
    ("find_files", {"pattern": "../*"}),
    ("find_files", {"pattern": ""}),
    ("find_files", {"pattern": "/tmp/*"}),
    ("find_files", {"pattern": "x" * 257}),
    ("search_code", {"query": "add", "path": "../"}),
    ("search_code", {"query": "add", "path": ".env"}),
    ("search_code", {"query": "add", "path": "outside-link"}),
    ("search_code", {"query": "", "path": "."}),
    ("search_code", {"query": "x" * 257, "path": "."}),
])
def test_discovery_denial_precedes_handler_and_is_durable(workspace, tmp_path, name, arguments):
    """发现工具的非法输入不进入 handler，原 ID、拒绝原因及预算在安全边界落盘。"""
    import json
    from dataclasses import replace
    from guarded_harness.checkpoint import Checkpoint
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    from guarded_harness.state_store import StateStore

    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "outside-link").symlink_to(outside, target_is_directory=True)
    (workspace / ".env").write_text("HARMLESS_FIXTURE_VALUE=1\n")
    run_dir = tmp_path / "run"
    harness = Harness(workspace, run_dir, FakeModel([
        tool_message("denied-discovery", name, arguments),
    ]), max_steps=1)
    spec = harness.gate.tools[name]
    handler = Mock(wraps=spec.handler)
    harness.gate.tools[name] = replace(spec, handler=handler)
    state = harness.run("Inspect repository")
    handler.assert_not_called()
    outcome = state.actions["denied-discovery"]["result"]
    assert outcome["error"]["code"] == "SANDBOX_DENIED"
    assert outcome["tool_call_id"] == "denied-discovery"
    saved = Checkpoint(run_dir / "checkpoint.json").load()
    assert saved == StateStore(run_dir / "state.json").load()
    assert saved.actions["denied-discovery"]["status"] == "REJECTED"
    assert saved.rejections[0]["result"] == outcome
    assert saved.steps_used == saved.tool_calls_used == 1
    events = [json.loads(line) for line in harness.audit.path.read_text().splitlines()]
    linked = [row for row in events if row["tool_call_id"] == "denied-discovery"]
    assert [(row["actor"], row["decision"]) for row in linked] == [("sandbox", "deny"), ("gate", "deny")]
    assert all(row["run_id"] == state.run_id and row["toolCallId"] == "denied-discovery" and row["rule"] for row in linked)


@pytest.mark.parametrize("arguments", [
    {"path": "artifacts/../../.env", "start_line": 1, "end_line": 2},
    {"path": "calculator.py", "start_line": 0, "end_line": 2},
    {"path": "calculator.py", "start_line": 3, "end_line": 2},
    {"path": "calculator.py", "start_line": 1, "end_line": 2001},
    {"path": "calculator.py", "start_line": True, "end_line": 2},
])
def test_range_permission_and_bounds_precede_handler(workspace, tmp_path, arguments):
    """新增读取接口仍遵守 G1/S1，非法路径或范围不进入 handler。"""
    from dataclasses import replace
    from guarded_harness.harness import Harness
    from tests.support import FakeModel
    harness = Harness(workspace, tmp_path / "run", FakeModel([]))
    spec = harness.gate.tools["read_range"]
    handler = Mock(wraps=spec.handler)
    harness.gate.tools["read_range"] = replace(spec, handler=handler)
    outcome = harness.gate.dispatch("invalid-range", "read_range", arguments)
    assert not outcome["ok"] and outcome["tool_call_id"] == "invalid-range"
    handler.assert_not_called()


@pytest.mark.parametrize("directory", [".", "../", ".git", "linked"])
def test_directory_permission_cannot_grant_root_sensitive_or_linked_paths(workspace, tmp_path, directory):
    """宿主目录范围也接受路径校验，不能以目录权限绕过敏感路径规则。"""
    from guarded_harness.harness import Harness
    from tests.support import FakeModel
    (workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        Harness(workspace, tmp_path / "run", FakeModel([]), writable_directories=(directory,), task_kind="general")
    assert not (tmp_path / "run").exists()


def test_write_tools_are_not_enabled_in_read_only_configuration(tmp_path):
    """只读注册表不允许通过自报工具名称获得文件写入能力。"""
    result = make_gate(tmp_path, Mock()).dispatch("write-1", "write_file", {"path": "a"})
    assert result["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_unapproved_write_pauses_without_entering_handler(workspace, tmp_path):
    """G3：待审批动作持久化，但 handler 调用次数和目标内容均保持不变。"""
    from dataclasses import replace
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    from guarded_harness.checkpoint import Checkpoint

    original = (workspace / 'calculator.py').read_bytes()
    model = FakeModel([tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'VALUE = 42\n'})])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    spec = harness.gate.tools['write_file']
    handler = Mock(wraps=spec.handler)
    harness.gate.tools['write_file'] = replace(spec, handler=handler)
    state = harness.run('Update the allowed file')
    assert state.status == 'WAITING_APPROVAL'
    assert state.stop_reason == 'APPROVAL_REQUIRED'
    assert state.pending_action['tool_call_id'] == 'write-1'
    assert state.approvals['write-1']['status'] == 'PENDING'
    assert Checkpoint(tmp_path / 'run' / 'checkpoint.json').load().pending_action == state.pending_action
    assert (workspace / 'calculator.py').read_bytes() == original
    handler.assert_not_called()
    assert model.calls == 1


def test_approved_call_executes_once_and_replay_is_rejected(workspace, tmp_path):
    """G4：批准只允许原调用执行一次，模型重复提交不能再次进入 handler。"""
    from dataclasses import replace
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    content = 'def add(a, b):\n    return a + b\n'
    arguments = {'path': 'calculator.py', 'content': content}
    model = FakeModel([
        tool_message('write-1', 'write_file', arguments),
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('final-1', 'final_answer', {'answer': 'calculator.py has been updated.'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    spec = harness.gate.tools['write_file']
    handler = Mock(wraps=spec.handler)
    harness.gate.tools['write_file'] = replace(spec, handler=handler)
    harness.run('Update the allowed file')
    harness.decide('write-1', True, 'operator')
    assert (workspace / 'calculator.py').read_text() != content
    state = harness.resume()
    assert state.status == 'SUCCEEDED'
    assert state.steps_used == 3
    assert state.tool_calls_used == 3
    assert state.approvals['write-1']['status'] == 'CONSUMED'
    assert (workspace / 'calculator.py').read_text() == content
    result = harness.gate.dispatch('write-1', 'write_file', arguments)
    assert result['error']['code'] == 'DUPLICATE_CALL'
    assert harness.state.actions['write-1']['status'] == 'SUCCEEDED'
    assert harness.state.actions['write-1']['result']['ok']
    assert harness.state.approvals['write-1']['status'] == 'CONSUMED'
    handler.assert_called_once()


def test_rejection_persistence_failure_is_not_reported_as_a_committed_result(tmp_path):
    """拒绝状态提交失败时向宿主报告错误，不返回仿佛已持久化的业务结果。"""
    handler = Mock()
    gate = make_gate(tmp_path, handler)
    gate.persist = Mock(side_effect=OSError('fixture storage failure'))
    with pytest.raises(OSError, match='fixture storage failure'):
        gate.dispatch('invalid', 'read_file', {})
    handler.assert_not_called()


def test_approval_denial_stops_without_write(workspace, tmp_path):
    """显式拒绝审批终止运行，不把拒绝结果解释为允许执行。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    original = (workspace / 'calculator.py').read_bytes()
    model = FakeModel([tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'X = 1\n'})])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update file')
    harness.decide('write-1', False, 'operator')
    assert harness.state.status == 'STOPPED'
    assert harness.state.stop_reason == 'APPROVAL_DENIED'
    assert (workspace / 'calculator.py').read_bytes() == original
    with pytest.raises(RuntimeError):
        harness.resume()


def test_write_path_outside_explicit_allowlist_never_requests_approval(workspace, tmp_path):
    """路径权限先于审批检查，不允许借审批扩大写入能力。"""
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    harness = Harness(workspace, tmp_path / 'run', FakeModel([]), writable_paths=('calculator.py',), task_kind="general")
    result = harness.gate.dispatch('write-1', 'write_file', {'path': 'README.md', 'content': 'changed'})
    assert result['error']['code'] == 'SANDBOX_DENIED'
    assert not harness.state.approvals
    assert not harness.state.writes


def test_approval_cannot_be_reused_with_modified_parameters(workspace, tmp_path):
    """审批签名绑定原始工具参数，审批前修改动作内容会被拒绝。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    model = FakeModel([tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'X = 1\n'})])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update file')
    harness.state.actions['write-1']['arguments']['content'] = 'X = 2\n'
    with pytest.raises(ValueError, match='parameters changed'):
        harness.decide('write-1', True, 'operator')
    assert not harness.state.writes


def test_run_test_outside_configuration_never_enters_handler(workspace, tmp_path, unit_commands):
    """S3：Gate 调用前置命令校验，非法别名不进入测试 handler。"""
    from dataclasses import replace
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    harness = Harness(workspace, tmp_path / 'run', FakeModel([]), test_commands=unit_commands, task_kind="general")
    spec = harness.gate.tools['run_test']
    handler = Mock(wraps=spec.handler)
    harness.gate.tools['run_test'] = replace(spec, handler=handler)
    outcome = harness.gate.dispatch('test-1', 'run_test', {'name': 'unconfigured'})
    assert outcome['error']['code'] == 'SANDBOX_DENIED'
    assert not harness.state.tests
    handler.assert_not_called()


def test_repair_write_requires_real_initial_failure(workspace, tmp_path, unit_commands):
    """修复任务不能跳过初始测试直接修改文件，预检查失败不请求审批。"""
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    harness = Harness(workspace, tmp_path / 'run', FakeModel([]), task_kind='repair', writable_paths=('calculator.py',), test_commands=unit_commands)
    outcome = harness.gate.dispatch('write-1', 'write_file', {'path': 'calculator.py', 'content': 'def add(a,b): return a+b\n'})
    assert outcome['error']['code'] == 'SANDBOX_DENIED'
    assert 'initial failure' in outcome['error']['message']
    assert not harness.state.approvals


def test_exhausted_budget_does_not_reopen_a_successful_run(workspace, tmp_path):
    """终态优先于预算检查，额外调用不能把已成功运行改写为预算停止。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    model = FakeModel([
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('final', 'final_answer', {'answer': 'Done'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, max_tool_calls=2, task_kind="general")
    assert harness.run('Read source').status == 'SUCCEEDED'
    outcome = harness.gate.dispatch('extra', 'read_file', {'path': 'calculator.py'})
    assert outcome['error']['code'] == 'RUN_NOT_ACTIVE'
    assert harness.state.status == 'SUCCEEDED'
    assert harness.state.tool_calls_used == 2


def test_report_revision_interface_cannot_resume_denied_source_write(workspace, tmp_path):
    """报告退回接口不能扩大到源码写入，也不能使被拒绝的写入重新进入执行。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    model = FakeModel([tool_message('write', 'write_file', {'path': 'calculator.py', 'content': 'X = 1\n'})])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update source')
    with pytest.raises(ValueError, match='Only a pending report'):
        harness.request_report_revision('write', 'Try another source change', 'reviewer')
    assert harness.state.status == 'WAITING_APPROVAL'
    harness.decide('write', False, 'reviewer')
    with pytest.raises(RuntimeError, match='cannot be resumed'):
        harness.resume()
    assert not harness.state.writes
