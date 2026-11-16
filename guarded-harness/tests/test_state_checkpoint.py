"""验证状态往返保存，以及新进程读取业务快照时的数据完整性。"""

import json
import pytest
import subprocess
import sys
import textwrap
from dataclasses import asdict
from guarded_harness.checkpoint import Checkpoint
from guarded_harness.state_store import RunState, StateStore


def test_budget_extension_cli_preserves_progress_and_resumes_without_replaying_write(workspace, tmp_path):
    """宿主追加总预算后换进程继续，保留原调用、审批和计数，不重写已完成源码。"""
    import os
    from guarded_harness.harness import Harness
    from tests.support import FakeModel, tool_message

    content = "VALUE = 123\n"
    run_dir = tmp_path / "run"
    controller = Harness(workspace, run_dir, FakeModel([
        tool_message("write", "write_file", {"path": "calculator.py", "content": content}),
        tool_message("read", "read_file", {"path": "calculator.py"}),
    ]), max_steps=2, max_tool_calls=8, writable_paths=("calculator.py",), task_kind="general")
    controller.run("Apply the permitted change")
    controller.decide("write", True, "operator")
    assert controller.resume().stop_reason == "MAX_STEPS"
    before = asdict(Checkpoint(run_dir / "checkpoint.json").load())
    environment = {key: value for key, value in os.environ.items() if key not in {"DEEPSEEK_API_KEY", "GUARDED_HARNESS_API_KEY"}}
    child = subprocess.run([
        sys.executable, "-m", "guarded_harness.harness", "extend-budget", "--run-dir", str(run_dir),
        "--max-steps", "4", "--actor", "operator", "--reason", "Continue the same task", "--json",
    ], capture_output=True, text=True, timeout=20, check=True, env=environment)
    updated = json.loads(child.stdout)
    assert updated["max_steps"] == 4 and updated["max_tool_calls"] == 8
    assert (updated["status"], updated["stop_reason"]) == ("STOPPED", "BUDGET_EXTENDED")
    for key in ("run_id", "task", "workspace", "steps_used", "tool_calls_used", "steps", "evidence", "writes", "tests", "actions", "approvals", "seen_call_ids", "workspace_fingerprint", "initial_workspace_fingerprint", "last_action", "repeated_actions", "pending_action", "report_evidence", "writable_paths", "writable_directories", "test_commands", "required_tests", "model_config", "input_tokens", "output_tokens", "context_chars"):
        assert updated[key] == before[key]
    assert updated["budget_changes"][-1]["actor"] == "operator"
    assert updated["budget_changes"][-1]["previous"]["max_steps"] == 2
    assert updated["budget_changes"][-1]["updated"]["max_steps"] == 4
    assert "harness.py" in updated["budget_changes"][-1]["implementation_sha256"]

    continued = textwrap.dedent('''
        import json, sys
        from pathlib import Path
        from dataclasses import asdict
        from guarded_harness.harness import Harness
        from tests.support import FakeModel, tool_message
        controller = Harness.load(Path(sys.argv[1]), FakeModel([
            tool_message('write', 'write_file', {'path': 'calculator.py', 'content': 'VALUE = 123\\n'}),
            tool_message('final', 'final_answer', {'answer': 'Updated calculator.py'}),
        ]))
        print(json.dumps(asdict(controller.resume())))
    ''')
    child = subprocess.run([sys.executable, "-c", continued, str(run_dir)], capture_output=True, text=True, timeout=20, check=True)
    final = json.loads(child.stdout)
    assert final["status"] == "SUCCEEDED"
    assert final["steps_used"] == final["tool_calls_used"] == 4
    assert len(final["writes"]) == 1 and final["actions"]["write"]["status"] == "SUCCEEDED"
    assert (workspace / "calculator.py").read_text() == content
    events = [json.loads(line) for line in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "executed" and row["tool_call_id"] == "write" for row in events) == 1
    assert any(row["event"] == "budget_extended" and row["actor"] == "operator" for row in events)


