# 图像生成记录

两张图均通过内置 GPT Image（imagegen）生成，随后人工核对模块职责与箭头。输入仅包含工程结构和控制规则，没有发送运行记录、源码内容、用户名、路径或密钥。图像是架构说明；实现及证据以源文件、测试与发布清单为准。

- `architecture.png`：系统模块和信任边界。
- `data-flow.png`：工具调用、审批、落盘、恢复及最终验收的数据流。
- 图内使用英文标签，中文解释见项目 README。

## Architecture prompt

```text
Use case: infographic-diagram. Asset: engineering README architecture diagram. Create a precise, polished flat architecture infographic, high-resolution landscape 16:10, solid ivory-white background, dark navy sans-serif typography, teal control components, amber approval boundary, thin orthogonal arrows, generous whitespace, no gradients, no photos, no mascots, no fake logos. English labels only for accurate technical text; title "Guarded Harness — Architecture", subtitle "smolagents 1.26.0 | model decisions, program enforcement".
Exact architecture based on verified source:
At top outside the main runtime group: "Host / CLI", with the note "task · configuration · approval · resume". It controls "harness.py" orchestration. Approval arrow goes only from Host to Gate; the model cannot approve.
At left outside the main runtime group: "LLM API" connected bidirectionally ONLY to "ToolCallingAgent adapter", labeled "messages / tool calls". No LLM-to-files or LLM-to-command arrow.
Inside a large rounded rectangle labeled "Guarded Harness": a horizontal execution row "ToolCallingAgent adapter" → "Gate" → "Sandbox + tool handlers". Under adapter write "inherits smolagents ToolCallingAgent"; under Gate list "schema · permissions · risk", "approval · dedup · budgets"; under Sandbox list "paths · fixed commands", "environment · timeout · output".
At right outside group: "Target repository", connected only with Sandbox execution using controlled files and configured tests.
Within group below this row, "harness.py — orchestration & completion checks". Smaller label "source evidence + approved report + current tests". Its output exits to "SUCCEEDED / COMPLETED" or "PAUSED / STOPPED".
Bottom within group: three storage cards "state_store.py / business state", "checkpoint.py / recovery", "audit.py / append-only events". Gate and orchestration have clear downward persistence arrows into these cards. Small footer "Original tool_call_id preserved · completed writes never replayed · UNKNOWN_RESULT requires review".
Keep the primary execution row visually dominant. All tool calls must visibly pass Gate before Sandbox. Do not invent services, containers, databases, cloud deployment or OS isolation. Do not show Sandbox authorizing approvals. Avoid crossing connectors. Fit every label inside its box. No personal paths, usernames, secrets or real run IDs.
```

## Data-flow prompt

```text
Use case: infographic-diagram. Asset: engineering README runtime data-flow diagram. Match a clean flat technical infographic: landscape 16:10 high-resolution, ivory-white background, navy sans-serif typography, teal arrows and components, amber approval branch, red rejection/review branch, generous whitespace, thin orthogonal routing. English labels only, no photos, logos, mascots, decoration or texture. Title "Guarded Harness — Tool Call Data Flow". Subtitle "Decision → enforcement → durable evidence → next decision".
Main horizontal row, evenly spaced boxes with numbered labels: "1 Task + context" → "2 LLM decision" → "3 ToolCall / original ID" → "4 Gate preflight" → "5 Sandbox execution" → "6 Result + evidence". A teal feedback route below main row returns "structured observation" from Result to LLM decision.
Directly below Gate use two explicit branches: red "DENY" → "structured error / same tool_call_id" → "persist rejection + audit", then feedback to the LLM; amber "WRITE" → "WAITING_APPROVAL" → "Host approves" → "revalidate parameters + workspace" → "IN_FLIGHT checkpoint" → Sandbox execution. Read/test calls pass preflight then persist IN_FLIGHT before execution without approval branch. ALL accepted calls persist execution intent before execution; no bypass route from model to execution.
Directly below execution/results draw "Persist confirmed result" → "state + checkpoint + audit". Beneath it add recovery rail: "New process loads checkpoint" → diamond "confirmed result?" with green "YES: restore observation, no replay" and red "NO: UNKNOWN_RESULT / WAITING_REVIEW". Annotate red branch "uncertain writes/tests only".
At bottom separate final-answer gate: "final_answer" → "Completion checks" → "SUCCEEDED / COMPLETED"; under checks small text "approved report · current source evidence · required tests for repair". Failure arrow says "reject final answer". Footer "Timeout / cancellation terminates the test process group · budget exhaustion stops without claiming success".
Prioritize logical correctness, clearly separated branches and readable short labels. No personal information, absolute user paths, keys or actual project source code. Do not imply that model text alone marks success.
```

## 数据流图连接顺序复核

生成后发现主线箭头可能绕过持久化节点，使用相同工具定向修订。最终图要求所有执行先持久化 IN_FLIGHT，所有成功观察先保存已确认结果，再返回模型。

```text
Edit the attached Guarded Harness data-flow infographic. Preserve all typography, colors, boxes, labels, positions and all other content exactly. Correct precisely two connectors so the picture matches durable execution order: (1) Remove the direct horizontal arrow from '4 Gate preflight' to '5 Sandbox execution'. Keep the existing two downward Gate branches and both IN_FLIGHT-to-execution arrows. There must be no path to Sandbox execution that bypasses IN_FLIGHT persistence. (2) Move the origin of the long teal 'structured observation' return route: disconnect it from '6 Result + evidence' and instead connect its starting end to the RIGHT EDGE of the lower 'state + checkpoint + audit' box, routing around the right outside margin back up to the existing observation feedback route, with a clear arrow entering '2 LLM decision'. The return observation must happen after confirmed result persistence. Keep Result downward to Persist confirmed result to state + checkpoint + audit intact. Do not add other connectors, boxes or text. Keep the whole 1586x992 composition readable.
```

```text
Make one precise connector correction to this existing 1586x992 diagram. Keep every box, text label, color, position and all other connectors unchanged. The top-right box '6 Result + evidence' must connect DOWNWARD to the 'Persist confirmed result' box below it: draw a vertical arrow starting at the bottom center of box 6 (approximately x1428,y200) and ending at the top center of Persist confirmed result (approximately x1428,y316). Where this arrow crosses the horizontal structured-observation return line around y234, use a small bridge/hop or clearly separated crossing, with NO junction dot. It must be visually clear that Result flows to Persist, while observation only returns from the lower 'state + checkpoint + audit' box via the existing far-right outer route. Preserve the intentionally empty horizontal gap between Gate and Sandbox; do NOT add an arrow there. Change only the missing Result-to-Persist connector.
```

