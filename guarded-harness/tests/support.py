"""测试专用模型与离线进程驱动，不属于正式运行入口。"""

import argparse
import json
from pathlib import Path
from copy import deepcopy
from dataclasses import asdict
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction, MessageRole, Model
from guarded_harness.harness import Harness, configuration_values, resolve_config, CompatibleChatModel
from guarded_harness.checkpoint import Checkpoint

def tool_message(call_id: str, name: str, arguments: dict) -> ChatMessage:
    """构造只包含一个工具调用的 assistant 消息，调用 ID 由调用方指定。"""
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        tool_calls=[
            ChatMessageToolCall(
                id=call_id,
                type="function",
                function=ChatMessageToolCallFunction(name=name, arguments=arguments),
            )
        ],
    )


class FakeModel(Model):
    """按预设顺序返回消息，不根据输入上下文生成新决策。

    同一实例中的 calls 持续递增，覆盖 Agent 的常规模型请求及预算耗尽时的收尾请求。
    """

    def __init__(self, script: list[ChatMessage]):
        """复制消息序列，隔离调用方与 Agent 对消息对象的修改。"""
        super().__init__(model_id="fake-scripted-model")
        self.script = deepcopy(script)
        self.calls = 0

    def generate(self, messages, **kwargs) -> ChatMessage:
        """返回下一条消息的副本；序列耗尽则抛出 RuntimeError。

        messages 和 kwargs 用于兼容框架接口，不参与响应选择。
        """
        if self.calls >= len(self.script):
            raise RuntimeError("FakeModel script exhausted; provide the next model response")
        # 框架可能解析或改写工具参数，返回副本以保留原始脚本。
        response = deepcopy(self.script[self.calls])
        self.calls += 1
        return response


def load_model(path: Path) -> FakeModel:
    """加载工具请求列表，每项包含 id、tool 和 arguments 三个字段。

    这里只校验响应序列结构；具体工具权限和参数由 Gate 校验。
    """
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("Model script must be a JSON array")
    messages = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {"id", "tool", "arguments"}:
            raise ValueError("Each response requires id, tool and arguments")
        if type(record["id"]) is not str or not record["id"].strip() or type(record["tool"]) is not str:
            raise ValueError("Tool names and original call IDs must be strings")
        messages.append(tool_message(record["id"], record["tool"], record["arguments"]))
    return FakeModel(messages)


def add_model_arguments(parser) -> None:
    """注册模型选项；认证参数仅允许指定环境变量名称。"""
    parser.add_argument('--backend', choices=('fake', 'deepseek', 'compatible'))
    parser.add_argument('--script', type=Path)
    parser.add_argument('--model-id')
    parser.add_argument('--base-url')
    parser.add_argument('--api-key-env')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--model-timeout', type=float)
    parser.add_argument('--model-max-tokens', type=int)


def configured_model(args, saved: dict | None = None):
    """创建模型与非敏感元数据，恢复时拒绝模型身份和请求配置变化。"""
    previous = saved or {}
    backend = args.backend or ('fake' if args.script else previous.get('backend'))
    if backend is None:
        raise ValueError('Specify --script for an offline run or --backend for a real model')
    if previous and backend != previous.get('backend'):
        raise ValueError('Cannot change the model backend of an existing run')
    if backend == 'fake':
        if args.script is None:
            raise ValueError('Fake backend requires --script')
        if any(value is not None for value in (args.model_id, args.base_url, args.api_key_env, args.model_timeout, args.model_max_tokens, args.env_file)):
            raise ValueError('Fake backend does not accept real-model configuration')
        return load_model(args.script), {'backend': 'fake'}
    if args.script is not None:
        raise ValueError('Real-model backends cannot be combined with --script')
    values = configuration_values(args.env_file)
    config = resolve_config(
        backend, values, model_id=args.model_id, base_url=args.base_url,
        api_key_env=args.api_key_env, timeout_seconds=args.model_timeout,
        max_tokens=args.model_max_tokens, saved=previous or None,
    )
    return CompatibleChatModel(config, values), config.public_dict()


def offline_main() -> None:
    """解析宿主命令并将完整运行状态以 JSON 写入标准输出。

    等待审批或核对是正常业务状态。非法参数、审批不匹配或运行错误由进程报告失败。
    status 仅读取权威检查点，不修改运行状态。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Create and start a run")
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, required=True)
    add_model_arguments(run)
    run.add_argument("--task", required=True)
    run.add_argument("--max-steps", type=int, default=8)
    run.add_argument("--write-path", action="append", default=[])
    run.add_argument("--task-kind", choices=("understand", "repair"), default="understand")
    run.add_argument("--test-commands", type=Path)
    run.add_argument("--require-test", action="append")
    run.add_argument("--test-timeout", type=float, default=30.0)
    run.add_argument("--test-output-limit", type=int, default=64 * 1024)
    run.add_argument("--max-tool-calls", type=int)
    for name in ("approve", "deny"):
        decision = commands.add_parser(name, help=f"{name.title()} a pending action")
        decision.add_argument("--run-dir", type=Path, required=True)
        decision.add_argument("--call-id", required=True)
        decision.add_argument("--actor", required=True)
    revise = commands.add_parser("revise-report", help="Return a pending report with host feedback")
    revise.add_argument("--run-dir", type=Path, required=True)
    revise.add_argument("--call-id", required=True)
    revise.add_argument("--actor", required=True)
    revise.add_argument("--feedback", required=True)
    resume = commands.add_parser("resume", help="Continue an approved or interrupted run")
    resume.add_argument("--run-dir", type=Path, required=True)
    add_model_arguments(resume)
    status = commands.add_parser("status", help="Read the authoritative checkpoint")
    status.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        test_commands = json.loads(args.test_commands.read_text(encoding="utf-8")) if args.test_commands else {}
        model, metadata = configured_model(args)
        harness = Harness(
            args.workspace, args.run_dir, model, args.max_steps, tuple(args.write_path),
            task_kind=args.task_kind, test_commands=test_commands,
            required_tests=tuple(args.require_test) if args.require_test else None,
            test_timeout_seconds=args.test_timeout, test_output_limit=args.test_output_limit,
            max_tool_calls=args.max_tool_calls, model_config=metadata,
        )
        state = harness.run(args.task)
    elif args.command == "status":
        state = Checkpoint(args.run_dir / "checkpoint.json").load()
    elif args.command == "resume":
        saved = Checkpoint(args.run_dir / "checkpoint.json").load().model_config
        model, _ = configured_model(args, saved)
        state = Harness.load(args.run_dir, model).resume()
    elif args.command == "revise-report":
        harness = Harness.load(args.run_dir, FakeModel([]))
        state = harness.request_report_revision(args.call_id, args.feedback, args.actor)
    else:
        harness = Harness.load(args.run_dir, FakeModel([]))
        state = harness.decide(args.call_id, args.command == "approve", args.actor)
    print(json.dumps(asdict(state), ensure_ascii=False, indent=2))



if __name__ == "__main__":
    offline_main()