@pytest.mark.parametrize("limits", [
    {}, {"max_steps": 2}, {"max_steps": 1}, {"max_steps": 0}, {"max_steps": True},
    {"max_steps": 3.5}, {"max_steps": 4, "max_tool_calls": 7}, {"max_tool_calls": 16},
])
def test_budget_extension_rejects_noop_reduction_and_insufficient_allowance(workspace, tmp_path, limits):
    """预算只能增加，且必须让当前已耗尽的限制有剩余额度；拒绝时不改计数。"""
    from guarded_harness.harness import Harness
    from tests.support import FakeModel, tool_message
    controller = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("read-1", "read_file", {"path": "calculator.py"}),
        tool_message("read-2", "read_file", {"path": "calculator.py"}),
    ]), max_steps=2, max_tool_calls=8)
    assert controller.run("Read source").stop_reason == "MAX_STEPS"
    before = asdict(controller.state)
    with pytest.raises(ValueError):
        controller.extend_budget(**limits, actor="operator", reason="Continue task")
    assert asdict(controller.state) == before
    assert asdict(Checkpoint(controller.run_dir / "checkpoint.json").load()) == before


def test_tool_call_budget_can_be_extended_without_resetting_seen_ids(workspace, tmp_path):
    """工具预算耗尽也可由宿主追加；预算拒绝的旧 ID 仍不能重新执行。"""
    from guarded_harness.harness import Harness
    from tests.support import FakeModel, tool_message
    controller = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("read", "read_file", {"path": "calculator.py"}),
        tool_message("over-budget", "final_answer", {"answer": "Done"}),
    ]), max_steps=4, max_tool_calls=1, task_kind="general")
    assert controller.run("Explain source").stop_reason == "MAX_TOOL_CALLS"
    controller.extend_budget(max_tool_calls=3, actor="operator", reason="Allow final verification")
    assert controller.state.tool_calls_used == 1 and controller.state.steps_used == 2
    assert "over-budget" in controller.state.seen_call_ids
    recovered = Harness.load(controller.run_dir, FakeModel([
        tool_message("over-budget", "final_answer", {"answer": "Done"}),
        tool_message("new-final", "final_answer", {"answer": "Verified calculator.py"}),
    ]))
    assert recovered.resume().status == "SUCCEEDED"
    assert recovered.state.tool_calls_used == 3 and recovered.state.steps_used == 4
    assert recovered.state.rejections[-1]["result"]["error"]["code"] == "DUPLICATE_CALL"


@pytest.mark.parametrize("limits", [{"max_steps": 4}, {"model_max_tokens": 4096}])
def test_budget_extension_blocks_workspace_drift(workspace, tmp_path, limits):
    """追加预算不能越过工作区版本核对；旧读取证据必须失效并停止。"""
    from guarded_harness.harness import Harness
    from tests.support import FakeModel, tool_message
    controller = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("read", "read_file", {"path": "calculator.py"}),
    ]), max_steps=1)
    controller.run("Explain source")
    (workspace / "calculator.py").write_text("EXTERNAL = True\n")
    with pytest.raises(ValueError, match="Workspace changed"):
        controller.extend_budget(**limits, actor="operator", reason="Continue task")
    assert controller.state.status == "WAITING_REVIEW"
    assert not controller.state.evidence and not controller.state.budget_changes
    assert controller.state.max_steps == 1


