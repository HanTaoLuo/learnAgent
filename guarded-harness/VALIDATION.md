# 命令行验证流程

所有操作使用现有的 `guarded-harness` 和 `pytest` 命令。先运行详细回归，再使用真实模型验证代码理解、测试修复和新进程恢复。每轮输出集中到同一个新会话目录，不复用历史结果。

## 0. 准备环境和结果目录

在同一个终端执行，替换目标仓库路径：

```bash
cd /path/to/guarded-harness
source .venv/bin/activate
set -o pipefail

TARGET_REPO="/替换为你的仓库绝对路径"
VERIFY_DIR="$PWD/../guarded-harness-verification/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$(dirname "$VERIFY_DIR")"
mkdir "$VERIFY_DIR"

python --version > "$VERIFY_DIR/environment.txt"
python -m pip show smolagents pytest requests python-dotenv >> "$VERIFY_DIR/environment.txt"
python scripts/export_evidence.py --snapshot "$VERIFY_DIR/source.sha256"
```

- 后续命令沿用 `TARGET_REPO` 和 `VERIFY_DIR`。换终端时，重新激活环境并设置相同路径。
- `VERIFY_DIR` 必须位于目标仓库之外；目标是更大的父目录时，需要另选结果位置。
- 新一轮使用新的结果目录。已有运行通过 `resume` 继续，不用 `run` 覆盖。
- 真实模型需要已配置 `DEEPSEEK_API_KEY`；使用环境文件时，在 `run` / `resume` 中加 `--env-file /环境文件绝对路径`。不要把密钥写进日志或配置 JSON。

## 各轮验证内容

| 轮次 | 主要命令 | 验证内容 | 预期现象 |
| --- | --- | --- | --- |
| 1：详细回归 | `python -m pytest --case-details` | 门禁、路径、审批、重复执行、进程组、状态、恢复、审计和完成检查 | 用例逐项通过；负例通过表示拦截正确 |
| 2：真实代码理解 | `guarded-harness run --task-kind understand --interactive` | 模型自主发现和读取源码，审批报告，源码保持不变 | 显示工具进度；批准报告后最终完成 |
| 3：真实测试修复 | `guarded-harness run --task-kind repair --interactive` | 失败基线、模型生成的 Diff、审批后修改、重测当前版本 | 批准前源码不变，批准后修改和重测；到报告审批时暂停 |
| 4：新进程恢复 | `guarded-harness resume --interactive` | 同一运行恢复，预算延续，已完成写入不重复 | 批准报告后完成；原源码调用只执行一次 |

第 1 轮的 FakeModel 仅用于确定性回归；第 2–4 轮都使用真实模型，没有固定工具响应脚本。

## 1. 执行详细自动化回归

```bash
mkdir -p "$VERIFY_DIR/01-regression"
cp "$VERIFY_DIR/source.sha256" "$VERIFY_DIR/01-regression/source.sha256"

if python -m pytest --case-details \
  --junitxml="$VERIFY_DIR/01-regression/junit.xml" \
  2>&1 | tee "$VERIFY_DIR/01-regression/pytest.txt"
then
  printf '0\n' > "$VERIFY_DIR/01-regression/exit-code.txt"
else
  VERIFY_EXIT=$?
  printf '%s\n' "$VERIFY_EXIT" > "$VERIFY_DIR/01-regression/exit-code.txt"
fi
```

预期输出包含每条用例名称、`PASSED` / `FAILED`、用例说明、耗时，以及通过的 Python `assert` 表达式及源码行号。失败会显示长堆栈和捕获输出，末尾有实际通过数量。

| 文件 | 内容 |
| --- | --- |
| `01-regression/pytest.txt` | 完整终端日志和用例/断言清单 |
| `01-regression/junit.xml` | 机器可读结果、说明、耗时及 Python assert 数量 |
| `01-regression/exit-code.txt` | 应为 `0` |

`pytest.raises`、`Mock.assert_not_called()` 等检查由所属用例的结果体现，不计入 Python assert 清单。断言数量不等于用例数量。收集用例、跳过或预期失败都不能代替场景实际通过。

需要定位问题时可以单独执行某个模块；提交前仍要运行完整回归：

```bash
python -m pytest tests/test_gate.py --case-details
```

