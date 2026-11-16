"""验证最终答复不能绕过读取证据和内容一致性检查。"""

import pytest
from tests.support import FakeModel
from guarded_harness.harness import Harness


def test_final_answer_without_evidence_is_rejected(workspace, tmp_path):
    """没有读取证据时通过 Gate 提交答案应返回错误，且不生成报告。"""
    harness = Harness(workspace, tmp_path / "run", FakeModel([]))
    result = harness.gate.dispatch("final-1", "final_answer", {"answer": "Done"})
    assert not result["ok"]
    assert result["tool_call_id"] == "final-1"
    assert "evidence" in result["error"]["message"]
    assert not (tmp_path / "run" / "report.md").exists()


def test_changing_source_invalidates_read_evidence(workspace, tmp_path):
    """读取完成后修改源码，旧内容哈希必须使完成检查失败。"""
    harness = Harness(workspace, tmp_path / "run", FakeModel([]))
    assert harness.gate.dispatch("read-1", "read_file", {"path": "calculator.py"})["ok"]
    (workspace / "calculator.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        harness.validate_completion("add returns a + b")


def test_empty_answer_is_rejected(workspace, tmp_path):
    """纯空白答复不满足最终答案的文本要求。"""
    harness = Harness(workspace, tmp_path / "run", FakeModel([]))
    with pytest.raises(ValueError, match="non-empty"):
        harness.validate_completion(" ")


def prepare_repaired_controller(workspace, run_dir, commands):
    """建立真实失败与修复证据，并批准独立报告，返回可直接校验的控制器。"""
    from tests.support import tool_message

    (workspace / 'calculator.py').write_text('def add(a,b): return a - b\n')
    replacement = 'def add(a,b): return a + b\n'
    model = FakeModel([
        tool_message('baseline', 'run_test', {'name': 'unit'}),
        tool_message('fix', 'write_file', {'path': 'calculator.py', 'content': replacement}),
    ])
    harness = Harness(workspace, run_dir, model, max_steps=12, task_kind='repair', writable_paths=('calculator.py',), test_commands=commands)
    harness.run('Fix add')
    harness.decide('fix', True, 'operator')
    assert harness.gate.execute_pending()['ok']
    assert harness.gate.dispatch('read-1', 'read_file', {'path': 'calculator.py'})['ok']
    assert harness.gate.dispatch('retest-1', 'run_test', {'name': 'unit'})['result']['status'] == 'PASSED'
    report = 'calculator.py repaired. Tests: ' + ', '.join(commands) + '.\n'
    assert harness.gate.dispatch('report-1', 'write_report', {'content': report})['error']['code'] == 'APPROVAL_REQUIRED'
    harness.decide('report-1', True, 'operator')
    assert harness.gate.execute_pending()['ok']
    return harness, report


def test_passed_tests_become_stale_after_another_approved_write(workspace, tmp_path, unit_commands):
    """H2：通过后再修改源码，旧测试失效；当前版本重新执行通过后才能完成。"""
    harness, report = prepare_repaired_controller(workspace, tmp_path / 'run', unit_commands)
    harness.validate_completion('Done')
    harness.gate.dispatch('write-2', 'write_file', {'path': 'calculator.py', 'content': 'def add(a,b):\n    return a + b\n'})
    harness.decide('write-2', True, 'operator')
    harness.gate.execute_pending()
    harness.gate.dispatch('read-2', 'read_file', {'path': 'calculator.py'})
    harness.gate.dispatch('report-2', 'write_report', {'content': report})
    harness.decide('report-2', True, 'operator')
    harness.gate.execute_pending()
    with pytest.raises(ValueError, match='rerun stale tests'):
        harness.validate_completion('Done')
    assert harness.gate.dispatch('retest-2', 'run_test', {'name': 'unit'})['result']['status'] == 'PASSED'
    harness.validate_completion('Done')


def test_changed_report_artifact_cannot_use_old_approval(workspace, tmp_path, unit_commands):
    """报告在批准后发生变化时拒绝最终答案，不能仅检查文件是否存在。"""
    harness, _ = prepare_repaired_controller(workspace, tmp_path / 'run', unit_commands)
    (tmp_path / 'run' / 'report.md').write_text('calculator.py and unit: altered report\n')
    with pytest.raises(ValueError, match='stale or changed'):
        harness.validate_completion('Done')


def test_all_required_commands_must_pass(workspace, tmp_path, unit_commands):
    """只通过一个命令不能代替完整必测集合，缺失结果明确阻止完成。"""
    commands = {**unit_commands, 'regression': unit_commands['unit']}
    harness, _ = prepare_repaired_controller(workspace, tmp_path / 'run', commands)
    with pytest.raises(ValueError, match='regression'):
        harness.validate_completion('Done')


def test_understanding_mode_rejects_source_write_capabilities(workspace, tmp_path):
    """代码理解模式在装配时拒绝源码写入白名单。"""
    with pytest.raises(ValueError, match='cannot modify'):
        Harness(workspace, tmp_path / 'run', FakeModel([]), task_kind='understand', writable_paths=('calculator.py',))


def test_latest_failed_result_cannot_reuse_an_earlier_pass(workspace, tmp_path, unit_commands):
    """相同代码和命令的最新结果失败时，旧通过记录不能满足必测条件。"""
    import sys

    failure_flag = tmp_path / 'force_failure'
    code = f'import pathlib,sys,pytest; sys.exit(1 if pathlib.Path({str(failure_flag)!r}).exists() else pytest.main(["-q","-p","no:cacheprovider","test_calculator.py"]))'
    commands = {'unit': [sys.executable, '-c', code]}
    harness, _ = prepare_repaired_controller(workspace, tmp_path / 'run', commands)
    harness.validate_completion('Done')
    failure_flag.write_text('fail')
    outcome = harness.gate.dispatch('failed-retest', 'run_test', {'name': 'unit'})
    assert outcome['result']['status'] == 'FAILED'
    assert outcome['result']['valid']
    with pytest.raises(ValueError, match='must pass'):
        harness.validate_completion('Done')
