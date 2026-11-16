# Checkpoint 恢复证明

本说明由原始快照和审计计算生成。原始会话未修改；本目录的脱敏快照仅供审阅。

| 项目 | 恢复前 | 恢复后 |
| --- | --- | --- |
| Run ID | `980fdbd70f8942098d93e29a7c610e6c` | `980fdbd70f8942098d93e29a7c610e6c` |
| 状态 | `WAITING_APPROVAL / APPROVAL_REQUIRED` | `SUCCEEDED / COMPLETED` |
| 步数 | 8 | 9 |
| 工具次数 | 8 | 9 |
| 源码写入数 | 1 | 1 |

暂停点是报告审批；源码修复和当前版本测试已完成。新进程加载同一运行后批准报告并完成最终验收。每个源码修改调用的 `executed` 事件均为一次，原写入记录不变。`execution_recovered` 仅恢复已保存结果，不表示再次执行。逐项布尔检查及调用 ID 见 `recovery-proof.json`。

此记录验证正常暂停后的跨进程恢复。崩溃窗口 `UNKNOWN_RESULT`、工作区变化和重复调用的负例由离线测试覆盖，不将本次正常恢复包装成故障注入测试。