| 测试文件 | 主要验证 |
| --- | --- |
| `test_gate.py` | 参数、前置拒绝、重复动作、审批、防重复写入 |
| `test_sandbox.py` | 路径、软链接、命令、环境、输出、超时和取消 |
| `test_state_checkpoint.py` | 状态完整性、未知结果、新进程恢复、工作区变化 |
| `test_audit.py` | 追加记录、原调用关联、前置和执行期间拒绝 |
| `test_harness_loop.py` | 循环、模型协议、配置、交互、预算、上下文和取消 |
| `test_completion.py` | 交付物、当前测试、旧证据失效和最终验收 |

## 2. 调用真实模型进行代码理解

替换 `--task` 为你实际想了解的问题：

```bash
mkdir -p "$VERIFY_DIR/02-understand"

guarded-harness run \
  --workspace "$TARGET_REPO" \
  --run-dir "$VERIFY_DIR/02-understand/run" \
  --backend deepseek \
  --task-kind understand \
  --task "分析项目入口、核心模块和调用关系，并引用实际源码" \
  --interactive \
  2>&1 | tee "$VERIFY_DIR/02-understand/console.txt"

guarded-harness status \
  --run-dir "$VERIFY_DIR/02-understand/run" --json \
  > "$VERIFY_DIR/02-understand/final-state.json"
```

操作和预期：

1. 终端显示 `Step ... tools=[...]`，模型自行选择搜索和读取工具。
2. 报告写入前显示报告内容。内容正确输入 `y`；需要修改用 `r` 反馈；拒绝用 `n`。
3. 输入 `q` 或遇到 EOF 会保留待审批状态。再次调用 `resume --interactive` 可以继续。
4. 完成后检查 `Status: SUCCEEDED`、`Reason: COMPLETED`。查看 `final-state.json`，源码 `writes` 应为空，`evidence` 应有实际引用。

本轮输出：`console.txt`、`final-state.json`，以及 `run/` 内的 `report.md`、`state.json`、`checkpoint.json`、`audit.jsonl`。纯理解任务通常没有 `changes.patch` 或 `test-results.json`。报告的语义正确性需要你对照源码审阅。

## 3. 调用真实模型进行测试修复

### 配置实际项目

复制配置到工作区之外并编辑副本：

```bash
cp configs/local.json "$VERIFY_DIR/config.json"
```

在副本的 `repair-python` 中设置可写范围和实际测试命令，其余配置保留。例如：

```json
"write_directories": ["src"],
"test_commands": {
  "unit": ["@python", "-m", "pytest", "-vv", "-p", "no:cacheprovider"]
},
"required_tests": ["unit"]
```

- `src` 必须替换为项目中实际存在且允许修改的子目录。
- 顶层文件使用 `write_paths`，并清空不适用的 `write_directories`。
- `@python` 是 Harness 的解释器。目标项目使用独立环境时，替换为那个解释器的绝对路径。
- `unit` 必须能复现要修复的问题。依赖缺失、解释器错误和命令配置错误应先解决，不能当作业务修复证据。
- 没有现成失败的项目可以先完成代码理解；修复轮次应在有真实可复现问题的分支或独立工作副本中进行，不在验证过程中临时修改运行中的工作区。

编辑完成后记录配置哈希：

```bash
shasum -a 256 "$VERIFY_DIR/config.json" > "$VERIFY_DIR/config.sha256"
```

### 启动修复并逐次审批

```bash
mkdir -p "$VERIFY_DIR/03-repair"

guarded-harness run \
  --workspace "$TARGET_REPO" \
  --run-dir "$VERIFY_DIR/03-repair/run" \
  --config "$VERIFY_DIR/config.json" \
  --profile repair-python \
  --task-kind repair \
  --task "修复当前失败的测试，保持业务约束，并说明修改原因和验证结果" \
  --interactive \
  2>&1 | tee "$VERIFY_DIR/03-repair/console.txt"
```

按下面的顺序观察实际结果：

1. 模型调用 `run_test` 建立初始失败基线。
2. 源码修改请求显示 `write_file` 或 `edit_file` 及具体 Diff，此时尚未写入。审阅正确才输入 `y`；`edit_file` 还绑定原文件哈希。
3. 批准后程序执行修改，模型读取修改后源码并重跑 `unit`。合理的后续修改仍须逐次审批。
4. **到 `write_report` 的报告审批提示时，先输入 `q` 暂停。** 这是为下一轮验证新进程恢复保留一个安全步骤。若模型提前提交不满足要求的报告，用 `r` 退回，不为完成流程而批准错误内容。

