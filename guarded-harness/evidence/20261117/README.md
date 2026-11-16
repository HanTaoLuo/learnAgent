# 发布验证证据

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
