"""回归测试共享夹具，隔离目标仓库文件的修改。"""

import inspect
import shutil
from pathlib import Path

import pytest


def pytest_addoption(parser):
    """详细审查开关只属于测试，不进入正式模型运行入口。"""
    parser.addoption("--case-details", action="store_true", help="Print case descriptions and passed Python assert expressions")


def pytest_collection_finish(session):
    """保存最终选中的用例顺序及说明；收集清单不代表已执行通过。"""
    session.config._guarded_case_details = [
        (item.nodeid, inspect.getdoc(getattr(item, "obj", None)) or "未提供用例说明")
        for item in session.items
    ]


def pytest_assertion_pass(item, lineno, orig, expl):
    """记录通过的断言表达式，不记录求值后的模型消息、文件内容或认证值。"""
    if item.config.getoption("--case-details"):
        statements = getattr(item, "_guarded_assertions", None)
        if statements is None:
            statements = item._guarded_assertions = []
        statements.append((lineno, " ".join(orig.splitlines())))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """将断言清单关联到实际执行报告；XML 保存说明和数量，日志保存表达式。"""
    outcome = yield
    report = outcome.get_result()
    if item.config.getoption("--case-details") and report.when == "call":
        report.guarded_assertions = list(getattr(item, "_guarded_assertions", []))
        properties = [
            ("description", inspect.getdoc(getattr(item, "obj", None)) or ""),
            ("python_asserts_passed", len(report.guarded_assertions)),
        ]
        item.user_properties = [entry for entry in item.user_properties if entry[0] not in {"description", "python_asserts_passed"}] + properties
        report.user_properties = list(item.user_properties)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """逐项解释本次结果；失败仍由 pytest 提供堆栈，不把收集或缺席当作通过。"""
    if not config.getoption("--case-details") or config.option.collectonly:
        return
    reports = {}
    priority = {"PASSED": 1, "SKIPPED": 2, "FAILED": 3, "ERROR": 4}
    for group in terminalreporter.stats.values():
        for report in group:
            when = getattr(report, "when", None)
            if when not in {"setup", "call", "teardown"} or (when != "call" and report.passed):
                continue
            status = "ERROR" if report.failed and when != "call" else report.outcome.upper()
            previous = reports.get(report.nodeid)
            if previous is None or priority[status] > priority[previous[0]]:
                reports[report.nodeid] = (status, report)
    terminalreporter.write_sep("=", "用例说明与断言清单")
    for nodeid, description in getattr(config, "_guarded_case_details", []):
        found = reports.get(nodeid)
        if found is None:
            terminalreporter.write_line("NOT_RUN " + nodeid)
            continue
        status, report = found
        terminalreporter.write_line(f"{status} {nodeid} ({report.duration:.3f}s)")
        terminalreporter.write_line("  验证：" + " ".join(description.splitlines()))
        for line, statement in getattr(report, "guarded_assertions", []):
            terminalreporter.write_line(f"  ASSERT PASSED {nodeid.split('::')[0]}:{line}: {statement}")
    terminalreporter.write_line(f"pytest exit status: {exitstatus}; 清单仅记录 Python assert，pytest.raises / Mock 检查由用例结果体现。")


@pytest.fixture
def workspace(tmp_path):
    """为每个用例复制独立工作区，保持基准仓库与其他用例不受影响。"""
    source = Path(__file__).resolve().parent / "fixtures" / "repo"
    return Path(shutil.copytree(source, tmp_path / "repo", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")))


@pytest.fixture
def unit_commands():
    """使用当前解释器执行目标仓库测试，禁用持久化 pytest 缓存。"""
    import sys

    return {'unit': [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_calculator.py']}