暂停后保存权威检查点和状态：

```bash
cp "$VERIFY_DIR/03-repair/run/checkpoint.json" "$VERIFY_DIR/03-repair/checkpoint-before-resume.json"

guarded-harness status \
  --run-dir "$VERIFY_DIR/03-repair/run" --json \
  > "$VERIFY_DIR/03-repair/before-state.json"
```

预期此时为 `WAITING_APPROVAL / APPROVAL_REQUIRED`，待审批工具是 `write_report`；`writes` 有获批源码变更，`tests` 有初始失败及当前版本通过结果。**此时还未完成最终验收。** `changes.patch`、`test-results.json` 可能还没导出，中间证据保存在状态文件中。

## 4. 使用命令恢复同一次运行

查看当前待审批报告，然后由新进程继续：

```bash
guarded-harness preview --run-dir "$VERIFY_DIR/03-repair/run"

guarded-harness resume \
  --run-dir "$VERIFY_DIR/03-repair/run" \
  --interactive \
  2>&1 | tee "$VERIFY_DIR/03-repair/resume-console.txt"

cp "$VERIFY_DIR/03-repair/run/checkpoint.json" "$VERIFY_DIR/03-repair/checkpoint-after-resume.json"

guarded-harness status \
  --run-dir "$VERIFY_DIR/03-repair/run" --json \
  > "$VERIFY_DIR/03-repair/final-state.json"
```

报告内容正确时输入 `y`。预期最终为 `SUCCEEDED / COMPLETED`。每次 CLI 调用都是独立进程，不需要随机强杀进程来制造正常中断。

对比 `checkpoint-before-resume.json` 和 `checkpoint-after-resume.json`：

| 检查项 | 预期 |
| --- | --- |
| `run_id` | 前后一致 |
| `steps_used`、`tool_calls_used` | 延续原计数，不清零，且不超过预算 |
| 已完成源码写入 | 原调用不重复执行；通常报告恢复前后的源码写入次数相同 |
| 原源码调用的审计 | 每个原 ID 只有一次 `event=executed` |
| `tests` | 有初始失败，最后一次必测在当前版本通过；指纹和写入次数对应当前源码 |
| 报告、Diff、测试导出 | 与状态记录及实际文件相符 |

`checkpoint_saved`、`checkpoint_loaded`、`execution_recovered` 可以多次出现，不代表重复写入。如果模型提出新的修改，应审阅新的调用，不能仅凭总写入次数增加就认定重放。

将实际前后数值、检查的原调用 ID 和对比结论记录到 `03-repair/recovery-notes.md`，与两份检查点共同作为恢复记录。不要把预期值直接抄成已通过结论。

完成后的 `03-repair/run/` 应包含：

```text
state.json
checkpoint.json
audit.jsonl
report.md
changes.patch
test-results.json
```

## 达到步数、工具或模型输出上限后继续原任务

新建运行可以用 `run --max-steps 60 --max-tool-calls 240`，或修改所选 profile 的字段。已经保存的运行不会随配置文件修改而改变预算。

原运行因 `MAX_STEPS` 停止时，先由宿主显式提高总上限，再恢复同一目录：

```bash
guarded-harness extend-budget \
  --run-dir "$VERIFY_DIR/02-understand/run" \
  --max-steps 60 \
  --actor operator \
  --reason "代码理解尚未完成，追加步数预算"

guarded-harness status --run-dir "$VERIFY_DIR/02-understand/run"

guarded-harness resume \
  --run-dir "$VERIFY_DIR/02-understand/run" \
  --interactive \
  2>&1 | tee "$VERIFY_DIR/02-understand/resume-console.txt"
```

如果原来是 `Steps: 24/24`、`Tool calls: 19/96`，追加后应显示：

```text
Status: STOPPED
Reason: BUDGET_EXTENDED
Steps: 24/60
Tool calls: 19/96
```

这表示预算已保存、尚未执行；下一条 `resume` 才调用模型。60 是新的累计步数上限，剩余 36 步，不是额外增加 60 步。`run_id`、已用计数、证据、调用去重、审批和已完成写入都保留。

