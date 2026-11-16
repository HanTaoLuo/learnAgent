"""验证文件真实落点、敏感路径及读取大小约束。"""

import json
import pytest
from tests.support import FakeModel, tool_message
from guarded_harness.harness import Harness
from guarded_harness.sandbox import Sandbox, SandboxViolation


def test_escape_and_symlink_escape_are_denied(tmp_path, workspace):
    """拒绝相对路径越界、软链接越界和绝对路径访问。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    for path in ("../outside.txt", "link.txt", str(outside)):
        with pytest.raises(SandboxViolation):
            Sandbox(workspace).read_file(path)


def test_sensitive_paths_and_symlink_alias_are_denied(workspace):
    """请求路径和软链接落点中的敏感文件均受同一拒绝规则约束。"""
    secret = workspace / ".env"
    secret.write_text("TEST_SECRET=example", encoding="utf-8")
    (workspace / "alias.txt").symlink_to(secret)
    for path in (".env", "alias.txt", ".git/config", "artifacts/../../.env"):
        with pytest.raises(SandboxViolation):
            Sandbox(workspace).read_file(path)


def test_read_limit_is_enforced(workspace):
    """超出原始字节上限时拒绝返回文件内容。"""
    with pytest.raises(SandboxViolation, match="limit"):
        Sandbox(workspace, max_read_bytes=4).read_file("calculator.py")


def test_write_rejects_symlink_and_preserves_external_content(workspace, tmp_path):
    """即便路径在写入白名单中，软链接目标仍不能进入替换流程。"""
    outside = tmp_path / 'outside.py'
    outside.write_text('ORIGINAL = True\n')
    (workspace / 'link.py').symlink_to(outside)
    with pytest.raises(SandboxViolation, match='Symlinks'):
        Sandbox(workspace).write_file('link.py', 'CHANGED = True\n', ['link.py'])
    assert outside.read_text() == 'ORIGINAL = True\n'


def test_atomic_write_creates_allowed_file_and_returns_hashes(workspace):
    """允许创建父目录已存在的新文件，后续替换记录前后内容哈希。"""
    import hashlib

    sandbox = Sandbox(workspace)
    first = sandbox.write_file('new.py', 'X = 1\n', ['new.py'])
    second = sandbox.write_file('new.py', 'X = 2\n', ['new.py'])
    assert first['before_sha256'] is None
    assert second['before_sha256'] == first['sha256']
    assert second['sha256'] == hashlib.sha256(b'X = 2\n').hexdigest()
    assert (workspace / 'new.py').read_text() == 'X = 2\n'


def test_workspace_fingerprint_tracks_source_changes_but_ignores_runtime_cache(workspace):
    """工作区版本使用内容而非修改时间，Python 缓存不触发旧审批失效。"""
    sandbox = Sandbox(workspace)
    original = sandbox.fingerprint()
    (workspace / '__pycache__').mkdir()
    (workspace / '__pycache__' / 'module.pyc').write_bytes(b'cache')
    assert sandbox.fingerprint() == original
    (workspace / 'calculator.py').write_text('VALUE = 1\n')
    assert sandbox.fingerprint() != original


def test_fingerprint_tracks_allowed_directories_with_env_prefix(workspace):
    """指纹排除规则与访问规则保持一致，普通 .env 前缀目录仍参与版本核对。"""
    directory = workspace / '.environment'
    directory.mkdir()
    source = directory / 'config.py'
    source.write_text('VALUE = 1\n')
    sandbox = Sandbox(workspace)
    initial = sandbox.fingerprint()
    assert sandbox.read_file('.environment/config.py')['content'] == 'VALUE = 1\n'
    source.write_text('VALUE = 2\n')
    assert sandbox.fingerprint() != initial


def test_unregistered_test_command_is_never_spawned(workspace, tmp_path, monkeypatch):
    """S3：未配置命令无法启动进程，拒绝发生在 Popen 之前。"""
    from unittest.mock import Mock
    import guarded_harness.sandbox as module

    spawn = Mock()
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    with pytest.raises(SandboxViolation):
        Sandbox(workspace).run_test('rm -rf /', {}, tmp_path / 'runtime')
    spawn.assert_not_called()


def test_test_process_uses_filtered_environment(workspace, tmp_path, monkeypatch):
    """子进程环境不继承凭据、代理或 Python/pytest 注入参数。"""
    import json
    import sys

    for key in ('OPENAI_API_KEY', 'HTTP_PROXY', 'PYTHONPATH', 'PYTEST_ADDOPTS'):
        monkeypatch.setenv(key, 'must-not-propagate')
    commands = {'environment': [sys.executable, '-c', 'import os,json; print(json.dumps(dict(os.environ)))']}
    result = Sandbox(workspace).run_test('environment', commands, tmp_path / 'runtime')
    assert result['status'] == 'PASSED'
    environment = json.loads(result['output'])
    assert not any(key in environment for key in ('OPENAI_API_KEY', 'HTTP_PROXY', 'PYTHONPATH', 'PYTEST_ADDOPTS'))
    assert environment['HOME'] == str(tmp_path / 'runtime')
    assert environment['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] == '1'


def test_test_output_is_bounded_and_process_is_terminated(workspace, tmp_path):
    """输出超限终止真实进程，缓冲区只保留配置字节数。"""
    import sys

    commands = {'flood': [sys.executable, '-c', 'import os,time; os.write(1,b"x"*65536); time.sleep(30)']}
    result = Sandbox(workspace).run_test('flood', commands, tmp_path / 'runtime', output_limit=256, timeout_seconds=3)
    assert result['status'] == 'OUTPUT_LIMIT'
    assert result['output_bytes'] == 256
    assert result['output'] == 'x' * 256
    assert result['returncode'] is not None
    assert result['duration_seconds'] < 2


def command_with_descendant(liveness_pipe, leader_exits=False):
    """创建持有 FIFO 写端且忽略 SIGTERM 的派生进程，以 EOF 验证其真实退出。"""
    import sys

    child = 'import os,signal,time,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); live=os.open(sys.argv[1],os.O_WRONLY); print("ready",flush=True); time.sleep(30)'
    parent = f'import subprocess,sys,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); child=subprocess.Popen([sys.executable,"-c",{child!r},{str(liveness_pipe)!r}],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL); child.stdout.readline(); print(child.pid,flush=True)'
    if not leader_exits:
        parent += '; time.sleep(30)'
    return {'slow': [sys.executable, '-c', parent]}


def assert_liveness_pipe_closed(descriptor):
    """读取 FIFO EOF 证明唯一持有写端的进程已退出，不依赖进程表访问权限。"""
    import os
    import time

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            assert os.read(descriptor, 1) == b''
            return
        except BlockingIOError:
            time.sleep(0.01)
    raise AssertionError('Descendant still holds the liveness pipe open')


@pytest.mark.parametrize('reason', ['TIMEOUT', 'CANCELLED'])
def test_timeout_and_cancellation_kill_descendant_group(workspace, tmp_path, reason):
    """S4：组长和派生进程忽略 SIGTERM 时，超时及取消仍导致派生进程关闭全部 FD。"""
    import os
    import threading

    pipe = tmp_path / 'liveness.fifo'
    os.mkfifo(pipe)
    descriptor = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    cancel = threading.Event()
    timer = threading.Timer(1, cancel.set) if reason == 'CANCELLED' else None
    if timer:
        timer.start()
    try:
        result = Sandbox(workspace).run_test('slow', command_with_descendant(pipe), tmp_path / 'runtime', timeout_seconds=1 if reason == 'TIMEOUT' else 5, cancel=cancel)
        assert result['status'] == reason
        assert result['returncode'] is not None
        assert result['duration_seconds'] < 3
        # 只有派生进程成功打开 FIFO 并发出 ready，组长才会输出其 PID。
        assert result['output'].strip().isdigit()
        assert_liveness_pipe_closed(descriptor)
    finally:
        os.close(descriptor)
        if timer:
            timer.cancel()


def test_real_pytest_failure_and_success_return_exit_codes(workspace, tmp_path, unit_commands):
    """测试结果来自真实 pytest 退出码，不能由固定模型响应替代。"""
    sandbox = Sandbox(workspace)
    passed = sandbox.run_test('unit', unit_commands, tmp_path / 'runtime')
    assert passed['status'] == 'PASSED'
    assert passed['returncode'] == 0
    (workspace / 'calculator.py').write_text('def add(a,b): return a - b\n')
    failed = sandbox.run_test('unit', unit_commands, tmp_path / 'runtime')
    assert failed['status'] == 'FAILED'
    assert failed['returncode'] != 0
    assert 'failed' in failed['output']


def test_exited_group_leader_does_not_leave_a_running_descendant(workspace, tmp_path):
    """即使组长已正常退出，也清理仍属于该组且忽略 SIGTERM 的派生进程。"""
    import os

    pipe = tmp_path / 'liveness.fifo'
    os.mkfifo(pipe)
    descriptor = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)
    try:
        result = Sandbox(workspace).run_test('slow', command_with_descendant(pipe, leader_exits=True), tmp_path / 'runtime', timeout_seconds=3)
        assert result['status'] == 'PASSED'
        assert result['output'].strip().isdigit()
        assert_liveness_pipe_closed(descriptor)
    finally:
        os.close(descriptor)



def test_symlink_swap_between_check_and_open_is_denied(tmp_path):
    """S2 边界：仅访问本用例创建的文件，复现检查后替换链接的并发窗口。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    target = workspace / 'source.txt'
    target.write_text('inside')
    outside = tmp_path / 'outside-fixture.txt'
    outside.write_text('OUTSIDE_FIXTURE')
    sandbox = Sandbox(workspace)
    original = sandbox.resolve_read
    def swap_after_check(path):
        resolved = original(path)
        target.unlink()
        target.symlink_to(outside)
        return resolved
    sandbox.resolve_read = swap_after_check
    with pytest.raises((SandboxViolation, OSError)):
        sandbox.read_file('source.txt')


