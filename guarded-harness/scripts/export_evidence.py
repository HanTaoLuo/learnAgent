#!/usr/bin/env python3
"""导出脱敏、可核对的发布证据；不修改原会话，不加载 Agent，也不重放工具。"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

from guarded_harness.sandbox import Sandbox


ROOT = Path(__file__).resolve().parents[1]
SAFE_RUN_FILES = ("state.json", "checkpoint.json", "audit.jsonl", "report.md", "changes.patch", "test-results.json")
SENSITIVE = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_files():
    """发布版本的输入集合；证据自身另行计算哈希，避免循环引用。"""
    paths = [ROOT / name for name in ("README.md", "VALIDATION.md", "pyproject.toml", ".gitignore")]
    for directory in ("src", "tests", "configs", "scripts", "docs"):
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not any(part in {"__pycache__", ".pytest_cache", ".git", ".DS_Store"}
                                 or part.endswith(".egg-info") for part in p.parts)
                     and p.suffix not in {".pyc", ".pyo"})
    return sorted(paths)


def source_hashes():
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in source_files()}


def hash_text(hashes):
    return "".join(f"{value}  {name}\n" for name, value in sorted(hashes.items()))


def runtime_hashes():
    return {p.name: digest(p.read_bytes()) for p in (ROOT / "src/guarded_harness").glob("*.py")}


class Publisher:
    def __init__(self, session, regression, output):
        self.session, self.regression, self.output = session, regression, output
        self.inputs = {}
        self.replacements = []
        for stage, label in (("02-understand", "understand"), ("03-repair", "repair")):
            run = session / stage / "run"
            state = read_json(run / "checkpoint.json")
            self.replacements.extend([(str(run), f"/workspace/runs/{label}"),
                                      (state["workspace"], f"/workspace/targets/{label}")])
        self.replacements.extend([(str(session), "/workspace/validation"),
                                  (str(regression), "/workspace/regression"),
                                  (str(ROOT), "/workspace/guarded-harness"),
                                  (str(Path.home()), "/home/operator"),
                                  (platform.node(), "validation-host"),
                                  (Path.home().name, "operator")])
        self.replacements.sort(key=lambda item: len(item[0]), reverse=True)
        self.secrets = [value for name, value in os.environ.items()
                        if re.search(r"(?:API_KEY|TOKEN|PASSWORD|SECRET)$", name) and len(value) >= 8]

    def sanitize(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED_SECRET]")
        for old, new in self.replacements:
            if old:
                text = text.replace(old, new)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = re.sub(r"(?:/private)?/var/folders/[^\s\"'<>]+", "/tmp/validation", text)
        text = re.sub(r"/opt/homebrew/anaconda3", "/opt/python", text)
        text = re.sub(r"/Users/[^/\s\"<>]+", "/home/operator", text)
        text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]{16,}", r"\1[REDACTED_SECRET]", text)
        return SENSITIVE.sub("[REDACTED_SECRET]", text)

    def write(self, relative, text):
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.sanitize(text), encoding="utf-8")

    def write_json(self, relative, data):
        self.write(relative, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def copy(self, source, relative):
        self.inputs[str(relative)] = digest(source.read_bytes())
        self.write(relative, source.read_text(encoding="utf-8"))

    def validate_regression(self):
        snapshot = self.regression / "source.sha256"
        require(snapshot.exists(), "Regression requires its own pre-test source.sha256 snapshot")
        captured = {name: sha for sha, name in (line.split(maxsplit=1) for line in snapshot.read_text().splitlines())}
        require(captured == source_hashes(), "Regression source snapshot differs from the current release")
        require((self.regression / "exit-code.txt").read_text().strip() == "0", "Regression exit code must be 0")
        tree = ET.parse(self.regression / "junit.xml")
        suites = list(tree.getroot().iter("testsuite"))
        require(bool(suites), "No JUnit testsuite found")
        require(all(int(s.get(key, "0")) == 0 for s in suites for key in ("failures", "errors", "skipped")),
                "Regression has failures, errors or skipped cases")
        cases = list(tree.getroot().iter("testcase"))
        executed = [case.get("classname").replace(".", "/") + ".py::" + case.get("name") for case in cases]
        result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "--override-ini", "addopts=", "-p", "no:cacheprovider"],
                                cwd=ROOT, text=True, capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)
        require(result.returncode == 0, "Current test collection failed")
        collected = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
        require(bool(collected) and Counter(collected) == Counter(executed), "JUnit case set differs from current collection")
        return {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0,
                "exit_code": 0, "duration_seconds": sum(float(s.get("time", "0")) for s in suites)}

    def validate_runs(self):
        states = {}
        for stage in ("02-understand", "03-repair"):
            run = self.session / stage / "run"
            state = read_json(run / "checkpoint.json")
            require(state == read_json(run / "state.json"), f"{stage}: state/checkpoint mismatch")
            require((state["status"], state["stop_reason"]) == ("SUCCEEDED", "COMPLETED"), f"{stage}: run incomplete")
            require(state["model_config"].get("backend") in {"deepseek", "compatible"}
                    and state["input_tokens"] > 0 and state["output_tokens"] > 0, f"{stage}: real-backend usage evidence missing")
            require(Sandbox(Path(state["workspace"])).fingerprint() == state["workspace_fingerprint"],
                    f"{stage}: current target repository differs from recorded evidence")
            require(digest((run / "report.md").read_bytes()) == state["report_evidence"]["sha256"], f"{stage}: report changed")
            events = [json.loads(line) for line in (run / "audit.jsonl").read_text().splitlines()]
            require(all(event["run_id"] == state["run_id"] and event["tool_call_id"] == event["toolCallId"]
                        and {"actor", "rule", "decision"} <= event.keys() for event in events), f"{stage}: audit correlation mismatch")
            states[stage] = state
        repair = states["03-repair"]
        run = self.session / "03-repair/run"
        require(read_json(run / "test-results.json") == repair["tests"], "Exported tests differ from saved results")
        require((run / "changes.patch").read_text() == "".join(write["diff"] for write in repair["writes"]), "Diff differs from write records")
        require(repair["writes"] and all(a["status"] == "SUCCEEDED" for a in repair["actions"].values()), "Repair contains unfinished actions")
        require(any(test["status"] == "FAILED" and test["returncode"] != 0 and test["write_count"] == 0
                    and test["fingerprint_before"] == test["fingerprint_after"] == repair["initial_workspace_fingerprint"]
                    for test in repair["tests"]), "Repair lacks an initial failing baseline")
        for name in repair["required_tests"]:
            latest = [test for test in repair["tests"] if test["name"] == name][-1]
            require(latest["valid"] and latest["status"] == "PASSED" and latest["returncode"] == 0
                    and latest["fingerprint_before"] == latest["fingerprint_after"] == repair["workspace_fingerprint"]
                    and latest["write_count"] == len(repair["writes"])
                    and latest["argv"] == repair["test_commands"][name], "Required test is stale or failed")
        # 新会话使用任务启动前的源码快照；旧会话可以用已记录的预算审计佐证。
        session_snapshot = self.session / "source.sha256"
        if session_snapshot.exists():
            captured = {name: sha for sha, name in (line.split(maxsplit=1) for line in session_snapshot.read_text().splitlines())}
            if all(captured.get("src/guarded_harness/" + name) == sha for name, sha in runtime_hashes().items()):
                return states, {"method": "session pre-run source snapshot matches all runtime file hashes",
                                "runtime_sha256": runtime_hashes(),
                                "limitation": "Local file hashes are reproducibility evidence, not signed attestations."}
        changes = states["02-understand"]["budget_changes"]
        witness = next((change for change in reversed(changes) if change.get("implementation_sha256") == runtime_hashes()), None)
        require(witness is not None, "No matching runtime-source hash witness in session audit history")
        understand_events = [json.loads(line) for line in (self.session / "02-understand/run/audit.jsonl").read_text().splitlines()]
        require(any(event.get("change_id") == witness["change_id"] and event.get("implementation_sha256") == runtime_hashes()
                    for event in understand_events), "Runtime hash witness missing from append-only audit")
        return states, {"method": "same-session budget audit records matching all runtime file hashes",
                        "runtime_sha256": runtime_hashes(), "witness_change_id": witness["change_id"],
                        "limitation": "Repair has no independent source snapshot captured at its start; the matching audit witness corroborates the session runtime, not a signed per-run attestation."}

    def recovery(self, after):
        before = read_json(self.session / "03-repair/checkpoint-before-resume.json")
        events = [json.loads(line) for line in (self.session / "03-repair/run/audit.jsonl").read_text().splitlines()]
        source_ids = [key for key, action in after["actions"].items() if action["risk"] == "write" and action["tool"] != "write_report"]
        counts = {key: sum(event["event"] == "executed" and event["tool_call_id"] == key for event in events) for key in source_ids}
        checks = {"same_run_id": before["run_id"] == after["run_id"],
                  "paused_on_report": before["status"] == "WAITING_APPROVAL" and before["pending_action"]["tool"] == "write_report",
                  "writes_preserved": bool(before["writes"]) and before["writes"] == after["writes"],
                  "counters_continue": after["steps_used"] > before["steps_used"] and after["tool_calls_used"] > before["tool_calls_used"],
                  "limits_preserved": before["max_steps"] == after["max_steps"] and before["max_tool_calls"] == after["max_tool_calls"],
                  "source_executed_once": bool(counts) and all(count == 1 for count in counts.values()),
                  "checkpoint_loaded": any(event["event"] == "checkpoint_loaded" for event in events),
                  "completion_confirmed": after["status"] == "SUCCEEDED" and after["stop_reason"] == "COMPLETED"}
        require(all(checks.values()), "Recovery proof failed: " + ", ".join(key for key, value in checks.items() if not value))
        self.copy(self.session / "03-repair/checkpoint-before-resume.json", "real-repair/checkpoint-before-resume.json")
        self.copy(self.session / "03-repair/run/checkpoint.json", "real-repair/checkpoint-after-resume.json")
        proof = {"run_id": after["run_id"], "checks": checks, "source_execution_counts": counts,
                 "before": {key: before[key] for key in ("status", "stop_reason", "steps_used", "tool_calls_used", "revision")},
                 "after": {key: after[key] for key in ("status", "stop_reason", "steps_used", "tool_calls_used", "revision")}}
        self.write_json("real-repair/recovery-proof.json", proof)
        self.write("real-repair/recovery-notes.md", f"""# Checkpoint 恢复证明