工具预算耗尽时，同样用 `extend-budget --max-tool-calls 240`；两项都耗尽则在同一命令中提高两项。预算只能增加，且必须让已耗尽项留有额度。该命令不需要模型凭据，不执行工具，也不改变路径权限。

`MODEL_OUTPUT_LIMIT` 是单次模型生成被截断：例如配置 `max_tokens: 1024` 时，报告正文或工具参数超出该上限。它与累计步数、工具次数及输入上下文限制不同。先提高原运行的单次输出上限，再恢复：

```bash
guarded-harness extend-budget \
  --run-dir "$VERIFY_DIR/02-understand/run" \
  --model-max-tokens 4096 \
  --actor operator \
  --reason "单次模型输出被截断，提高输出上限"

guarded-harness resume \
  --run-dir "$VERIFY_DIR/02-understand/run" \
  --interactive \
  2>&1 | tee "$VERIFY_DIR/02-understand/resume-output-console.txt"
```

预期：追加命令保存 `model_config.max_tokens: 4096`，状态变为 `STOPPED / BUDGET_EXTENDED`，模型身份、接口地址、认证变量和超时不变。`resume` 才重新生成被截断的响应；截断内容不作为完整工具调用执行，已完成动作不重放。4096 是以后每次请求的输出上限，不是累计剩余额度，也不是从上次中断的 token 继续。源码与报告仍需审批。若步数或工具次数同时耗尽，应在追加命令中一并提高对应上限。

本实现允许的单次输出上限为 8192 tokens，服务端还可能有自己的限制。仍然截断时应缩小单次报告或修改范围。新运行可用 `run --model-max-tokens 4096`，或在 profile 中设置 `model_max_tokens`。只修改配置文件不会改变旧运行，直接向 `resume` 传入不同模型配置也会被拒绝；须先执行 `extend-budget`。

每次追加会保存 `budget_changes`，并追加请求及保存审计，包含操作者、原因、前后上限、原计数和当前实现哈希。模型没有预算追加工具。审批拒绝、已完成/失败任务、工作区变化、未知执行结果及 `CONTEXT_LIMIT` 不能靠此命令解锁。不要直接修改 `checkpoint.json` 或清零计数。

本功能升级了框架代码。可以继续已有业务任务，但用于最终交付时应保留旧源码哈希，注明版本变化，并在最终框架版本上重新执行完整回归；不要覆盖旧哈希来假装整轮未发生升级。

## 其他直接调用命令

手动审批时，先从 `status` 或预览中复制真实调用 ID：

```bash
guarded-harness approve --run-dir /实际运行目录 --call-id "实际调用ID" --actor operator
guarded-harness deny --run-dir /实际运行目录 --call-id "实际调用ID" --actor operator
guarded-harness resume --run-dir /实际运行目录 --interactive
guarded-harness status --run-dir /实际运行目录 --json
```

`approve` 仅保存批准，`resume` 才执行。交互模式中的 `y` 会完成这两个操作。

运行中的任务可从另一个终端请求取消；先激活环境并填写实际目录：

```bash
guarded-harness cancel --run-dir /实际运行目录 --actor operator
```

`WAITING_REVIEW` 时先查看记录并核对实际文件：

```bash
guarded-harness review --run-dir /实际运行目录 --json
guarded-harness review --run-dir /实际运行目录 \
  --close --actor operator --note "已核对当前文件，保留原未知结果，后续另建运行"
```

| 状态 | 含义与处理 |
| --- | --- |
| `WAITING_APPROVAL` | 正常暂停，等待你审阅和批准；尚未完成 |
| `STOPPED / MAX_STEPS`、`MAX_TOOL_CALLS` | 已停止且未完成；可显式 `extend-budget` 追加总额度，再 `resume`，不清零历史 |
| `STOPPED / MODEL_OUTPUT_LIMIT` | 单次输出被截断；通过 `extend-budget --model-max-tokens` 提高输出上限后 `resume`，不执行残缺调用 |
| `STOPPED / BUDGET_EXTENDED` | 新额度已保存，执行 `resume` 继续原任务 |
| `STOPPED / CONTEXT_LIMIT` | 排查读取范围或上下文预算，必要时调整配置后新建运行；步数追加不能解决上下文限制 |
| `WAITING_REVIEW / WORKSPACE_CHANGED` | 工作区变化，旧证据或审批失效；先核对，不直接续跑 |
| `WAITING_REVIEW / UNKNOWN_RESULT` | 副作用可能发生但结果未确认；不重放原调用，核对关闭后另建运行 |
| `FAILED / ERROR` | 查看终端日志和检查点，处理配置、网络或执行错误 |

