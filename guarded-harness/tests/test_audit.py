"""验证审计追加语义、事件顺序及调用链关联字段。"""

import json
import pytest
from guarded_harness.audit import AuditLog
from guarded_harness.gate import Gate, ToolSpec
from guarded_harness.state_store import RunState
from unittest.mock import Mock


def test_audit_appends_and_preserves_correlation_fields(tmp_path):
    """拒绝和成功请求均追加独立事件，原记录内容及公共关联字段保持完整。"""
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path, "run-1")
    state = RunState("run-1", "read", str(tmp_path), 4)
    gate = Gate({"read_file": ToolSpec({"path": str}, Mock(return_value="source"))}, state, audit)
    gate.dispatch("bad-id", "read_file", {})
    original = path.read_bytes()
    gate.dispatch("good-id", "read_file", {"path": "a"})
    assert path.read_bytes().startswith(original)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["event"] for record in records] == ["rejected", "allowed", "executed"]
    assert records[0]["tool_call_id"] == "bad-id"
    assert records[0]["toolCallId"] == "bad-id"
    assert all(record["run_id"] == "run-1" for record in records)
    assert all({"rule", "decision", "actor", "tool_call_id"} <= record.keys() for record in records)


def test_approval_and_execution_are_linked_by_original_call_id(workspace, tmp_path):
    """A2：宿主审批、Gate 放行和执行结果共享运行与原始调用标识。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    model = FakeModel([
        tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'VALUE = 1\n'}),
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('final-1', 'final_answer', {'answer': 'Updated calculator.py.'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update file')
    harness.decide('write-1', True, 'reviewer-1')
    harness.resume()
    records = [json.loads(line) for line in (tmp_path / 'run' / 'audit.jsonl').read_text().splitlines()]
    linked = [item for item in records if item['tool_call_id'] == 'write-1']
    assert [item['event'] for item in linked] == ['preflight_checked', 'approval_requested', 'approval_decided', 'preflight_checked', 'allowed', 'executed']
    assert linked[2]['actor'] == 'reviewer-1'
    assert all(item['run_id'] == harness.state.run_id for item in linked)


def test_sensitive_symlink_denial_records_sandbox_and_gate(workspace, tmp_path):
    """S2/A1：真实落点指向敏感文件时，边界拒绝与 Gate 拒绝共享原调用 ID。"""
    from dataclasses import replace
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    (workspace / '.env').write_text('PRIVATE=value')
    (workspace / 'alias.txt').symlink_to(workspace / '.env')
    harness = Harness(workspace, tmp_path / 'run', FakeModel([]))
    spec = harness.gate.tools['read_file']
    handler = Mock(wraps=spec.handler)
    harness.gate.tools['read_file'] = replace(spec, handler=handler)
    result = harness.gate.dispatch('sensitive-link', 'read_file', {'path': 'alias.txt'})
    assert result['error']['code'] == 'SANDBOX_DENIED'
    handler.assert_not_called()
    events = [json.loads(line) for line in harness.audit.path.read_text().splitlines()]
    linked = [item for item in events if item['tool_call_id'] == 'sensitive-link']
    assert [(item['actor'], item['decision']) for item in linked] == [('sandbox', 'deny'), ('gate', 'deny')]


@pytest.mark.parametrize("name,arguments", [
    ("list_files", {"path": "."}),
    ("find_files", {"pattern": "*.py"}),
    ("search_code", {"query": "PRIVATE_FIXTURE", "path": "."}),
])
def test_runtime_discovery_boundary_denial_is_audited_and_persisted(workspace, tmp_path, name, arguments):
    """前置检查后替换根目录，执行检查仍拒绝，并保存 Sandbox 与 Gate 的拒绝链。"""
    from dataclasses import replace
    from guarded_harness.checkpoint import Checkpoint
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    from guarded_harness.state_store import StateStore

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "secret.py"
    marker.write_text("PRIVATE_FIXTURE = 1\n")
    original_root = tmp_path / "original-root"
    harness = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("runtime-boundary", name, arguments),
    ]), max_steps=1)
    spec = harness.gate.tools[name]

    def change_root_after_preflight(**kwargs):
        workspace.rename(original_root)
        workspace.symlink_to(outside, target_is_directory=True)
        return spec.handler(**kwargs)

    harness.gate.tools[name] = replace(spec, handler=change_root_after_preflight)
    state = harness.run("Inspect repository")
    workspace.unlink()
    original_root.rename(workspace)
    outcome = state.actions["runtime-boundary"]["result"]
    assert outcome["error"]["code"] == "SANDBOX_DENIED"
    assert "PRIVATE_FIXTURE" not in state.steps[0]["observations"]
    assert marker.read_text() == "PRIVATE_FIXTURE = 1\n"
    events = [json.loads(line) for line in harness.audit.path.read_text().splitlines()]
    linked = [row for row in events if row["tool_call_id"] == "runtime-boundary"]
    assert any(row["actor"] == "sandbox" and row["decision"] == "deny" and row["rule"] == "execution_boundary" for row in linked)
    assert any(row["actor"] == "gate" and row["event"] == "rejected" and row["rule"] == "SANDBOX_DENIED" for row in linked)
    assert not any(row["event"] == "executed" for row in linked)
    saved = Checkpoint(harness.run_dir / "checkpoint.json").load()
    assert saved == StateStore(harness.run_dir / "state.json").load()
    assert saved.actions["runtime-boundary"]["status"] == "REJECTED"
    assert saved.rejections[0]["result"] == outcome
    recovered = Harness.load(harness.run_dir, FakeModel([]))
    assert recovered.state.actions["runtime-boundary"]["result"] == outcome
    assert recovered.state.steps_used == recovered.state.tool_calls_used == 1


def test_file_open_symlink_denial_is_classified_as_boundary(workspace, tmp_path, monkeypatch):
    """实际 open 拒绝检查后替换的文件链接，不能退化成无法识别边界的普通执行失败。"""
    import os
    from dataclasses import replace
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    outside = tmp_path / "outside.txt"
    outside.write_text("PRIVATE_FIXTURE")
    target = workspace / "calculator.py"
    harness = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("file-swap", "read_file", {"path": "calculator.py"}),
    ]), max_steps=1)
    spec = harness.gate.tools["read_file"]
    active = {"handler": False, "swapped": False}
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if active["handler"] and not active["swapped"] and path == "calculator.py" and "dir_fd" in kwargs:
            target.unlink()
            target.symlink_to(outside)
            active["swapped"] = True
        return original_open(path, flags, *args, **kwargs)

    def read_after_preflight(**kwargs):
        active["handler"] = True
        return spec.handler(**kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    harness.gate.tools["read_file"] = replace(spec, handler=read_after_preflight)
    state = harness.run("Read source")
    outcome = state.actions["file-swap"]["result"]
    assert active["swapped"] and outcome["error"]["code"] == "SANDBOX_DENIED"
    assert state.actions["file-swap"]["status"] == "REJECTED"
    assert state.rejections[0]["result"] == outcome
    events = [json.loads(line) for line in harness.audit.path.read_text().splitlines()]
    assert any(row["tool_call_id"] == "file-swap" and row["actor"] == "sandbox" and row["decision"] == "deny" for row in events)
    assert outside.read_text() == "PRIVATE_FIXTURE"


@pytest.mark.parametrize("name", ["write_file", "run_test"])
def test_side_effect_boundary_failure_retains_unknown_result(workspace, tmp_path, unit_commands, name):
    """边界异常发生前已有写入或测试副作用时，只增加拒绝审计，不允许自动重放。"""
    from dataclasses import replace
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    from guarded_harness.sandbox import SandboxViolation

    arguments = {"path": "calculator.py", "content": "VALUE = 123\n"} if name == "write_file" else {"name": "unit"}
    harness = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("uncertain", name, arguments),
    ]), task_kind="general", writable_paths=("calculator.py",), test_commands=unit_commands)
    spec = harness.gate.tools[name]

    def fail_after_side_effect(**kwargs):
        spec.handler(**kwargs)
        raise SandboxViolation("Boundary changed after external execution")

    harness.gate.tools[name] = replace(spec, handler=fail_after_side_effect)
    state = harness.run("Execute configured action")
    if name == "write_file":
        harness.decide("uncertain", True, "operator")
        state = harness.resume()
        assert state.approvals["uncertain"]["status"] == "CONSUMED"
    assert state.status == "WAITING_REVIEW" and state.stop_reason == "UNKNOWN_RESULT"
    assert state.actions["uncertain"]["status"] == "UNKNOWN_RESULT"
    assert state.rejections[0]["result"]["error"]["code"] == "UNKNOWN_RESULT"
    events = [json.loads(line) for line in harness.audit.path.read_text().splitlines()]
    assert any(row["tool_call_id"] == "uncertain" and row["actor"] == "sandbox" and row["decision"] == "deny" for row in events)
    recovered = Harness.load(harness.run_dir, FakeModel([]))
    assert recovered.resume().stop_reason == "UNKNOWN_RESULT"
    assert len(recovered.state.writes) == (1 if name == "write_file" else 0)
    assert len(recovered.state.tests) == (1 if name == "run_test" else 0)
