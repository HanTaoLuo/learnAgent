"""通过确定性模型替身验证实际 Agent 循环和运行终态。"""

import json
import pytest
import requests
from dataclasses import replace
from tests.support import FakeModel, tool_message
from guarded_harness.harness import CompatibleChatModel, Harness, ModelConfig, ModelRuntimeError, configuration_values, resolve_config
from guarded_harness.state_store import StateStore
from smolagents.models import ChatMessage, MessageRole
from types import SimpleNamespace
from unittest.mock import Mock


def test_production_entry_has_no_scripted_model():
    """正式入口只暴露真实模型，离线响应替身属于测试依赖。"""
    import subprocess
    import sys
    import guarded_harness.harness as module

    result = subprocess.run([sys.executable, "-m", "guarded_harness.harness", "run", "--help"], capture_output=True, text=True, check=True)
    assert "--script" not in result.stdout
    assert "fake" not in result.stdout
    assert not hasattr(module, "FakeModel")


def test_real_profile_runs_task_and_approval_needs_no_credential(transport, workspace, tmp_path, monkeypatch, capsys):
    """宿主配置启动真实协议，审批不依赖凭据或固定响应脚本，恢复沿用原配置。"""
    import sys
    from guarded_harness.harness import main

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"profiles": {"daily": {"backend": "deepseek", "model_id": "profile-model", "max_steps": 6, "max_tool_calls": 12}}}))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    transport.responses = [response("read", "read_file"), response("report", "write_report", {"content": "calculator.py defines add."}), response("final", "final_answer", {"answer": "See report.md"})]
    run_dir = tmp_path / "run"

    def command(*args):
        monkeypatch.setattr(sys, "argv", ["cli", *args, "--json"])
        main()
        return json.loads(capsys.readouterr().out)

    state = command("run", "--workspace", str(workspace), "--run-dir", str(run_dir), "--config", str(config), "--profile", "daily", "--task", "Explain addition")
    assert state["status"] == "WAITING_APPROVAL"
    assert state["max_steps"] == 6 and state["model_config"]["model_id"] == "profile-model"
    requests_before = len(transport.requests)
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    command("approve", "--run-dir", str(run_dir), "--call-id", "report", "--actor", "operator")
    assert len(transport.requests) == requests_before
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    assert command("resume", "--run-dir", str(run_dir))["status"] == "SUCCEEDED"


def test_directory_permission_and_hash_bound_edit_still_require_approval(workspace, tmp_path):
    """模型可在宿主授权目录中选择文件，但准确补丁必须审批，旧哈希不能覆盖新内容。"""
    from hashlib import sha256
    package = workspace / "package"
    package.mkdir()
    target = package / "logic.py"
    target.write_text("VALUE = 1\n")
    original_hash = sha256(target.read_bytes()).hexdigest()
    model = FakeModel([
        tool_message("edit", "edit_file", {"path": "package/logic.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2", "expected_sha256": original_hash}),
        tool_message("stale-edit", "edit_file", {"path": "package/logic.py", "old_text": "VALUE = 2", "new_text": "VALUE = 3", "expected_sha256": original_hash}),
        tool_message("read", "read_file", {"path": "package/logic.py"}),
        tool_message("final", "final_answer", {"answer": "Updated package/logic.py"}),
    ])
    harness = Harness(workspace, tmp_path / "run", model, task_kind="general", writable_directories=("package",))
    assert harness.run("Update the configured package").status == "WAITING_APPROVAL"
    assert target.read_text() == "VALUE = 1\n"
    harness.decide("edit", True, "operator")
    state = harness.resume()
    assert state.status == "SUCCEEDED" and target.read_text() == "VALUE = 2\n"
    assert state.actions["stale-edit"]["result"]["error"]["code"] == "SANDBOX_DENIED"
    assert len(state.writes) == 1


def test_large_source_range_is_verified_and_can_complete(workspace, tmp_path):
    """大文件按行读取形成可核对的完整文件哈希证据，仍需批准报告才能完成。"""
    (workspace / "large.py").write_text("VALUE = 1\n" * 9000)
    model = FakeModel([
        tool_message("range", "read_range", {"path": "large.py", "start_line": 100, "end_line": 110}),
        tool_message("report", "write_report", {"content": "large.py lines 100-110 assign VALUE = 1."}),
        tool_message("final", "final_answer", {"answer": "See report.md"}),
    ])
    harness = Harness(workspace, tmp_path / "run", model)
    state = harness.run("Explain the requested source range")
    assert state.status == "WAITING_APPROVAL"
    result = state.actions["range"]["result"]["result"]
    assert result["total_lines"] == 9000 and result["content"].count("VALUE = 1") == 11
    assert state.evidence[0]["start_line"] == 100
    harness.decide("report", True, "operator")
    assert harness.resume().status == "SUCCEEDED"


def test_context_keeps_recent_tool_pairs_without_losing_durable_evidence(workspace, tmp_path):
    """上下文只保留装得下的完整动作组，完整读取证据及动作仍保存在运行状态中。"""
    class CapturingModel(FakeModel):
        def generate(self, messages, **kwargs):
            self.last_context = json.dumps([message.dict() for message in messages], default=str)
            assert len(self.last_context) <= 70000
            return super().generate(messages, **kwargs)

    for index, character in enumerate("ABC"):
        (workspace / f"source-{index}.py").write_text(character * 40000)
    model = CapturingModel([
        *[tool_message(f"read-{index}", "read_file", {"path": f"source-{index}.py"}) for index in range(3)],
        tool_message("final", "final_answer", {"answer": "Read the requested files."}),
    ])
    state = Harness(workspace, tmp_path / "run", model, task_kind="general", context_chars=70000).run("Inspect three files")
    assert state.status == "SUCCEEDED" and len(state.evidence) == 3
    assert len(state.steps) == 4 and "A" * 1000 not in model.last_context and "C" * 1000 in model.last_context


def test_interactive_mode_approves_exact_report_and_keeps_json_clean(transport, workspace, tmp_path, monkeypatch, capsys):
    """终端审批由宿主输入触发，提示和预览写 stderr，JSON 输出仍可解析。"""
    import sys
    from guarded_harness.harness import main
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    transport.responses = [response("read", "read_file"), response("report", "write_report", {"content": "calculator.py defines addition."}), response("final", "final_answer", {"answer": "See report.md"})]
    monkeypatch.setattr("builtins.input", lambda: "y")
    monkeypatch.setattr(sys, "argv", ["cli", "run", "--workspace", str(workspace), "--run-dir", str(tmp_path / "run"), "--backend", "deepseek", "--task", "Explain addition", "--interactive", "--json"])
    main()
    captured = capsys.readouterr()
    state = json.loads(captured.out)
    assert state["status"] == "SUCCEEDED" and state["approvals"]["report"]["status"] == "CONSUMED"
    assert "calculator.py defines addition." in captured.err and "Approve" in captured.err