本说明由原始快照和审计计算生成。原始会话未修改；本目录的脱敏快照仅供审阅。

| 项目 | 恢复前 | 恢复后 |
| --- | --- | --- |
| Run ID | `{before['run_id']}` | `{after['run_id']}` |
| 状态 | `{before['status']} / {before['stop_reason']}` | `{after['status']} / {after['stop_reason']}` |
| 步数 | {before['steps_used']} | {after['steps_used']} |
| 工具次数 | {before['tool_calls_used']} | {after['tool_calls_used']} |
| 源码写入数 | {len(before['writes'])} | {len(after['writes'])} |

暂停点是报告审批；源码修复和当前版本测试已完成。新进程加载同一运行后批准报告并完成最终验收。每个源码修改调用的 `executed` 事件均为一次，原写入记录不变。`execution_recovered` 仅恢复已保存结果，不表示再次执行。逐项布尔检查及调用 ID 见 `recovery-proof.json`。

此记录验证正常暂停后的跨进程恢复。崩溃窗口 `UNKNOWN_RESULT`、工作区变化和重复调用的负例由离线测试覆盖，不将本次正常恢复包装成故障注入测试。
""")

    def reports(self, state):
        original = (self.session / "03-repair/run/report.md").read_text()
        old = "7c9eb6d4...112780c"
        if old not in original:
            self.write("real-repair/REPORT_ERRATA.md", "# 报告发布审阅\n\n未匹配到导出器支持的已知哈希笔误规则，原审批报告未改写。导出器不执行通用语义审查，结论仍需人工对照原始证据审阅。\n")
            return
        require(original.count(old) == 1 and state["writes"][0]["before_sha256"].startswith("7c9eb6d4"),
                "Report typo correction is ambiguous; review before publishing")
        corrected = original.replace(old, state["writes"][0]["before_sha256"])
        self.write("real-repair/run/report-corrected.md", "> 发布勘误副本：仅修正原文件 SHA-256 引用。它不是原审批产物；原审批报告为同目录 report.md，详见 ../REPORT_ERRATA.md。\n\n" + corrected)
        self.write("real-repair/REPORT_ERRATA.md", f"""# 报告勘误

