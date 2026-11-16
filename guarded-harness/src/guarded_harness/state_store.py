"""持久化运行状态，与 smolagents 的进程内会话历史独立存储。"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """在同目录同步临时文件后原子发布 UTF-8 内容，并同步目录项。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@dataclass
class RunState:
    """一次运行的业务状态及执行证据。

    status 表示运行阶段，stop_reason 表示终止原因，两者独立于模型答复。
    approvals、pending_action 和 actions 共同保存审批与执行日志。
    revision 用于拒绝陈旧控制器覆盖其他控制器已提交的快照。
    """

    # 运行配置：workspace 保存解析后的绝对路径，max_steps 为 Agent 步数上限。
    run_id: str
    task: str
    workspace: str
    max_steps: int
    status: str = "CREATED"
    stop_reason: str | None = None
    # 步骤记录与读取证据；证据保存相对路径和读取内容的 SHA-256。
    plan: list[str] = field(default_factory=list)
    # 初始计划由控制器生成，模型可修订；游标仅表示计划进度，不替代完成证据。
    plan_source: str = "harness"
    plan_cursor: int = 0
    plan_revision: int = 0
    plan_status: str = "ACTIVE"
    steps: list[dict] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    # 审批绑定原始调用、参数签名和审批时的工作区内容指纹。
    approvals: dict = field(default_factory=dict)
    pending_action: dict | None = None
    actions: dict = field(default_factory=dict)
    writable_paths: list[str] = field(default_factory=list)
    writable_directories: list[str] = field(default_factory=list)
    workspace_fingerprint: str | None = None
    revision: int = 0
    # steps_used 不超过 max_steps；tool_calls_used 计入活动运行内、预算以内的拒绝。
    steps_used: int = 0
    tool_calls_used: int = 0
    # 去重状态包含被拒绝的调用 ID，防止同一调用 ID 反复尝试执行。
    seen_call_ids: list[str] = field(default_factory=list)
    last_action: str | None = None
    repeated_actions: int = 0
    # 仅在完成检查和报告写入均成功后设置最终答案与产物路径。
    answer: str | None = None
    artifact: str | None = None
    # 写入前后哈希属于执行证据，不能用它代替修改后的读取或测试结果。
    writes: list[dict] = field(default_factory=list)
    # 缺少 task_kind 的旧快照按 general 读取；新控制器默认使用 understand。
    task_kind: str = "general"
    initial_workspace_fingerprint: str | None = None
    max_tool_calls: int | None = None
    # 宿主显式追加预算的历史；原执行计数、调用去重及已确认结果保持不变。
    budget_changes: list[dict] = field(default_factory=list)
    test_commands: dict[str, list[str]] = field(default_factory=dict)
    required_tests: list[str] = field(default_factory=list)
    test_timeout_seconds: float = 30.0
    test_output_limit: int = 64 * 1024
    tests: list[dict] = field(default_factory=list)
    active_process: dict | None = None
    report_evidence: dict | None = None
    # 仅记录公开模型配置，密钥值始终由宿主环境提供。
    model_config: dict = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    context_chars: int = 120000
    # 每次拒绝单独保存，重复调用不能覆盖原成功动作或消费中的审批。
    rejections: list[dict] = field(default_factory=list)


class StateStore:
    """单写者 JSON 状态存储，通过同目录临时文件和原子替换发布快照。

    文件内容在替换前执行 fsync，替换后同步父目录。状态、审计与外部副作用
    不在同一个事务内；文件替换也不提供并发写入的冲突检测。
    """

    def __init__(self, path: Path):
        """指定状态文件路径；父目录在首次保存时创建。"""
        self.path = Path(path)

    def save(self, state: RunState) -> None:
        """序列化完整状态并替换目标文件，失败时清理残留临时文件。

        临时文件与目标文件位于同一目录，避免跨文件系统替换。
        序列化、同步或替换失败时异常向上传播，已有状态文件不提前截断。
        """
        atomic_write_text(self.path, json.dumps(asdict(state), ensure_ascii=False, indent=2))

    def load(self) -> RunState:
        """加载 RunState；不进行模式迁移、缺失文件回退或损坏数据修复。"""
        return RunState(**json.loads(self.path.read_text(encoding="utf-8")))