**进程退出码 0 不等于业务成功。** 只有 `SUCCEEDED / COMPLETED` 加上符合任务的产物和证据才是业务完成。`review --close` 只关闭旧任务，不会将其标为成功。

## 5. 核对本轮结果并打包

本节归档保留原始运行，可能包含本机路径和任务上下文，仅用于本地留存。公开 Git 提交使用第 6 节的脱敏副本。

先确认完整回归、两类真实任务及恢复检查均通过，再核对代码和配置未变：

```bash
shasum -a 256 -c "$VERIFY_DIR/source.sha256"
shasum -a 256 -c "$VERIFY_DIR/config.sha256"
```

预期全部为 `OK`。源码或配置在本轮中途变化时，应重新验证，不拼接不同版本的结果。

| 提交要求 | 本轮文件 |
| --- | --- |
| 源码与测试 | 当前 `src/`、`tests/`、`configs/`、`pyproject.toml` |
| 详细 pytest 实际结果 | `01-regression/pytest.txt`、`junit.xml`、`exit-code.txt` |
| 完整运行记录 | `03-repair/run/state.json` 及终端日志 |
| 审计 | `03-repair/run/audit.jsonl` |
| Checkpoint 恢复记录 | 两份检查点及 `03-repair/recovery-notes.md` |
| 产物与验证证据 | `03-repair/run/report.md`、`changes.patch`、`test-results.json` |

在项目根目录打包源码，再追加本会话结果：

```bash
zip -r "$VERIFY_DIR/submission.zip" \
  src tests configs scripts docs README.md VALIDATION.md pyproject.toml .gitignore \
  -x '*/__pycache__/*' '*.pyc' '*.env*'

(
  cd "$VERIFY_DIR"
  zip -r submission.zip \
    01-regression 02-understand 03-repair \
    source.sha256 config.json config.sha256 environment.txt \
    -x '*/__pycache__/*' '*.pyc' '*/.lock' '*cancel.request' '*.env*'
)

unzip -t "$VERIFY_DIR/submission.zip"
shasum -a 256 "$VERIFY_DIR/submission.zip" > "$VERIFY_DIR/submission.sha256"
```

打包命令只负责归档，不会替你判断测试和业务是否通过。提交前应审阅报告、实际 Diff、失败与通过结果，以及恢复对比结论。不包含虚拟环境、环境文件、缓存、目标仓库工作副本或旧会话。

## 6. 导出用于 Git 的脱敏证据

上面的会话目录保存原始运行，不应直接上传。已审批报告、状态与审计之间存在哈希关系，导出时保留原始执行哈希，并另计算发布副本的文件哈希。脱敏副本用于审阅，不用于 `resume`。

完成最终版本的回归后，指定实际会话和回归目录，输出到一个尚不存在的发布目录：

```bash
python scripts/export_evidence.py \
  --session-dir "$VERIFY_DIR" \
  --regression-dir "$VERIFY_DIR/01-regression" \
  --output evidence/my-validation

shasum -a 256 -c evidence/my-validation/source.sha256
```

导出器会拒绝失败或用例集合不匹配的回归结果，核对状态、报告、Diff、测试与恢复前后的记录，替换本机路径、操作者、主机名和可识别凭据，生成恢复证明、报告勘误及发布清单。它不调用模型，不重放工具，也不修改原会话。

`release-manifest.json` 区分原始输入文件哈希、执行时源码证据及发布副本哈希。若会话运行时源码与当前源码不匹配，应先重跑真实任务；不能修改旧哈希来掩盖版本变化。导出器只验证可观察结构与证据一致性，源码和报告内容仍需人工审阅。

仅提交项目源码、测试、文档、图像和已审阅的 `evidence/`；不要提交原始会话、虚拟环境、`.env`、密钥文件或历史备份。