原审批报告 `run/report.md` 第 7 行将原文件哈希缩写写为 `{old}`，正确完整值为：

```text
{state['writes'][0]['before_sha256']}
```

完整值来自获批 `edit_file` 参数、修改前读取结果和写入记录，三者一致。`run/report-corrected.md` 只修正这处文字引用，未重新执行任务或声称重新审批。原报告及其审批哈希保留：`{state['report_evidence']['sha256']}`。执行记录、Diff 和测试结论无需改写。

程序完成检查验证报告存在、已审批、哈希及版本关联，不逐字验证报告中每个自然语言结论或哈希缩写；本处由发布审阅发现。
""")

    def export(self):
        results = self.validate_regression()
        states, binding = self.validate_runs()
        require(not self.output.exists(), "Output directory already exists; use a new release directory")
        self.output.mkdir(parents=True)
        try:
            for name in ("pytest.txt", "junit.xml", "exit-code.txt"):
                self.copy(self.regression / name, "regression/" + name)
            for stage, label in (("02-understand", "real-understand"), ("03-repair", "real-repair")):
                run = self.session / stage / "run"
                for name in SAFE_RUN_FILES:
                    if (run / name).exists():
                        self.copy(run / name, f"{label}/run/{name}")
                for name in ("console.txt", "resume-console.txt"):
                    if (self.session / stage / name).exists():
                        self.copy(self.session / stage / name, f"{label}/{name}")
            self.copy(self.session / "config.json", "config.json")
            if (self.session / "environment.txt").exists():
                self.copy(self.session / "environment.txt", "historical-environment.txt")
            self.recovery(states["03-repair"])
            self.reports(states["03-repair"])
            self.write("source.sha256", hash_text(source_hashes()))
            self.write_json("environment.json", {"python": platform.python_version(), "platform": sys.platform,
                                                "packages": {name: version(name) for name in ("guarded-harness", "smolagents", "pytest", "requests", "python-dotenv", "urllib3", "certifi", "charset-normalizer", "idna")}})
            self.write("README.md", """# 发布验证证据