@pytest.mark.parametrize('operation', ['read', 'write', 'list'])
def test_parent_symlink_swap_after_validation_cannot_escape(tmp_path, operation):
    """路径检查后替换整个父目录，也不能读取外部内容、列出外部名称或越界写入。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    parent = workspace / 'package'
    parent.mkdir()
    (parent / 'source.txt').write_text('inside')
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'source.txt'
    marker.write_text('OUTSIDE_FIXTURE')
    sandbox = Sandbox(workspace)
    method = {'read': 'resolve_read', 'write': 'validate_write', 'list': 'resolve_directory'}[operation]
    original = getattr(sandbox, method)
    def swap(*args, **kwargs):
        resolved = original(*args, **kwargs)
        parent.rename(workspace / 'original-package')
        parent.symlink_to(outside, target_is_directory=True)
        return resolved
    setattr(sandbox, method, swap)
    with pytest.raises((SandboxViolation, OSError)):
        if operation == 'read':
            sandbox.read_file('package/source.txt')
        elif operation == 'write':
            sandbox.write_file('package/new.txt', 'replacement', ['package/new.txt'])
        else:
            sandbox.list_files('package')
    assert marker.read_text() == 'OUTSIDE_FIXTURE'
    assert not (outside / 'new.txt').exists()


def test_write_parent_swap_after_temp_sync_aborts_and_cleans_original_directory(tmp_path, monkeypatch):
    """临时内容同步后父目录被替换时拒绝发布，并通过原目录描述符清理临时文件。"""
    import os
    import stat
    import guarded_harness.sandbox as module

    workspace = tmp_path / 'repo'
    workspace.mkdir()
    parent = workspace / 'package'
    parent.mkdir()
    (parent / 'source.txt').write_text('inside')
    parked = workspace / 'original-package'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'source.txt').write_text('outside')
    sandbox = Sandbox(workspace)
    fsync = os.fsync
    swapped = False
    def swap_after_sync(descriptor):
        nonlocal swapped
        fsync(descriptor)
        if not swapped and stat.S_ISREG(os.fstat(descriptor).st_mode):
            swapped = True
            parent.rename(parked)
            parent.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(module.os, 'fsync', swap_after_sync)
    with pytest.raises((SandboxViolation, OSError)):
        sandbox.write_file('package/source.txt', 'replacement', ['package/source.txt'])
    assert swapped
    assert (outside / 'source.txt').read_text() == 'outside'
    assert (parked / 'source.txt').read_text() == 'inside'
    assert sorted(path.name for path in parked.iterdir()) == ['source.txt']


def test_search_does_not_return_content_from_swapped_symlink(tmp_path):
    """搜索与完整读取使用相同的实际打开边界，替换后的外部文本不能进入摘要。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    target = workspace / 'source.txt'
    target.write_text('inside')
    outside = tmp_path / 'outside.txt'
    outside.write_text('OUTSIDE_FIXTURE')
    sandbox = Sandbox(workspace)
    original = sandbox.resolve_read
    def swap(path):
        resolved = original(path)
        target.unlink()
        target.symlink_to(outside)
        return resolved
    sandbox.resolve_read = swap
    assert sandbox.search_code('OUTSIDE_FIXTURE', '.')['matches'] == []


