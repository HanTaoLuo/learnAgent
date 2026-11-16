"""保存运行快照并识别未确认执行；Agent 上下文重建由 Harness 负责。"""

from pathlib import Path

from .state_store import RunState, StateStore


class Checkpoint:
    """为步骤边界快照提供独立存储接口，底层复用 StateStore。"""

    def __init__(self, path: Path):
        """配置快照文件；具体保存时机由 Harness 控制。"""
        self.store = StateStore(path)

    def save(self, state: RunState) -> None:
        """以完整业务状态覆盖上一份快照，使用原子文件替换。"""
        self.store.save(state)

    def load(self) -> RunState:
        """反序列化业务状态；不恢复 Agent 执行，也不重新执行已记录的调用。

        文件缺失、JSON 损坏或字段不匹配时直接抛出异常。
        """
        return self.store.load()

    def inspect_recovery(self, state: RunState) -> bool:
        """标记缺失结果的写入或测试执行，返回是否需要宿主核对。

        此检查必须先于任何自动执行。只读操作不重放，可由模型使用新调用 ID 重新请求。
        """
        if state.status == "STOPPED" and state.stop_reason == "HOST_REVIEW_CLOSED":
            return False
        unknown = False
        for action in state.actions.values():
            if action["status"] == "IN_FLIGHT":
                action["status"] = "UNKNOWN_RESULT" if action["risk"] in {"write", "test"} else "INTERRUPTED"
            if action["status"] == "UNKNOWN_RESULT":
                unknown = True
        if unknown:
            state.status = "WAITING_REVIEW"
            state.stop_reason = "UNKNOWN_RESULT"
        return unknown