@pytest.mark.parametrize("stage", ["pending", "denied", "completed", "unknown"])
@pytest.mark.parametrize("limits", [{"max_steps": 60}, {"model_max_tokens": 4096}])
def test_budget_extension_cannot_unlock_approval_or_review_or_terminal_states(workspace, tmp_path, stage, limits):
    """预算控制不是审批或恢复后门，待审批、拒绝、完成与未知写入均不可解锁。"""
    from dataclasses import replace
    from guarded_harness.harness import Harness
    from guarded_harness.sandbox import SandboxViolation
    from tests.support import FakeModel, tool_message
    if stage == "completed":
        model = FakeModel([tool_message("read", "read_file", {"path": "calculator.py"}), tool_message("final", "final_answer", {"answer": "Read calculator.py"})])
        controller = Harness(workspace, tmp_path / "run", model, task_kind="general")
        assert controller.run("Read source").status == "SUCCEEDED"
    else:
        controller = Harness(workspace, tmp_path / "run", FakeModel([
            tool_message("write", "write_file", {"path": "calculator.py", "content": "VALUE = 123\n"}),
        ]), writable_paths=("calculator.py",), task_kind="general")
        controller.run("Update source")
        if stage == "denied":
            controller.decide("write", False, "operator")
        elif stage == "unknown":
            spec = controller.gate.tools["write_file"]
            def unconfirmed(**kwargs):
                spec.handler(**kwargs)
                raise SandboxViolation("Unconfirmed result")
            controller.gate.tools["write_file"] = replace(spec, handler=unconfirmed)
            controller.decide("write", True, "operator")
            assert controller.resume().status == "WAITING_REVIEW"
    before = asdict(controller.state)
    with pytest.raises(ValueError, match="budget-stopped"):
        controller.extend_budget(**limits, actor="operator", reason="Must not bypass safeguards")
    assert asdict(controller.state) == before
    assert "extend-budget" not in controller.gate.tools and "extend_budget" not in controller.gate.tools


def test_host_review_closes_unknown_run_without_replaying_or_claiming_success(workspace, tmp_path):
    """人工核对后终止旧运行，未知动作仍保留，新进程不能把关闭状态重新开启。"""
    from dataclasses import replace
    from guarded_harness.harness import Harness
    from guarded_harness.sandbox import SandboxViolation
    from tests.support import FakeModel, tool_message
    controller = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("write", "write_file", {"path": "calculator.py", "content": "VALUE = 1\n"}),
    ]), writable_paths=("calculator.py",), task_kind="general")
    spec = controller.gate.tools["write_file"]
    def uncertain(**kwargs):
        spec.handler(**kwargs)
        raise SandboxViolation("Result not confirmed")
    controller.gate.tools["write_file"] = replace(spec, handler=uncertain)
    controller.run("Apply change")
    controller.decide("write", True, "operator")
    assert controller.resume().status == "WAITING_REVIEW"
    controller.close_review("Checked current file; continuing in a separate run", "operator")
    recovered = Harness.load(controller.run_dir)
    assert recovered.state.status == "STOPPED" and recovered.state.stop_reason == "HOST_REVIEW_CLOSED"
    assert recovered.state.actions["write"]["status"] == "UNKNOWN_RESULT"
    assert len(recovered.state.writes) == 1
    with pytest.raises(RuntimeError, match="cannot be resumed"):
        recovered.resume()


def test_model_plan_is_updated_and_used_after_new_process_resume(workspace, tmp_path):
    """模型计划、版本及游标跨进程恢复，并实际注入下一次模型调用上下文。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    run_dir = tmp_path / "run"
    plan = ["Inspect calculator.py", "Review and publish report", "Finalize"]
    harness = Harness(workspace, run_dir, FakeModel([
        tool_message("plan-create", "update_plan", {"steps": plan, "current_step": 0}),
        tool_message("read", "read_file", {"path": "calculator.py"}),
        tool_message("plan-progress", "update_plan", {"steps": plan, "current_step": 1}),
        tool_message("report", "write_report", {"content": "calculator.py defines add(a, b)."}),
    ]), max_steps=6)
    state = harness.run("Understand addition")
    assert state.plan == plan and state.plan_revision == 3 and state.plan_cursor == 1
    harness.decide("report", True, "operator")
    script = textwrap.dedent('''
        import json, sys
        from pathlib import Path
        from dataclasses import asdict
        from tests.support import FakeModel, tool_message
        from guarded_harness.harness import Harness
        class CapturingModel(FakeModel):
            def generate(self, messages, **kwargs):
                context = str(messages)
                assert 'Review and publish report' in context
                assert '"current_step": 1' in context
                assert '"revision": 3' in context
                return super().generate(messages, **kwargs)
        controller = Harness.load(Path(sys.argv[1]), CapturingModel([
            tool_message('plan-finish', 'update_plan', {'steps': ['Inspect calculator.py', 'Review and publish report', 'Finalize'], 'current_step': 2}),
            tool_message('final', 'final_answer', {'answer': 'See report.md'}),
        ]))
        assert controller.state.plan_cursor == 1
        print(json.dumps(asdict(controller.resume())))
    ''')
    child = subprocess.run([sys.executable, "-c", script, str(run_dir)], capture_output=True, text=True, timeout=20, check=True)
    restored = json.loads(child.stdout)
    assert restored["status"] == "SUCCEEDED" and restored["plan_status"] == "COMPLETED"
    assert restored["plan"] == plan and restored["plan_revision"] == 4 and restored["plan_cursor"] == len(plan)
    assert restored["steps_used"] == 6 and restored["tool_calls_used"] == 6
    events = [json.loads(line) for line in (run_dir / "audit.jsonl").read_text().splitlines()]
    assert {row["tool_call_id"] for row in events if row["event"] == "plan_updated"} == {"plan-create", "plan-progress", "plan-finish"}


def test_workspace_drift_marks_saved_plan_for_review(workspace, tmp_path):
    """工作区变化保留计划文字及历史进度，但不让旧计划作为当前完成依据。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    harness = Harness(workspace, tmp_path / "run", FakeModel([
        tool_message("read", "read_file", {"path": "calculator.py"}),
        tool_message("report", "write_report", {"content": "calculator.py defines add."}),
    ]))
    harness.run("Understand addition")
    plan = list(harness.state.plan)
    (workspace / "calculator.py").write_text("EXTERNAL = True\n")
    restored = Harness.load(harness.run_dir, FakeModel([]))
    assert restored.state.plan == plan
    assert restored.state.plan_status == "NEEDS_REVIEW"
    assert restored.resume().status == "WAITING_REVIEW"