@pytest.mark.parametrize('operation', ['find', 'fingerprint'])
def test_replaced_workspace_root_is_rejected(tmp_path, operation):
    """工作区根在初始化后被外部目录链接替换时，发现与指纹计算均不能越界。"""
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'private-fixture.txt').write_text('outside')
    sandbox = Sandbox(workspace)
    workspace.rename(tmp_path / 'original-repo')
    workspace.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxViolation):
        sandbox.find_files('*.txt') if operation == 'find' else sandbox.fingerprint()


def test_internal_read_symlink_retains_validated_target_behavior(workspace):
    """允许指向工作区内普通文件的读取别名，实际访问仍绑定校验后的目标。"""
    (workspace / 'alias.py').symlink_to(workspace / 'calculator.py')
    sandbox = Sandbox(workspace)
    assert sandbox.read_file('alias.py')['content'] == sandbox.read_file('calculator.py')['content']






def test_discovery_hides_sensitive_paths_and_symlinks(workspace, tmp_path):
    """列目录、查找和搜索均不能暴露敏感文件或通过软链接读取外部内容。"""
    (workspace / '.env').write_text('SECRET=outside-needle')
    (workspace / '.git').mkdir()
    (workspace / '.git' / 'config').write_text('outside-needle')
    outside = tmp_path / 'outside.py'
    outside.write_text('outside-needle')
    (workspace / 'alias.py').symlink_to(outside)
    sandbox = Sandbox(workspace)
    listing = sandbox.list_files('.')
    assert not {'.env', '.git', 'alias.py'} & {item['path'] for item in listing['entries']}
    assert 'alias.py' not in {item['path'] for item in sandbox.find_files('*.py')['files']}
    assert not sandbox.search_code('outside-needle', '.')['matches']
    for path in ('../', str(tmp_path), '.git'):
        with pytest.raises(SandboxViolation):
            sandbox.list_files(path)