本目录将同一会话的真实模型记录与当前版本的离线回归汇总为脱敏发布副本。原始会话未修改，原始旧回归不覆盖。当前通过数量、环境及源码绑定见 `release-manifest.json`、`environment.json` 和 `source.sha256`。

`environment.json` 是发布回归环境；`historical-environment.txt` 是真实任务会话保存的环境记录，未完整记录 HTTP 依赖。发布整理升级了 requests 后重跑离线回归，没有重新请求真实模型服务，不将两个环境混称为同一份实测。

| 内容 | 位置 |
| --- | --- |
| 完整离线回归、JUnit、退出码 | `regression/` |
| 真实代码理解状态、审计、报告 | `real-understand/` |
| 真实修复状态、Diff、失败及通过测试、报告、审计 | `real-repair/run/` |
| 恢复前后快照、机器检查及说明 | `real-repair/checkpoint-*.json`、`recovery-proof.json`、`recovery-notes.md` |
| 报告原件、更正版及勘误 | `real-repair/run/report*.md`、`REPORT_ERRATA.md` |

路径、操作者、主机名、临时目录和可识别凭据已替换。运行 ID、原始调用 ID、计数、状态及执行证据哈希保留。发布文件哈希在 manifest 中单独记录；不要拿脱敏副本冒充原始字节，也不要对本目录执行 `resume`。

真实修复仅包含一个函数和一个测试，不证明复杂仓库的普遍修复能力。代码理解的初始 `console.txt` 是重复创建同一目录导致的失败日志；成功状态以 `run/state.json`、审计及 `resume-console.txt` 为准，保留错误日志供追溯。报告勘误说明了结构验收与语义审阅的边界。
""")
            public_hashes = {str(path.relative_to(self.output)): digest(path.read_bytes()) for path in sorted(self.output.rglob("*")) if path.is_file()}
            manifest = {"schema": 1, "version": version("guarded-harness"), "generated_at": datetime.now(timezone.utc).isoformat(),
                        "regression": results, "runtime_binding": binding,
                        "environment_scope": {"environment.json": "current offline regression after dependency alignment",
                                              "historical-environment.txt": "saved real-run session environment; HTTP dependencies were not fully captured",
                                              "real_model_rerun_during_export": False},
                        "runs": {label: {key: state[key] for key in ("run_id", "status", "stop_reason", "steps_used", "tool_calls_used")}
                                 for label, state in states.items()},
                        "redaction": {"paths": "portable /workspace placeholders", "actors": "operator", "host": "validation-host",
                                      "credentials": "environment values and recognizable token patterns removed", "ids": "preserved for correlation",
                                      "resume_supported": False},
                        "original_input_sha256": self.inputs, "published_sha256": public_hashes}
            self.write_json("release-manifest.json", manifest)
            # 只检查导出范围，不打印命中的内容，避免扫描本身泄露凭据。
            for path in self.output.rglob("*"):
                if path.is_file():
                    text = path.read_text()
                    require(not SENSITIVE.search(text) and str(Path.home()) not in text
                            and not re.search(r"/Users/|MacBook[^\s\"<>]*", text)
                            and all(secret not in text for secret in self.secrets), "Sensitive content remains in " + str(path.relative_to(self.output)))
        except Exception:
            shutil.rmtree(self.output)
            raise
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="Write a pre-test source manifest without exporting a session")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--regression-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.snapshot:
        require(not any((args.session_dir, args.regression_dir, args.output)), "Snapshot and export options are mutually exclusive")
        require(not args.snapshot.exists(), "Snapshot already exists; preserve the old source record")
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(hash_text(source_hashes()))
        print("Source snapshot saved")
        return
    require(all((args.session_dir, args.regression_dir, args.output)), "Export requires --session-dir, --regression-dir and --output")
    session, regression, output = (path.resolve() for path in (args.session_dir, args.regression_dir, args.output))
    require(output != session and not output.is_relative_to(session) and not session.is_relative_to(output), "Output must be separate from the original session")
    require(output != regression and not output.is_relative_to(regression) and not regression.is_relative_to(output), "Output must be separate from raw regression")
    results = Publisher(session, regression, output).export()
    print(f"Exported {results['tests']} passed cases and verified recovery; original session unchanged")


if __name__ == "__main__":
    main()