@pytest.mark.parametrize("crash_point,max_steps,tamper", [
    ("action_result", 6, None),
    ("step_callback", 6, None),
    ("step_callback", 8, None),
    ("artifact_export", 6, None),
    ("step_callback", 6, "report"),
    ("step_callback", 6, "source"),
])
def test_final_result_recovers_without_model_or_tool_replay(workspace, tmp_path, unit_commands, crash_point, max_steps, tamper):
    """C3/H3：最终结果已提交后各收尾窗口退出，恢复不再消耗预算或重放写入。"""
    import textwrap
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    (workspace / 'calculator.py').write_text('def add(a, b): return a - b\n')
    replacement = 'def add(a, b): return a + b\n'
    report = 'calculator.py: fixed addition; unit failed before the change and passed afterward.\n'
    model = FakeModel([
        tool_message('baseline', 'run_test', {'name': 'unit'}),
        tool_message('fix', 'write_file', {'path': 'calculator.py', 'content': replacement}),
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('verify', 'run_test', {'name': 'unit'}),
        tool_message('report', 'write_report', {'content': report}),
    ])
    run_dir = tmp_path / 'run'
    controller = Harness(workspace, run_dir, model, max_steps=max_steps, task_kind='repair',
                         writable_paths=('calculator.py',), test_commands=unit_commands)
    assert controller.run('Fix add').status == 'WAITING_APPROVAL'
    controller.decide('fix', True, 'operator')
    assert controller.resume().status == 'WAITING_APPROVAL'
    controller.decide('report', True, 'operator')
    script = textwrap.dedent('''
        import os, sys
        from pathlib import Path
        import guarded_harness.harness as module
        from tests.support import FakeModel, tool_message
        controller = module.Harness.load(Path(sys.argv[1]), FakeModel([
            tool_message('final', 'final_answer', {'answer': 'See report.md'})
        ]))
        point = sys.argv[2]
        if point == 'action_result':
            original = controller.audit.record
            def record(event, tool_call_id=None, **kwargs):
                if event == 'executed' and tool_call_id == 'final':
                    os._exit(77)
                return original(event, tool_call_id, **kwargs)
            controller.audit.record = record
        elif point == 'step_callback':
            original = controller._agent.run
            def run(*args, **kwargs):
                original(*args, **kwargs)
                os._exit(77)
            controller._agent.run = run
        elif point == 'artifact_export':
            original = module.atomic_write_text
            def write(path, content):
                original(path, content)
                if path.name == 'test-results.json':
                    os._exit(77)
            module.atomic_write_text = write
        controller.resume()
    ''')
    child = subprocess.run([sys.executable, '-c', script, str(run_dir), crash_point],
                           capture_output=True, text=True, timeout=20)
    assert child.returncode == 77, child.stderr
    snapshot = Checkpoint(run_dir / 'checkpoint.json').load()
    assert snapshot.status == 'RUNNING'
    assert snapshot.actions['final']['status'] == 'SUCCEEDED'
    assert snapshot.steps_used == 6
    assert (run_dir / 'report.md').read_text() == report
    if tamper == 'report':
        (run_dir / 'report.md').write_text('Modified after final acceptance')
    elif tamper == 'source':
        (workspace / 'calculator.py').write_text('EXTERNAL = True\n')
    no_more_requests = FakeModel([])
    recovered = Harness.load(run_dir, no_more_requests)
    state = recovered.resume()
    assert no_more_requests.calls == 0
    assert state.steps_used == 6
    assert state.tool_calls_used == 6
    assert len(state.writes) == 1
    assert len(state.tests) == 2
    if tamper:
        assert state.status == 'WAITING_REVIEW'
        assert state.stop_reason == ('WORKSPACE_CHANGED' if tamper == 'source' else 'COMPLETION_EVIDENCE_CHANGED')
        assert not state.report_evidence['valid']
    else:
        assert (state.status, state.stop_reason) == ('SUCCEEDED', 'COMPLETED')
        assert state.answer == 'See report.md'
        assert (run_dir / 'report.md').read_text() == report
        assert json.loads((run_dir / 'test-results.json').read_text()) == state.tests
        assert (run_dir / 'changes.patch').read_text() == state.writes[0]['diff']
        assert (workspace / 'calculator.py').read_text() == replacement
        assert StateStore(run_dir / 'state.json').load() == Checkpoint(run_dir / 'checkpoint.json').load()
        events = [json.loads(line) for line in (run_dir / 'audit.jsonl').read_text().splitlines()]
        for call_id in ('fix', 'report'):
            assert sum(event['event'] == 'executed' and event['tool_call_id'] == call_id for event in events) == 1
        assert any(event['event'] == 'completion_recovered' for event in events)