def test_find_and_literal_search_return_source_paths_and_line_numbers(workspace):
    """glob 支持根目录文件，搜索返回真实行号而不是正则表达式匹配。"""
    (workspace / 'pattern.py').write_text('first\nvalue = ".*"\nthird\n')
    sandbox = Sandbox(workspace)
    paths = {item['path'] for item in sandbox.find_files('**/*.py')['files']}
    assert {'calculator.py', 'test_calculator.py', 'pattern.py'} <= paths
    matches = sandbox.search_code('.*', '.')['matches']
    assert matches == [{'path': 'pattern.py', 'line': 2, 'text': 'value = ".*"'}]


def test_discovery_reports_truncated_results(workspace):
    """结果条目受限且显式标记不完整，不能将截断结果解释为全仓库结果。"""
    for index in range(250):
        (workspace / f'file-{index}.txt').write_text('needle\n')
    sandbox = Sandbox(workspace)
    assert len(sandbox.list_files('.')['entries']) == 200
    assert sandbox.list_files('.')['truncated']
    assert len(sandbox.find_files('*.txt')['files']) == 200
    search = sandbox.search_code('needle', '.')
    assert len(search['matches']) == 100
    assert search['truncated']
    assert len(json.dumps(search).encode()) < 64 * 1024


def test_search_enforces_total_read_budget_and_skips_binary(workspace):
    """大批无匹配文件也受读取预算约束，二进制内容不作为源码摘要返回。"""
    for index in range(20):
        (workspace / f'large-{index}.txt').write_bytes(b'z' * (64 * 1024))
    (workspace / 'binary.bin').write_bytes(b'needle\0content')
    result = Sandbox(workspace).search_code('needle', '.')
    assert result['bytes_read'] <= 1024 * 1024
    assert result['truncated']
    assert not result['matches']


def test_discovery_calls_use_gate_and_do_not_replace_read_evidence(workspace, tmp_path):
    """发现结果经过 Gate 与审计，但摘要不足以满足最终答复的源码读取条件。"""
    model = FakeModel([
        tool_message('list', 'list_files', {'path': '.'}),
        tool_message('find', 'find_files', {'pattern': '*.py'}),
        tool_message('search', 'search_code', {'query': 'def add', 'path': '.'}),
        tool_message('read', 'read_file', {'path': 'calculator.py'}),
        tool_message('final', 'final_answer', {'answer': 'add returns a + b'}),
    ])
    harness = Harness(workspace, tmp_path / 'run', model, max_steps=6, task_kind="general")
    state = harness.run('Locate and explain the addition function')
    assert state.status == 'SUCCEEDED'
    assert state.tool_calls_used == 5
    assert len(state.evidence) == 1
    events = [json.loads(line) for line in (tmp_path / 'run' / 'audit.jsonl').read_text().splitlines()]
    assert {'list', 'find', 'search'} <= {item['tool_call_id'] for item in events if item['event'] == 'executed'}