def test_host_cancel_request_blocks_next_tool_and_explicit_resume_preserves_budget(workspace, tmp_path):
    """取消请求通过宿主控制文件传递，生成中的模型答复不能在取消后执行工具。"""
    from guarded_harness.harness import request_cancel
    run_dir = tmp_path / "run"

    class CancellingModel(FakeModel):
        def generate(self, messages, **kwargs):
            request_cancel(run_dir, "operator")
            return super().generate(messages, **kwargs)

    harness = Harness(workspace, run_dir, CancellingModel([
        tool_message("cancelled-read", "read_file", {"path": "calculator.py"}),
    ]), max_steps=4)
    state = harness.run("Explain source")
    assert state.status == "STOPPED" and state.stop_reason == "CANCELLED"
    assert not state.evidence and state.steps_used == 1
    recovered = Harness.load(run_dir, FakeModel([
        tool_message("read", "read_file", {"path": "calculator.py"}),
        tool_message("report", "write_report", {"content": "calculator.py defines add."}),
        tool_message("final", "final_answer", {"answer": "See report.md"}),
    ]))
    assert recovered.resume().status == "WAITING_APPROVAL"
    recovered.decide("report", True, "operator")
    assert recovered.resume().status == "SUCCEEDED"
    assert recovered.state.steps_used == 4


@pytest.mark.parametrize("profile", [{"api_key": "must-not-be-stored"}, {"write_directories": "src"}, {"required_tests": "unit"}])
def test_host_profile_rejects_secret_fields_and_ambiguous_permission_types(workspace, tmp_path, profile):
    """配置仅接收公开字段及明确列表，避免凭据落盘和字符串被拆成目录授权。"""
    from guarded_harness.harness import read_profile
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": {"default": profile}}))
    with pytest.raises(ValueError):
        read_profile(path, None, workspace)


def test_workspace_configuration_cannot_grant_itself_permissions(workspace):
    from guarded_harness.harness import read_profile
    path = workspace / "config.json"
    path.write_text(json.dumps({"profiles": {"default": {"write_directories": ["src"]}}}))
    with pytest.raises(ValueError, match="outside"):
        read_profile(path, None, workspace)


def test_oversized_latest_context_stops_with_explicit_reason(workspace, tmp_path):
    """最新工具结果不能完整放入上下文时停止，不丢失结果后继续猜测或伪装完成。"""
    (workspace / "large.txt").write_text("A" * 60000)
    model = FakeModel([tool_message("read", "read_file", {"path": "large.txt"})])
    state = Harness(workspace, tmp_path / "run", model, context_chars=30000).run("Explain this file")
    assert state.status == "STOPPED" and state.stop_reason == "CONTEXT_LIMIT"
    assert model.calls == 1 and state.actions["read"]["status"] == "SUCCEEDED"


def test_initial_plan_tracks_real_repair_evidence(workspace, tmp_path, unit_commands):
    """实际失败、审批、写入及重测推进初始计划，不额外占用模型步骤。"""
    (workspace / "calculator.py").write_text("def add(a, b): return a - b\n")
    model = FakeModel([
        tool_message("read-before", "read_file", {"path": "calculator.py"}),
        tool_message("baseline", "run_test", {"name": "unit"}),
        tool_message("fix", "write_file", {"path": "calculator.py", "content": "def add(a, b): return a + b\n"}),
        tool_message("read-after", "read_file", {"path": "calculator.py"}),
        tool_message("verify", "run_test", {"name": "unit"}),
        tool_message("report", "write_report", {"content": "calculator.py fixed; unit failed before and passed after."}),
        tool_message("final", "final_answer", {"answer": "See report.md"}),
    ])
    harness = Harness(workspace, tmp_path / "run", model, max_steps=7, task_kind="repair",
                      writable_paths=("calculator.py",), test_commands=unit_commands)
    state = harness.run("Fix addition")
    assert state.plan and state.plan_cursor == 2
    assert state.plan_source == "harness" and state.plan_status == "PAUSED"
    harness.decide("fix", True, "operator")
    harness.resume()
    assert state.plan_cursor == 5
    harness.decide("report", True, "operator")
    harness.resume()
    assert state.status == "SUCCEEDED"
    assert state.plan_cursor == len(state.plan) and state.plan_status == "COMPLETED"
    assert state.steps_used == 7


@pytest.mark.parametrize("steps,cursor", [([], 0), (["a", 1], 0), ([" "], 0), (["x" * 513], 0),
                                        (["x"] * 17, 0), (["a"], -1), (["a"], 1), (["a"], True)])
def test_invalid_plan_never_changes_business_state(workspace, tmp_path, steps, cursor):
    """非法计划在 Gate 及 preflight 拦截，不覆盖已保存的计划，也不产生审批。"""
    model = FakeModel([tool_message("invalid-plan", "update_plan", {"steps": steps, "current_step": cursor})])
    state = Harness(workspace, tmp_path / "run", model, max_steps=1).run("Understand addition")
    assert state.plan_source == "harness" and state.plan_revision == 1
    assert state.actions["invalid-plan"]["status"] == "REJECTED"
    assert state.plan_cursor == 0 and not state.approvals


def test_model_plan_cannot_replace_completion_evidence(workspace, tmp_path):
    """模型把计划推进到最后一步也不能绕过报告、读取或最终验收。"""
    model = FakeModel([
        tool_message("plan", "update_plan", {"steps": ["Read", "Deliver"], "current_step": 1}),
        tool_message("final", "final_answer", {"answer": "All work is done"}),
    ])
    state = Harness(workspace, tmp_path / "run", model, max_steps=2).run("Understand addition")
    assert state.status == "STOPPED" and state.plan_status == "PAUSED"
    assert state.plan == ["Read", "Deliver"] and state.plan_cursor == 1
    assert state.plan_source == "model" and state.plan_revision == 2
    assert not state.actions["final"]["result"]["ok"]