def test_state_and_checkpoint_preserve_business_fields(tmp_path):
    """完整往返保存运行配置、证据、预算、审批字段与停止原因。"""
    state = RunState("run-1", "understand", "/example/repo", 10)
    state.plan = ["read source"]
    state.steps_used = 3
    state.tool_calls_used = 5
    state.evidence = [{"path": "a.py", "sha256": "example"}]
    state.approvals = {"write-1": "pending"}
    state.pending_action = {"tool_call_id": "write-1"}
    state.status = "STOPPED"
    state.stop_reason = "MANUAL_STOP"
    store = StateStore(tmp_path / "state.json")
    checkpoint = Checkpoint(tmp_path / "checkpoint.json")
    store.save(state)
    checkpoint.save(state)
    assert asdict(store.load()) == asdict(state)
    assert asdict(checkpoint.load()) == asdict(state)


def test_snapshot_can_be_loaded_in_new_process_without_resetting_counter(tmp_path):
    """使用同一解释器启动独立进程，验证读取快照不会清零已有计数。"""
    path = tmp_path / "checkpoint.json"
    state = RunState("run-1", "understand", "/example/repo", 10, steps_used=3)
    Checkpoint(path).save(state)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; from pathlib import Path; from dataclasses import asdict; "
            "from guarded_harness.checkpoint import Checkpoint; "
            "print(json.dumps(asdict(Checkpoint(Path(sys.argv[1])).load())))",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout)["steps_used"] == 3
    # 本断言只验证跨进程反序列化，不覆盖 Agent 上下文恢复或继续执行。


