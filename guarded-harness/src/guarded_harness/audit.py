"""以追加 JSONL 记录运行事件，通过 run_id 和 tool_call_id 关联决策链。"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """单写者审计日志；记录接口只追加事件，不提供覆盖或删除操作。

    每条记录独立 flush/fsync。文件未加进程锁，不支持多个进程并发写入，
    也不提供日志防篡改或与业务状态提交之间的事务保证。
    """

    def __init__(self, path: Path, run_id: str):
        """绑定审计文件和运行标识，并创建日志所在目录。"""
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        event: str,
        tool_call_id: str | None = None,
        *,
        rule: str,
        decision: str,
        actor: str = "harness",
        **details,
    ) -> None:
        """追加一条事件，并在返回前同步文件内容。

        Args:
            event: 事件类型，例如 rejected、allowed 或 executed。
            tool_call_id: 模型发出的原始调用标识；运行级事件允许为 None。
            rule: 导致本次决定的规则或错误码。
            decision: 放行、拒绝、执行结果或停止原因。
            actor: 作出决定的组件或参与者。
            **details: 补充字段，必须可 JSON 序列化且不与公共字段重名。

        序列化和文件写入错误直接向调用方传播，避免审计失败被静默忽略。
        """
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "tool_call_id": tool_call_id,
            "toolCallId": tool_call_id,
            "event": event,
            "rule": rule,
            "decision": decision,
            "actor": actor,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            # 先清空 Python 缓冲，再请求操作系统同步文件内容。
            stream.flush()
            os.fsync(stream.fileno())