def test_default_api_requires_approved_report(workspace, tmp_path):
    """H1：未指定任务类型时使用严格理解模式，读取证据不足以替代独立交付物。"""
    model = FakeModel([
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('final', 'final_answer', {'answer': 'Completed'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, max_steps=2)
    state = harness.run('Explain add')
    assert state.task_kind == 'understand'
    assert (state.status, state.stop_reason) == ('STOPPED', 'MAX_STEPS')
    assert 'report.md' in state.actions['final']['result']['error']['message']
    assert not (harness.run_dir / 'report.md').exists()


def test_offline_driver_keeps_strict_defaults_and_rejects_general(workspace, tmp_path):
    """测试驱动保留严格任务验收，不能用兼容模式替代正式任务。"""
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / 'requests.json'
    script.write_text(json.dumps([
        {'id': 'read', 'tool': 'read_file', 'arguments': {'path': 'calculator.py'}},
        {'id': 'final', 'tool': 'final_answer', 'arguments': {'answer': 'Completed'}},
    ]))
    command = [sys.executable, '-m', 'tests.support', 'run', '--workspace', str(workspace),
               '--run-dir', str(tmp_path / 'run'), '--script', str(script), '--task', 'Explain add', '--max-steps', '2']
    result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
    state = json.loads(result.stdout)
    assert state['task_kind'] == 'understand'
    assert state['status'] == 'STOPPED'
    assert 'report.md' in state['actions']['final']['result']['error']['message']
    rejected = subprocess.run(command + ['--task-kind', 'general'], capture_output=True, text=True, timeout=15)
    assert rejected.returncode == 2
    assert 'invalid choice' in rejected.stderr


def test_real_agent_loop_reads_then_finishes(workspace, tmp_path):
    """验证读取、最终答复、报告和审计均属于同一次实际 Agent 运行。"""
    model = FakeModel([
        tool_message("read-1", "read_file", {"path": "calculator.py"}),
        tool_message("answer-1", "final_answer", {"answer": "add returns a + b."}),
    ])
    run_dir = tmp_path / "run"
    state = Harness(workspace, run_dir, model, task_kind="general").run("Explain add")
    assert state.status == "SUCCEEDED"
    assert model.calls == 2
    assert state.tool_calls_used == 2
    assert state.steps_used == 2
    assert (run_dir / "report.md").read_text().strip() == state.answer
    assert StateStore(run_dir / "state.json").load().stop_reason == "COMPLETED"
    audit = [json.loads(line) for line in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert {record["tool_call_id"] for record in audit if record["event"] == "executed"} == {"read-1", "answer-1"}


def test_denial_is_returned_to_model_and_next_step_can_continue(workspace, tmp_path):
    """路径拒绝作为工具观察返回，后续有效调用仍可完成只读任务。"""
    model = FakeModel([
        tool_message("bad-1", "read_file", {"path": ".env"}),
        tool_message("read-1", "read_file", {"path": "calculator.py"}),
        tool_message("answer-1", "final_answer", {"answer": "add returns a + b."}),
    ])
    state = Harness(workspace, tmp_path / "run", model, task_kind="general").run("Explain add")
    assert state.status == "SUCCEEDED"
    observation = json.loads(state.steps[0]["observations"])
    assert observation["tool_call_id"] == "bad-1"
    assert observation["error"]["code"] == "SANDBOX_DENIED"
    assert len(state.evidence) == 1


def test_step_budget_stops_without_extra_model_request(workspace, tmp_path):
    """预算耗尽时不追加收尾模型请求，也不触发成功状态或生成报告。"""
    model = FakeModel([
        tool_message("read-1", "read_file", {"path": "calculator.py"}),
        ChatMessage(role=MessageRole.ASSISTANT, content="Fallback says completed."),
    ])
    state = Harness(workspace, tmp_path / "run", model, max_steps=1, task_kind="general").run("Explain add")
    assert state.status == "STOPPED"
    assert state.stop_reason == "MAX_STEPS"
    assert state.answer is None
    assert state.artifact is None
    assert model.calls == 1


def test_generation_failure_is_saved_before_raising(workspace, tmp_path):
    """模型生成异常向外传播前保存 FAILED 状态。"""
    import pytest

    run_dir = tmp_path / "run"
    harness = Harness(workspace, run_dir, FakeModel([]), task_kind="general")
    with pytest.raises(Exception, match="script exhausted"):
        harness.run("Explain add")
    assert StateStore(run_dir / "state.json").load().status == "FAILED"


def test_write_and_other_calls_in_same_step_are_all_rejected(workspace, tmp_path):
    """需审批动作不与其他调用共用步骤，避免暂停时产生未记录的部分批次。"""
    write = tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'X = 1\n'})
    read = tool_message('read-1', 'read_file', {'path': 'calculator.py'})
    write.tool_calls.extend(read.tool_calls)
    model = FakeModel([
        write,
        tool_message('read-2', 'read_file', {'path': 'calculator.py'}),
        tool_message('final-1', 'final_answer', {'answer': 'Read calculator.py.'}),
    ])
    original = (workspace / 'calculator.py').read_bytes()
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    state = harness.run('Read source')
    assert state.status == 'SUCCEEDED'
    rejected = [json.loads(line) for line in state.steps[0]['observations'].splitlines()]
    assert all(item['error']['code'] == 'PROTECTED_BATCH' for item in rejected)
    assert not state.approvals
    assert not state.writes
    assert (workspace / 'calculator.py').read_bytes() == original


def test_unresolved_approval_does_not_consume_more_model_budget(workspace, tmp_path):
    """未审批时多次请求继续保持等待，不再调用模型或清零预算。"""
    model = FakeModel([tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'X = 1\n'})])
    harness = Harness(workspace, tmp_path / 'run', model, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update file')
    for _ in range(2):
        assert harness.resume().status == 'WAITING_APPROVAL'
    assert model.calls == 1
    assert harness.state.steps_used == 1
    assert harness.state.tool_calls_used == 1


def test_command_line_approval_and_resume_use_separate_processes(workspace, tmp_path):
    """命令行接口跨四个进程完成创建、批准、恢复与状态读取。"""
    import subprocess
    import sys

    run_dir = tmp_path / 'run'
    initial = tmp_path / 'initial.json'
    continued = tmp_path / 'continued.json'
    original = (workspace / 'calculator.py').read_bytes()
    content = 'calculator.py: add returns a + b.\n'
    initial.write_text(json.dumps([
        {'id': 'read-cli', 'tool': 'read_file', 'arguments': {'path': 'calculator.py'}},
        {'id': 'report-cli', 'tool': 'write_report', 'arguments': {'content': content}},
    ]))
    continued.write_text(json.dumps([
        {'id': 'final-cli', 'tool': 'final_answer', 'arguments': {'answer': 'See report.md'}},
    ]))
    def command(*arguments):
        """执行单次宿主命令并解析业务状态，使用超时避免子进程挂起。"""
        result = subprocess.run([sys.executable, '-m', 'tests.support', *arguments], capture_output=True, text=True, check=True, timeout=20)
        return json.loads(result.stdout)
    pending = command('run', '--workspace', str(workspace), '--run-dir', str(run_dir), '--script', str(initial), '--task', 'Explain add')
    assert pending['status'] == 'WAITING_APPROVAL'
    approved = command('approve', '--run-dir', str(run_dir), '--call-id', 'report-cli', '--actor', 'operator')
    assert approved['approvals']['report-cli']['status'] == 'APPROVED'
    assert not (run_dir / 'report.md').exists()
    complete = command('resume', '--run-dir', str(run_dir), '--script', str(continued))
    assert complete['status'] == 'SUCCEEDED'
    assert not complete['writes']
    status = command('status', '--run-dir', str(run_dir))
    assert status == complete
    assert (run_dir / 'report.md').read_text() == content
    assert (workspace / 'calculator.py').read_bytes() == original


def test_understanding_task_requires_approved_report_artifact(workspace, tmp_path):
    """H1：没有独立交付物时拒绝完成；报告批准后继续完成，目标源码不变。"""
    report = '# Source analysis\ncalculator.py: add returns a + b.\n'
    model = FakeModel([
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('early-final', 'final_answer', {'answer': 'Done'}),
        tool_message('report-1', 'write_report', {'content': report}),
        tool_message('final-1', 'final_answer', {'answer': 'See report.md'}),
    ])
    original = (workspace / 'calculator.py').read_bytes()
    harness = Harness(workspace, tmp_path / 'run', model, task_kind='understand', max_steps=6)
    state = harness.run('Explain add')
    assert state.status == 'WAITING_APPROVAL'
    assert not (tmp_path / 'run' / 'report.md').exists()
    assert 'report.md' in state.actions['early-final']['result']['error']['message']
    harness.decide('report-1', True, 'operator')
    assert harness.resume().status == 'SUCCEEDED'
    assert (tmp_path / 'run' / 'report.md').read_text() == report
    assert not harness.state.writes
    assert (workspace / 'calculator.py').read_bytes() == original


def test_repair_task_runs_real_failure_fix_retest_and_artifact(workspace, tmp_path, unit_commands):
    """完整修复路径：真实失败、批准写入、当前版本重测、批准报告及最终验收。"""
    (workspace / 'calculator.py').write_text('def add(a,b): return a - b\n')
    replacement = 'def add(a,b): return a + b\n'
    report = '# Repair\ncalculator.py corrected. Required test unit passes on current source.\n'
    model = FakeModel([
        tool_message('baseline', 'run_test', {'name': 'unit'}),
        tool_message('fix', 'write_file', {'path': 'calculator.py', 'content': replacement}),
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('retest', 'run_test', {'name': 'unit'}),
        tool_message('report', 'write_report', {'content': report}),
        tool_message('final', 'final_answer', {'answer': 'See report.md and test-results.json'}),
    ])
    run_dir = tmp_path / 'run'
    harness = Harness(workspace, run_dir, model, max_steps=10, task_kind='repair', writable_paths=('calculator.py',), test_commands=unit_commands)
    assert harness.run('Fix add').pending_action['tool_call_id'] == 'fix'
    assert harness.state.tests[0]['status'] == 'FAILED'
    harness.decide('fix', True, 'operator')
    assert harness.resume().pending_action['tool_call_id'] == 'report'
    assert harness.state.tests[-1]['status'] == 'PASSED'
    harness.decide('report', True, 'operator')
    state = harness.resume()
    assert state.status == 'SUCCEEDED'
    assert state.steps_used == 6
    assert (run_dir / 'report.md').read_text() == report
    assert '-def add(a,b): return a - b' in (run_dir / 'changes.patch').read_text()
    assert '+def add(a,b): return a + b' in (run_dir / 'changes.patch').read_text()
    results = json.loads((run_dir / 'test-results.json').read_text())
    assert [result['status'] for result in results] == ['FAILED', 'PASSED']
    assert not results[0]['valid']
    assert results[1]['valid']
    assert results[1]['fingerprint_after'] == state.workspace_fingerprint


def test_tool_call_budget_stops_without_claiming_completion(workspace, tmp_path):
    """H3：工具调用预算耗尽后立即退出，不消费后续模型响应或生成报告。"""
    model = FakeModel([
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('final-1', 'final_answer', {'answer': 'Done'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, max_steps=4, max_tool_calls=1, task_kind="general")
    state = harness.run('Explain add')
    assert state.status == 'STOPPED'
    assert state.stop_reason == 'MAX_TOOL_CALLS'
    assert state.tool_calls_used == 1
    assert not (tmp_path / 'run' / 'report.md').exists()


def test_test_command_mutating_source_invalidates_workspace(workspace, tmp_path):
    """测试进程修改源码时不能被当作批准写入，立即转入工作区核对状态。"""
    import sys

    commands = {'mutate': [sys.executable, '-c', 'from pathlib import Path; Path("calculator.py").write_text("X = 1\\n")']}
    model = FakeModel([tool_message('test-1', 'run_test', {'name': 'mutate'})])
    harness = Harness(workspace, tmp_path / 'run', model, test_commands=commands, task_kind="general")
    state = harness.run('Run configured command')
    assert state.status == 'WAITING_REVIEW'
    assert state.stop_reason == 'WORKSPACE_CHANGED'
    assert not state.tests[-1]['valid']
    assert not (tmp_path / 'run' / 'report.md').exists()


def test_controller_cancellation_stops_test_and_model_loop(workspace, tmp_path):
    """宿主取消会停止测试进程及后续模型调用，持久化明确停止原因。"""
    import concurrent.futures
    import sys
    import time

    commands = {'slow': [sys.executable, '-c', 'import time; time.sleep(30)']}
    model = FakeModel([tool_message('test-1', 'run_test', {'name': 'slow'})])
    harness = Harness(workspace, tmp_path / 'run', model, test_commands=commands, task_kind="general")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(harness.run, 'Run slow command')
        deadline = time.monotonic() + 5
        while harness.state.active_process is None and not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert harness.state.active_process is not None
        harness.cancel()
        state = future.result(timeout=5)
    assert state.status == 'STOPPED'
    assert state.stop_reason == 'CANCELLED'
    assert state.tests[-1]['status'] == 'CANCELLED'
    assert state.active_process is None
    assert model.calls == 1


def test_repair_cli_restores_command_policy_and_test_evidence(workspace, tmp_path, unit_commands):
    """修复任务跨独立进程恢复，仍执行持久化的命令策略并生成完整证据产物。"""
    import subprocess
    import sys

    (workspace / 'calculator.py').write_text('def add(a,b): return a - b\n')
    config = tmp_path / 'commands.json'
    config.write_text(json.dumps(unit_commands))
    run_dir = tmp_path / 'run'
    def command(*arguments):
        """执行一次独立宿主进程并解析业务状态。"""
        result = subprocess.run([sys.executable, '-m', 'tests.support', *arguments], capture_output=True, text=True, check=True, timeout=20)
        return json.loads(result.stdout)
    start = command('run', '--workspace', str(workspace), '--run-dir', str(run_dir), '--task-kind', 'repair', '--test-commands', str(config), '--script', 'tests/fixtures/requests/repair-start.json', '--write-path', 'calculator.py', '--max-steps', '10', '--task', 'Fix failing add test')
    assert start['tests'][0]['status'] == 'FAILED'
    command('approve', '--run-dir', str(run_dir), '--call-id', 'fix', '--actor', 'operator')
    verified = command('resume', '--run-dir', str(run_dir), '--script', 'tests/fixtures/requests/repair-verify.json')
    assert verified['status'] == 'WAITING_APPROVAL'
    assert verified['tests'][-1]['status'] == 'PASSED'
    command('approve', '--run-dir', str(run_dir), '--call-id', 'repair-report', '--actor', 'operator')
    finished = command('resume', '--run-dir', str(run_dir), '--script', 'tests/fixtures/requests/repair-finish.json')
    assert finished['status'] == 'SUCCEEDED'
    assert finished['test_commands'] == unit_commands
    assert finished['steps_used'] == 6
    assert all((run_dir / name).is_file() for name in ('report.md', 'test-results.json', 'changes.patch', 'audit.jsonl', 'checkpoint.json'))
    # A2：关联真实测试执行、边界检查、审批与权威快照，而不仅检查事件顺序。
    events = [json.loads(line) for line in (run_dir / 'audit.jsonl').read_text().splitlines()]
    assert all(item['run_id'] == finished['run_id'] for item in events)
    assert {'gate', 'sandbox', 'state', 'operator'} <= {item['actor'] for item in events}
    assert all(item['tool_call_id'] == item['toolCallId'] for item in events)
    for call_id in ('baseline', 'fix', 'retest', 'repair-report'):
        action = finished['actions'][call_id]
        linked = [item for item in events if item['tool_call_id'] == call_id]
        assert action['status'] == 'SUCCEEDED'
        assert any(item['event'] == 'preflight_checked' and item['decision'] == 'allow' for item in linked)
        assert any(item['event'] == 'executed' for item in linked)
    assert any(item['event'] == 'checkpoint_saved' and item['revision'] == finished['revision'] for item in events)
    for test in finished['tests']:
        assert any(item['event'] == 'process_finished' and item['tool_call_id'] == test['tool_call_id']
                   and item['decision'] == test['status'] for item in events)


def test_keyboard_interrupt_terminates_running_cli_test(workspace, tmp_path):
    """真实 SIGINT 使 CLI 停止，并用子进程 FIFO EOF 验证测试已真实终止。"""
    import os
    import signal
    import subprocess
    import sys
    import time

    pipe = tmp_path / 'test-liveness.fifo'
    ready = tmp_path / 'ready'
    os.mkfifo(pipe)
    descriptor = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    code = f'import os,time,pathlib; live=os.open({str(pipe)!r},os.O_WRONLY); pathlib.Path({str(ready)!r}).write_text("ready"); time.sleep(30)'
    config = tmp_path / 'commands.json'
    config.write_text(json.dumps({'slow': [sys.executable, '-c', code]}))
    script = tmp_path / 'request.json'
    script.write_text(json.dumps([{'id': 'slow-test', 'tool': 'run_test', 'arguments': {'name': 'slow'}}]))
    run_dir = tmp_path / 'run'
    process = subprocess.Popen([sys.executable, '-m', 'tests.support', 'run', '--workspace', str(workspace), '--run-dir', str(run_dir), '--task', 'Run slow test', '--test-commands', str(config), '--script', str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = False
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            checkpoint = run_dir / 'checkpoint.json'
            if checkpoint.exists():
                state = json.loads(checkpoint.read_text())
                if state['active_process'] and ready.exists():
                    started = True
                    break
            time.sleep(0.01)
        assert started
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        assert process.returncode != 0
        state = StateStore(run_dir / 'state.json').load()
        assert state.stop_reason == 'CANCELLED'
        deadline = time.monotonic() + 1
        while True:
            try:
                assert os.read(descriptor, 1) == b''
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Test process still holds the FIFO after CLI interruption')
                time.sleep(0.01)
    finally:
        os.close(descriptor)
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=5)


def test_host_can_return_report_for_revision_without_writing_it(workspace, tmp_path):
    """退回报告只提交反馈，不写入原内容；模型修订后仍需再次批准。"""
    model = FakeModel([
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('report-old', 'write_report', {'content': 'calculator.py: incorrect description'}),
        tool_message('report-new', 'write_report', {'content': 'calculator.py: add returns Python a + b without runtime type checks.'}),
        tool_message('final', 'final_answer', {'answer': 'See report.md'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, task_kind='understand', max_steps=6)
    harness.run('Explain add')
    harness.request_report_revision('report-old', 'Clarify Python runtime type behavior.', 'reviewer')
    assert harness.state.stop_reason == 'REPORT_REVISION_REQUIRED'
    assert not (tmp_path / 'run' / 'report.md').exists()
    assert harness.resume().pending_action['tool_call_id'] == 'report-new'
    assert 'Clarify Python runtime type behavior.' in harness.state.steps[1]['observations']
    assert harness.state.approvals['report-old']['status'] == 'REVISION_REQUESTED'
    assert not (tmp_path / 'run' / 'report.md').exists()
    harness.decide('report-new', True, 'reviewer')
    assert harness.resume().status == 'SUCCEEDED'
    assert harness.state.steps_used == 4
    assert 'without runtime type checks' in (tmp_path / 'run' / 'report.md').read_text()


def test_protected_batch_honors_call_budget(tmp_path):
    """H3：批量调用超过工具预算时应按工具预算停止，不能全部漏记。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'source.py').write_text('VALUE = 1\n')
    batch = tool_message('write', 'write_report', {'content': 'source.py: VALUE = 1'})
    batch.tool_calls += tool_message('read', 'read_file', {'path': 'source.py'}).tool_calls
    model = FakeModel([batch, tool_message('extra-read', 'read_file', {'path': 'source.py'})])
    controller = Harness(workspace, tmp_path / 'run', model, max_steps=2, max_tool_calls=1)
    spec = controller.gate.tools['read_file']
    handler = Mock(wraps=spec.handler)
    controller.gate.tools['read_file'] = replace(spec, handler=handler)
    state = controller.run('Inspect source')
    # 第一批已经提出两个调用，后续请求不得借漏记的预算进入 handler。
    handler.assert_not_called()
    assert state.stop_reason == 'MAX_TOOL_CALLS'
    assert state.tool_calls_used == 1
    assert model.calls == 1
    assert {'write', 'read'} <= set(state.seen_call_ids)
    assert len(state.rejections) == 2


def test_duplicate_id_in_protected_batch_restores_each_rejection(workspace, tmp_path):
    """批次内重复 ID 的两个拒绝按步骤位置恢复，不覆盖首次意图或合并观察。"""
    batch = tool_message('same-id', 'write_report', {'content': 'calculator.py: addition'})
    batch.tool_calls += tool_message('same-id', 'read_file', {'path': 'calculator.py'}).tool_calls
    run_dir = tmp_path / 'run'
    state = Harness(workspace, run_dir, FakeModel([batch]), max_steps=1).run('Inspect source')
    assert state.tool_calls_used == 2
    assert not state.approvals and not state.writes
    recovered = Harness.load(run_dir, FakeModel([]))
    outcomes = [json.loads(line) for line in recovered.state.steps[0]['observations'].splitlines()]
    assert [outcome['error']['code'] for outcome in outcomes] == ['PROTECTED_BATCH', 'DUPLICATE_CALL']
    assert recovered.state.actions['same-id']['result']['error']['code'] == 'PROTECTED_BATCH'






@pytest.fixture
def transport(monkeypatch):
    """替换 HTTP 传输，记录真实序列化请求并提供有界响应数据。"""
    state = SimpleNamespace(requests=[], responses=[], status=200, error=None)

    class Response:
        """支持 requests 响应上下文及分块读取的协议替身。"""

        def __init__(self, payload):
            """保存将被分块读取的响应字节。"""
            self.status_code = state.status
            self.payload = payload

        def __enter__(self):
            """进入响应上下文。"""
            return self

        def __exit__(self, *args):
            """结束响应上下文，不抑制异常。"""
            return False

        def iter_content(self, chunk_size):
            """按传输请求的大小分块，覆盖响应上限检查。"""
            for offset in range(0, len(self.payload), chunk_size):
                yield self.payload[offset:offset + chunk_size]

    class Session:
        """捕获 session 配置与请求参数，不访问网络。"""

        trust_env = True

        def __enter__(self):
            """进入 session 上下文。"""
            return self

        def __exit__(self, *args):
            """结束 session 上下文，不抑制异常。"""
            return False

        def post(self, endpoint, **kwargs):
            """记录请求并返回队列中的响应或模拟传输故障。"""
            state.requests.append({'endpoint': endpoint, 'trust_env': self.trust_env, **kwargs})
            if state.error:
                raise state.error
            payload = state.responses.pop(0)
            if not isinstance(payload, bytes):
                payload = json.dumps(payload).encode()
            return Response(payload)

    monkeypatch.setattr(requests, 'Session', Session)
    return state


def response(call_id='read', name='read_file', arguments=None):
    """构造标准工具调用响应及可验证的 token 统计。"""
    return {
        'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments or {'path': 'calculator.py'})}}
        ]}}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 2},
    }


def real_model():
    """使用独立假凭据创建适配器，测试不得读取用户真实认证值。"""
    config = resolve_config('deepseek', {'DEEPSEEK_MODEL': 'model-under-test'})
    return CompatibleChatModel(config, {'DEEPSEEK_API_KEY': 'test-only-secret'}), config


def test_adapter_serializes_tools_and_parses_original_call_id(transport, workspace, tmp_path):
    """复用 smolagents schema，保留工具调用 ID 并记录服务端 token 用量。"""
    model, config = real_model()
    transport.responses = [response()]
    harness = Harness(workspace, tmp_path / 'run', model, model_config=config.public_dict())
    message = model.generate([], tools_to_call_from=harness._agent.tools_and_managed_agents)
    assert message.tool_calls[0].id == 'read'
    assert message.token_usage.input_tokens == 10
    request = transport.requests[0]
    assert request['endpoint'] == 'https://api.deepseek.com/chat/completions'
    assert request['headers']['Authorization'] == 'Bearer test-only-secret'
    assert request['json']['thinking'] == {'type': 'disabled'}
    assert request['json']['tools']
    assert not request['allow_redirects']
    assert not request['trust_env']


def test_real_adapter_drives_guarded_loop_without_persisting_secret(transport, workspace, tmp_path):
    """适配器驱动实际 Agent 循环并恢复报告审批，业务记录不包含认证值。"""
    model, config = real_model()
    transport.responses = [
        response('list', 'list_files', {'path': '.'}),
        response('read', 'read_file', {'path': 'calculator.py'}),
        response('report', 'write_report', {'content': 'calculator.py: add returns a + b.'}),
        response('final', 'final_answer', {'answer': 'See report.md'}),
    ]
    harness = Harness(workspace, tmp_path / 'run', model, task_kind='understand', max_steps=6, model_config=config.public_dict())
    assert harness.run('Explain addition').status == 'WAITING_APPROVAL'
    harness.decide('report', True, 'reviewer')
    assert harness.resume().status == 'SUCCEEDED'
    assert harness.state.input_tokens == 40
    assert harness.state.output_tokens == 8
    for path in (tmp_path / 'run').glob('*'):
        if path.is_file():
            assert 'test-only-secret' not in path.read_text()


@pytest.mark.parametrize('url', ['https://user:password@example.com', 'https://example.com?api_key=secret', 'https://example.com#secret', 'file:///tmp/model'])
def test_config_rejects_urls_containing_credentials_or_non_http_scheme(url):
    """不允许将凭据嵌入可持久化接口地址。"""
    with pytest.raises(ValueError):
        ModelConfig('compatible', 'model', url, 'API_KEY').validate()


def test_environment_overrides_explicit_file_without_modifying_global_environment(tmp_path, monkeypatch):
    """进程变量优先于 dotenv，解析不修改全局环境或将值写入公开配置。"""
    env_file = tmp_path / '.env'
    env_file.write_text('DEEPSEEK_API_KEY=file-value\nDEEPSEEK_MODEL=file-model\n')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'process-value')
    values = configuration_values(env_file)
    assert values['DEEPSEEK_API_KEY'] == 'process-value'
    assert values['DEEPSEEK_MODEL'] == 'file-model'
    config = resolve_config('deepseek', values)
    assert 'process-value' not in json.dumps(config.public_dict())
    assert 'file-value' not in json.dumps(config.public_dict())


def test_saved_model_configuration_cannot_be_redirected_on_resume():
    """恢复使用原接口、模型及请求预算，显式切换目标必须创建新运行。"""
    saved = resolve_config('deepseek', {}).public_dict()
    assert resolve_config('deepseek', {'DEEPSEEK_BASE_URL': 'https://different.example'}, saved=saved).public_dict() == saved
    with pytest.raises(ValueError, match='differs'):
        resolve_config('deepseek', {}, base_url='https://different.example', saved=saved)


def test_missing_credential_is_reported_by_variable_name():
    """认证缺失在创建运行前报告，不进行网络请求。"""
    with pytest.raises(ValueError, match='DEEPSEEK_API_KEY'):
        CompatibleChatModel(resolve_config('deepseek', {}), {})


def test_http_and_transport_errors_do_not_echo_credentials(transport):
    """错误响应和传输异常即使包含敏感文本，公开异常也不输出原始内容。"""
    model, _ = real_model()
    transport.status = 401
    transport.responses = [b'test-only-secret']
    with pytest.raises(ModelRuntimeError) as failure:
        model.generate([])
    assert '401' in str(failure.value)
    assert 'test-only-secret' not in str(failure.value)
    transport.error = requests.ConnectionError('test-only-secret')
    with pytest.raises(ModelRuntimeError) as failure:
        model.generate([])
    assert 'test-only-secret' not in str(failure.value)


@pytest.mark.parametrize('payload', [b'not json', {'choices': []}, {'choices': [{'message': {'role': 'user'}}]}])
def test_invalid_chat_response_is_rejected(transport, payload):
    """响应结构错误不能伪造成功模型输出。"""
    model, _ = real_model()
    transport.responses = [payload]
    with pytest.raises(ModelRuntimeError, match='contract'):
        model.generate([])


def test_model_response_size_limit_is_enforced(transport):
    """即使服务返回异常大响应，适配器也只接受配置的协议上限。"""
    model, _ = real_model()
    transport.responses = [b'x' * (1024 * 1024 + 1)]
    with pytest.raises(ModelRuntimeError, match='1 MiB'):
        model.generate([])


def test_harness_refuses_secret_fields_in_public_metadata(workspace, tmp_path):
    """业务状态接口拒绝误传密钥字段。"""
    from tests.support import FakeModel

    with pytest.raises(ValueError, match='public configuration'):
        Harness(workspace, tmp_path / 'run', FakeModel([]), model_config={'api_key': 'test-only-secret'})


def test_cli_requires_explicit_real_backend_when_no_script(workspace, tmp_path, monkeypatch):
    """遗漏离线脚本不会隐式切换真实服务，必须显式选择网络后端。"""
    import sys
    from guarded_harness.harness import main

    monkeypatch.setattr(sys, 'argv', ['cli', 'run', '--workspace', str(workspace), '--run-dir', str(tmp_path / 'run'), '--task', 'Read source'])
    with pytest.raises(ValueError, match='--backend'):
        main()
    assert not (tmp_path / 'run').exists()


def test_cli_real_backend_restores_public_configuration_and_environment_secret(transport, workspace, tmp_path, monkeypatch, capsys):
    """CLI 使用环境凭据，批准命令不调用模型，恢复固定原模型配置且不输出密钥。"""
    import sys
    from guarded_harness.harness import main

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only-secret')
    transport.responses = [
        response('read', 'read_file', {'path': 'calculator.py'}),
        response('report', 'write_report', {'content': 'calculator.py: add returns a + b.'}),
        response('final', 'final_answer', {'answer': 'See report.md'}),
    ]
    run_dir = tmp_path / 'run'
    def command(*arguments):
        """在同一测试进程调用 CLI，捕获标准输出供协议断言。"""
        monkeypatch.setattr(sys, 'argv', ['cli', *arguments, '--json'])
        main()
        output = capsys.readouterr().out
        assert 'test-only-secret' not in output
        return json.loads(output)
    state = command('run', '--workspace', str(workspace), '--run-dir', str(run_dir), '--task', 'Explain add', '--task-kind', 'understand', '--backend', 'deepseek', '--model-id', 'model-under-test', '--model-max-tokens', '512')
    assert state['status'] == 'WAITING_APPROVAL'
    before = len(transport.requests)
    command('approve', '--run-dir', str(run_dir), '--call-id', 'report', '--actor', 'operator')
    assert len(transport.requests) == before
    complete = command('resume', '--run-dir', str(run_dir))
    assert complete['status'] == 'SUCCEEDED'
    assert complete['model_config']['model_id'] == 'model-under-test'
    assert transport.requests[-1]['json']['max_tokens'] == 512


def test_cli_refuses_real_backend_combined_with_a_script(workspace, tmp_path, monkeypatch):
    """模型后端和固定响应脚本不能混用，避免把固定输出误记为真实验收。"""
    import sys
    from guarded_harness.harness import main

    monkeypatch.setattr(sys, 'argv', ['cli', 'run', '--workspace', str(workspace), '--run-dir', str(tmp_path / 'run'), '--task', 'Read source', '--backend', 'deepseek', '--script', 'tests/fixtures/requests/understand-start.json'])
    with pytest.raises(SystemExit):
        main()
    assert not (tmp_path / 'run').exists()


def test_provider_output_limit_stops_without_retrying_or_losing_usage(transport, workspace, tmp_path):
    """截断响应直接停止，既不执行残缺调用，也不浪费预算反复生成同一报告。"""
    model, config = real_model()
    clipped = response('report', 'write_report', {'content': 'incomplete'})
    clipped['choices'][0]['finish_reason'] = 'length'
    clipped['usage'] = {'prompt_tokens': 100, 'completion_tokens': 1024}
    transport.responses = [clipped]
    harness = Harness(workspace, tmp_path / 'run', model, max_steps=8, task_kind='understand', model_config=config.public_dict())
    state = harness.run('Explain code')
    assert state.status == 'STOPPED'
    assert state.stop_reason == 'MODEL_OUTPUT_LIMIT'
    assert state.input_tokens == 100
    assert state.output_tokens == 1024
    assert not state.actions
    assert len(transport.requests) == 1
    assert not (tmp_path / 'run' / 'report.md').exists()


@pytest.mark.parametrize("via_cli", [False, True])
def test_output_budget_extension_regenerates_clipped_response_without_replaying_tools(transport, workspace, tmp_path, monkeypatch, capsys, via_cli):
    """追加单次输出额度后重生成截断答复，原读取只执行一次，费用计数和审批保留。"""
    import sys
    from copy import deepcopy
    from guarded_harness.harness import ModelConfig, main
    model, config = real_model()
    clipped = response("clipped-report", "write_report", {"content": "calculator.py: incomplete"})
    clipped["choices"][0]["finish_reason"] = "length"
    clipped["usage"] = {"prompt_tokens": 100, "completion_tokens": config.max_tokens}
    transport.responses = [response("read", "read_file"), clipped,
                           response("report", "write_report", {"content": "calculator.py defines addition."}),
                           response("final", "final_answer", {"answer": "See report.md"})]
    controller = Harness(workspace, tmp_path / "run", model, max_steps=8, max_tool_calls=8, model_config=config.public_dict())
    before = deepcopy(controller.run("Explain addition"))
    assert before.stop_reason == "MODEL_OUTPUT_LIMIT" and before.steps_used == 2
    assert before.tool_calls_used == 1 and "clipped-report" not in before.actions
    if via_cli:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(sys, "argv", ["cli", "extend-budget", "--run-dir", str(controller.run_dir),
                          "--model-max-tokens", "4096", "--actor", "operator", "--reason", "Allow a complete report", "--json"])
        main()
        updated = json.loads(capsys.readouterr().out)
        assert updated["model_config"]["max_tokens"] == 4096
        controller = Harness.load(controller.run_dir, CompatibleChatModel(ModelConfig(**updated["model_config"]), {"DEEPSEEK_API_KEY": "test-only-secret"}))
    else:
        controller.extend_budget(model_max_tokens=4096, actor="operator", reason="Allow a complete report")
    state = controller.state
    assert len(transport.requests) == 2
    assert state.status == "STOPPED" and state.stop_reason == "BUDGET_EXTENDED"
    for key in ("run_id", "steps_used", "tool_calls_used", "input_tokens", "output_tokens", "evidence", "actions", "seen_call_ids", "approvals", "writable_paths", "writable_directories"):
        assert getattr(state, key) == getattr(before, key)
    assert {key: value for key, value in state.model_config.items() if key != "max_tokens"} == {key: value for key, value in before.model_config.items() if key != "max_tokens"}
    assert state.budget_changes[-1]["previous"]["model_max_tokens"] == config.max_tokens
    assert state.budget_changes[-1]["updated"]["model_max_tokens"] == 4096
    assert controller.resume().status == "WAITING_APPROVAL"
    assert transport.requests[-1]["json"]["max_tokens"] == 4096
    assert not (controller.run_dir / "report.md").exists()
    controller.decide("report", True, "operator")
    assert controller.resume().status == "SUCCEEDED"
    assert state.steps_used == 4 and state.tool_calls_used == 3
    assert "clipped-report" not in state.actions
    events = [json.loads(line) for line in (controller.run_dir / "audit.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "executed" and row["tool_call_id"] == "read" for row in events) == 1


@pytest.mark.parametrize("limit", [None, 0, True, 512, 1024, 8193, 3.5])
def test_output_extension_rejects_invalid_reduced_or_unchanged_limit(transport, workspace, tmp_path, limit):
    """单次输出额度只允许在支持范围内增加，拒绝时保留截断状态及用量。"""
    from copy import deepcopy
    model, config = real_model()
    clipped = response("clipped", "write_report", {"content": "incomplete"})
    clipped["choices"][0]["finish_reason"] = "length"
    transport.responses = [clipped]
    controller = Harness(workspace, tmp_path / "run", model, max_steps=8, model_config=config.public_dict())
    before = deepcopy(controller.run("Explain source"))
    with pytest.raises(ValueError):
        controller.extend_budget(model_max_tokens=limit, actor="operator", reason="Invalid extension")
    assert controller.state == before
    assert model.config == config and len(transport.requests) == 1


def test_step_extension_does_not_unlock_an_output_limited_run(transport, workspace, tmp_path):
    """不同额度不能互相替代，输出截断必须提高对应单次上限。"""
    model, config = real_model()
    clipped = response()
    clipped["choices"][0]["finish_reason"] = "length"
    transport.responses = [clipped]
    controller = Harness(workspace, tmp_path / "run", model, max_steps=8, model_config=config.public_dict())
    controller.run("Explain source")
    with pytest.raises(ValueError, match="model-max-tokens"):
        controller.extend_budget(max_steps=16, actor="operator", reason="Wrong budget")
    assert controller.state.max_steps == 8 and controller.state.stop_reason == "MODEL_OUTPUT_LIMIT"
    assert not controller.state.budget_changes


def test_output_extension_requires_saved_real_model_configuration(workspace, tmp_path):
    controller = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("read", "read_file", {"path": "calculator.py"}),
    ]), max_steps=1)
    controller.run("Explain source")
    with pytest.raises(ValueError, match="saved real-model"):
        controller.extend_budget(max_steps=4, model_max_tokens=4096, actor="operator", reason="No model metadata")
    assert controller.state.max_steps == 1 and not controller.state.budget_changes


def test_cli_parsing_errors_go_to_stderr_without_corrupting_json(transport, workspace, tmp_path, monkeypatch, capsys):
    """模型解析失败后继续执行时，诊断输出不能污染 CLI 的 JSON 标准输出。"""
    import sys
    from guarded_harness.harness import main

    monkeypatch.setenv('DEEPSEEK_API_KEY', 'test-only-secret')
    transport.responses = [
        {'choices': [{'message': {'role': 'assistant', 'content': 'not a tool call'}}]},
        response('read', 'read_file', {'path': 'calculator.py'}),
        response('report', 'write_report', {'content': 'calculator.py: add returns a + b.'}),
    ]
    monkeypatch.setattr(sys, 'argv', ['cli', 'run', '--workspace', str(workspace), '--run-dir', str(tmp_path / 'run'), '--task', 'Read source', '--backend', 'deepseek', '--json'])
    main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)['status'] == 'WAITING_APPROVAL'
    assert 'does not contain any JSON' in ' '.join(captured.err.split())