def create_waiting_write(workspace, run_dir):
    """创建已提出写入但尚未执行的运行，供独立进程恢复用例复用。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    content = 'VALUE = 123\n'
    model = FakeModel([tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': content})])
    harness = Harness(workspace, run_dir, model, max_steps=6, writable_paths=('calculator.py',), task_kind="general")
    harness.run('Update calculator.py')
    return harness, content


def test_new_process_resumes_approved_write_without_reset_or_reexecution(workspace, tmp_path):
    """C3：换进程恢复后完成写入，重复调用被拒绝，步骤和工具计数继续累计。"""
    import textwrap

    run_dir = tmp_path / 'run'
    harness, content = create_waiting_write(workspace, run_dir)
    harness.decide('write-1', True, 'operator')
    script = textwrap.dedent('''
        import json, sys
        from dataclasses import asdict
        from pathlib import Path
        from tests.support import FakeModel, tool_message
        from guarded_harness.harness import Harness
        class CapturingModel(FakeModel):
            def generate(self, messages, **kwargs):
                if self.calls == 0:
                    self.first_messages = str(messages)
                return super().generate(messages, **kwargs)
        model = CapturingModel([
            tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': sys.argv[2]}),
            tool_message('read-2', 'read_file', {'path': 'calculator.py'}),
            tool_message('final-2', 'final_answer', {'answer': 'Updated calculator.py.'}),
        ])
        controller = Harness.load(Path(sys.argv[1]), model)
        state = controller.resume()
        assert 'write-1' in model.first_messages
        assert 'before_sha256' in model.first_messages
        print(json.dumps(asdict(state)))
    ''')
    result = subprocess.run([sys.executable, '-c', script, str(run_dir), content], capture_output=True, text=True, check=True, timeout=20)
    state = json.loads(result.stdout)
    assert state['status'] == 'SUCCEEDED'
    assert state['steps_used'] == 4
    assert state['tool_calls_used'] == 4
    assert len(state['writes']) == 1
    assert state['run_id'] == harness.state.run_id
    assert (workspace / 'calculator.py').read_text() == content
    assert state['actions']['write-1']['status'] == 'SUCCEEDED'
    events = [json.loads(line) for line in (run_dir / 'audit.jsonl').read_text().splitlines()]
    assert len([item for item in events if item['event'] == 'executed' and item['tool_call_id'] == 'write-1']) == 1


def test_crash_after_write_before_result_commit_becomes_unknown(workspace, tmp_path):
    """C2：写入完成后强制退出，恢复标记 UNKNOWN_RESULT 而不自动重放。"""
    import textwrap
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    run_dir = tmp_path / 'run'
    harness, content = create_waiting_write(workspace, run_dir)
    harness.decide('write-1', True, 'operator')
    script = textwrap.dedent('''
        import os, sys
        from pathlib import Path
        from tests.support import FakeModel
        from guarded_harness.harness import Harness
        controller = Harness.load(Path(sys.argv[1]), FakeModel([]))
        original = controller.sandbox.write_file
        def crash_after_write(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        controller.sandbox.write_file = crash_after_write
        controller.resume()
    ''')
    result = subprocess.run([sys.executable, '-c', script, str(run_dir)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 73, result.stderr
    assert (workspace / 'calculator.py').read_text() == content
    controller = Harness.load(run_dir, FakeModel([]))
    assert controller.state.status == 'WAITING_REVIEW'
    assert controller.state.stop_reason == 'UNKNOWN_RESULT'
    assert controller.state.actions['write-1']['status'] == 'UNKNOWN_RESULT'
    revision = controller.state.revision
    assert controller.resume().stop_reason == 'UNKNOWN_RESULT'
    assert controller.state.revision == revision
    assert not controller.state.writes


def test_workspace_changes_invalidate_old_approval_and_evidence(workspace, tmp_path):
    """C4：恢复时检测目标版本变化，撤销旧审批和读取证据，停止等待核对。"""
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    model = FakeModel([
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('write-1', 'write_file', {'path': 'calculator.py', 'content': 'VALUE = 1\n'}),
    ])
    run_dir = tmp_path / 'run'
    controller = Harness(workspace, run_dir, model, writable_paths=('calculator.py',), task_kind="general")
    controller.run('Update calculator.py')
    controller.decide('write-1', True, 'operator')
    (workspace / 'calculator.py').write_text('EXTERNAL_CHANGE = True\n')
    recovered = Harness.load(run_dir, FakeModel([]))
    assert recovered.state.status == 'WAITING_REVIEW'
    assert recovered.state.stop_reason == 'WORKSPACE_CHANGED'
    assert recovered.state.approvals['write-1']['status'] == 'INVALIDATED'
    assert not recovered.state.evidence
    assert not recovered.state.writes
    recovered.resume()
    assert (workspace / 'calculator.py').read_text() == 'EXTERNAL_CHANGE = True\n'


def test_checkpoint_is_authoritative_if_state_view_is_outdated(workspace, tmp_path):
    """恢复使用先提交的权威检查点，不让较旧状态视图抹掉已批准动作。"""
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    run_dir = tmp_path / 'run'
    controller, _ = create_waiting_write(workspace, run_dir)
    old_view = (run_dir / 'state.json').read_bytes()
    controller.decide('write-1', True, 'operator')
    (run_dir / 'state.json').write_bytes(old_view)
    recovered = Harness.load(run_dir, FakeModel([]))
    assert recovered.state.approvals['write-1']['status'] == 'APPROVED'
    assert json.loads((run_dir / 'state.json').read_text())['revision'] == recovered.state.revision


def test_stale_controller_cannot_overwrite_newer_decision(workspace, tmp_path):
    """陈旧实例必须重新加载，不能覆盖其他控制器已提交的状态。"""
    import pytest
    from tests.support import FakeModel
    from guarded_harness.harness import Harness

    run_dir = tmp_path / 'run'
    original, _ = create_waiting_write(workspace, run_dir)
    latest = Harness.load(run_dir, FakeModel([]))
    with pytest.raises(RuntimeError, match='reload'):
        original.decide('write-1', True, 'operator')
    latest.decide('write-1', False, 'operator')
    assert latest.state.stop_reason == 'APPROVAL_DENIED'


def test_confirmed_write_is_not_replayed_after_exit_before_step_callback(workspace, tmp_path):
    """执行结果已提交但步骤回调未运行时退出，恢复后直接使用已知结果继续。"""
    import textwrap
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness

    run_dir = tmp_path / 'run'
    controller, content = create_waiting_write(workspace, run_dir)
    controller.decide('write-1', True, 'operator')
    script = textwrap.dedent('''
        import os, sys
        from pathlib import Path
        from tests.support import FakeModel
        from guarded_harness.harness import Harness
        controller = Harness.load(Path(sys.argv[1]), FakeModel([]))
        original = controller.audit.record
        def exit_after_result(event, tool_call_id=None, **kwargs):
            if event == 'executed' and tool_call_id == 'write-1':
                os._exit(74)
            return original(event, tool_call_id, **kwargs)
        controller.audit.record = exit_after_result
        controller.resume()
    ''')
    result = subprocess.run([sys.executable, '-c', script, str(run_dir)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 74, result.stderr
    model = FakeModel([
        tool_message('read-1', 'read_file', {'path': 'calculator.py'}),
        tool_message('final-1', 'final_answer', {'answer': 'Updated calculator.py.'}),
    ])
    recovered = Harness.load(run_dir, model)
    assert recovered.state.pending_action is None
    assert recovered.state.actions['write-1']['status'] == 'SUCCEEDED'
    assert len(recovered.state.writes) == 1
    outcome = json.loads(recovered.state.steps[0]['observations'])
    assert outcome['ok']
    recovered.resume()
    assert recovered.state.status == 'SUCCEEDED'
    assert len(recovered.state.writes) == 1
    assert (workspace / 'calculator.py').read_text() == content


def test_external_change_between_approval_and_execution_blocks_write(workspace, tmp_path):
    """同一控制器批准后若工作区变化，resume 仍必须重新校验版本。"""
    controller, _ = create_waiting_write(workspace, tmp_path / 'run')
    controller.decide('write-1', True, 'operator')
    (workspace / 'calculator.py').write_text('EXTERNAL = True\n')
    state = controller.resume()
    assert state.status == 'WAITING_REVIEW'
    assert state.approvals['write-1']['status'] == 'INVALIDATED'
    assert not state.writes
    assert (workspace / 'calculator.py').read_text() == 'EXTERNAL = True\n'


def test_concurrent_controller_is_rejected_by_process_lock(workspace, tmp_path):
    """运行锁覆盖跨进程控制器，防止两个宿主同时批准或执行同一动作。"""
    import fcntl
    import textwrap

    run_dir = tmp_path / 'run'
    create_waiting_write(workspace, run_dir)
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from tests.support import FakeModel
        from guarded_harness.harness import Harness
        try:
            Harness.load(Path(sys.argv[1]), FakeModel([]))
        except RuntimeError as error:
            assert 'Another controller' in str(error)
            sys.exit(42)
    ''')
    with (run_dir / '.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        result = subprocess.run([sys.executable, '-c', script, str(run_dir)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 42, result.stderr


def test_returned_rejection_is_durable_before_step_callback(tmp_path):
    """恢复窗口：拒绝已返回并已审计，但步骤回调前退出时仍应保留调用预算。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    (workspace / 'source.py').write_text('VALUE = 1\n')
    script = textwrap.dedent('''
        import os,sys
        from pathlib import Path
        from guarded_harness.harness import Harness
        from tests.support import FakeModel, tool_message
        controller = Harness(Path(sys.argv[1]),Path(sys.argv[2]),FakeModel([
            tool_message('invalid-call','read_file',{})
        ]),max_steps=3,max_tool_calls=2)
        original = controller.gate.dispatch
        def dispatch(*args,**kwargs):
            result = original(*args,**kwargs)
            if result['error']['code'] == 'INVALID_ARGUMENTS':
                os._exit(81)
            return result
        controller.gate.dispatch = dispatch
        controller.run('Inspect source')
    ''')
    run_dir = tmp_path / 'run'
    child = subprocess.run([sys.executable, '-c', script, str(workspace), str(run_dir)], capture_output=True, text=True, timeout=15)
    assert child.returncode == 81, child.stderr
    audit = [json.loads(line) for line in (run_dir / 'audit.jsonl').read_text().splitlines()]
    assert any(event['event'] == 'rejected' for event in audit)
    state = Checkpoint(run_dir / 'checkpoint.json').load()
    assert state.tool_calls_used == 1, state.tool_calls_used
    assert 'invalid-call' in state.seen_call_ids
    assert state.steps_used == 1
    assert state.actions['invalid-call']['result']['error']['code'] == 'INVALID_ARGUMENTS'
    assert state.rejections[0]['result'] == state.actions['invalid-call']['result']
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    model = FakeModel([
        tool_message('invalid-call', 'read_file', {}),
        tool_message('extra-read', 'read_file', {'path': 'source.py'}),
    ])
    recovered = Harness.load(run_dir, model)
    assert json.loads(recovered.state.steps[0]['observations'])['error']['code'] == 'INVALID_ARGUMENTS'
    result = recovered.resume()
    assert result.tool_calls_used == 2
    assert result.stop_reason == 'MAX_TOOL_CALLS'
    assert result.actions['extra-read']['status'] == 'REJECTED'
    again = Harness.load(run_dir, FakeModel([]))
    assert json.loads(again.state.steps[1]['observations'])['error']['code'] == 'DUPLICATE_CALL'
    assert again.state.actions['invalid-call']['result']['error']['code'] == 'INVALID_ARGUMENTS'


def test_resume_does_not_adopt_a_symlink_redirected_workspace(workspace, tmp_path):
    """跨进程加载时不能把旧工作区路径的新软链接落点当作已授权根。"""
    import shutil
    from tests.support import FakeModel, tool_message
    from guarded_harness.harness import Harness
    from guarded_harness.sandbox import SandboxViolation

    run_dir = tmp_path / 'run'
    controller = Harness(workspace, run_dir, FakeModel([
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('report', 'write_report', {'content': 'calculator.py: addition'}),
    ]))
    assert controller.run('Inspect source').status == 'WAITING_APPROVAL'
    outside = tmp_path / 'outside'
    shutil.copytree(workspace, outside)
    workspace.rename(tmp_path / 'original-repo')
    workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxViolation, match='different root'):
        Harness.load(run_dir, FakeModel([]))
