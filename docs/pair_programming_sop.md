# 多 Agent 自动化结对开发实战指南：Antigravity + Codex / Claude Code 双脑协同

## 一、 为什么需要多 Agent 结对？（核心背景与痛点）

在日常使用单一 AI 编程工具时，往往会遇到两个典型瓶颈：
1. **“既当运动员又当裁判”的自欺偏差（Confirmation Bias）**：
   单个 Agent 在写完代码后自行验证，极易忽视边界条件、破坏既有架构约束，甚至产生“测试跑过了其实只是 mock 被改写了”的幻觉。
2. **人工“搬运工”陷阱（Manual Copy-Paste Fatigue）**：
   开发者在 Web 界面、VS Code 插件、终端 CLI 之间来回复制代码、报错日志、方案设计，频繁打断思路，导致自动化流程中断，无法做到无人值守或低阻力推进。

### 我们的解法：双 Agent 结对 + 进程间通信（IPC）
将工作流拆解为**「实施与基建（Builder）」**与**「独立审查与质检（Auditor）」**两个明确角色，并通过本地无缝 IPC 通道实现程序化自动交付与复核，人类工程师退居为“终审裁判”。

```mermaid
flowchart TD
    subgraph Builder["实施者 (Antigravity / Gemini)"]
        A1[接收任务切片] --> A2[编码实施 / 本地脚手架修改]
        A2 --> A3[执行本地门禁 & 自测跑绿]
        A3 --> A4[起草结构化交付报告 REVIEW_REQUEST.md]
    end

    subgraph IPC["本地进程间通信 (IPC Bridge)"]
        B1["一键 IPC 脚本 (-w -f)"]
        B2["Unix Domain Socket / codex queue"]
        B3["实时监控活动流 (Tool / Reasoning)"]
    end

    subgraph Auditor["独立审查者 (Claude Code / Codex)"]
        C1[接收审查包]
        C2[独立执行测试与代码审查]
        C3[输出严苛评审意见 (P0~P3 / Sign-off)]
    end

    A4 -->|自动推送| B1 --> B2 --> C1
    C1 --> C2 --> C3
    C3 -->|JSONL 监听捕获| B3 -->|返回结论| A1
```

---

## 二、 角色分工与职责边界

| 角色 | 承担 Agent | 核心职责 | 行为约束 |
|---|---|---|---|
| **实施者 (Executor / Builder)** | **Antigravity** (Gemini) | 负责工程实施、代码编写、修复 BUG、搭建测试、生成阶段交付物 | 绝不自作主张上线，每做完一个独立功能阶段**必须主动停下并交付复核** |
| **独立审查者 (Auditor / Reviewer)** | **Claude Code** 或 **Codex** (CLI/Desktop) | 站在对立视角独立复核：比对设计规范、独立重跑测试命令、提出 P0~P3 隐患 | 不直接替实施者改写业务代码，专注于“挑刺、质检验收与架构把关” |
| **人类架构师 (Owner / Judge)** | **开发者本人** | 负责宏观目标把控、阶段性里程碑裁决、重大方案分歧仲裁 | 无需介入中间繁琐的复制粘贴与语法级沟通，仅看关键节点的复核汇报 |

---

## 三、 底层通信原理：如何做到相对自动化？

要让不同的 AI 工具直接在后台对话，核心是**打通本地进程间通信（IPC）与会话状态监听**，避免任何手动复制。

### 1. 与 Claude Code 的通信机制（Unix Domain Socket + JSONL）
* **会话与鉴权发现**：Claude Code 启动后，会在 `~/.claude/sessions/` 下维护当前进程信息（包含 `pid`、`messagingSocketPath`、`cwd`）及临时 peer 鉴权 Token（`*.key`）。
* **消息注入**：通过标准 `AF_UNIX` 套接字建立连接，发送身份鉴权帧后注入 User 消息帧：
  ```json
  {"type": "auth", "token": "<peerToken>"}
  {"type": "user", "message": {"role": "user", "content": "<交付报告内容>"}}
  ```
* **状态监听与等待（`-w`）**：脚本监听目标工程对应的日志转储 `~/.claude/projects/.../<session_id>.jsonl`，实时捕获 Claude Code 调用的工具（如 `bash`、`read_file`、`edit_file`），直到出现非 `tool_use` 的最终助理消息时自动截流返回。

### 2. 与 Codex 的通信机制（CLI Queue + Session Rollout）
* **会话索引匹配**：读取 `~/.codex/session_index.jsonl`，根据当前工作目录（`CWD`）自动匹配关联的 Codex 活跃会话。
* **消息注入**：利用 Codex CLI 原生的管道命令将消息送入队列：
  ```bash
  codex queue --thread <session_id> --message <content>
  ```
* **状态监听与等待（`-w`）**：监听 `~/.codex/sessions/.../rollout-*.jsonl` 事件流，捕获 `Reasoning`、`custom_tool_call` 与最终的 `task_complete` / `final_answer`，实现无侵入的双向闭环。

### 3. 工程化封装（自动工作区匹配与文件直发）
封装为标准化工具后，彻底抹平了繁琐的 PID 与 Session 查询：
* **自动工作区识别**：执行时不传 Session 参数，脚本会自动找当前目录（CWD）正在运行的会话。
* **文件直发模式（`-f`）**：避免命令行下长文本反引号、换行符和 shell 转义报错。

---

## 四、 标准作业程序（SOP）：日常推进四步法

在实际推进复杂功能时，我们遵循一套标准契约：

### 第 1 步：小切片自测（Self-Test）
实施方（Antigravity）每次仅实施一个完整可验证的小阶段（Phase），并在本地运行全量静态检查和门禁测试（如 `pnpm gate` 或 `pytest`），必须确保全部绿灯。

### 第 2 步：起草标准化交付包（Review Package）
在工程目录生成标准的复核请求文档（例如 `REVIEW_REQUEST.md`），明确写入：
1. **本期目标**（What & Why）
2. **变更范围与关键文件**（File Changes）
3. **可复现的验证命令与客观证据**（Verification Commands & Logs）
4. **已知妥协或未决事项**（Open Questions）
5. **明确给对方的指令**：“请独立运行上述测试命令，对照架构规范，检查是否存在回归隐患与边界漏洞。”

### 第 3 步：一键阻塞交付（IPC Roundtrip）
Antigravity 在后台执行一行命令，自动将报告丢给审查者并挂起等待：
```bash
# 交付给 Claude Code 复核并同步等待
agent-pair-ipc claude -w -f docs/reports/REVIEW_REQUEST.md

# 或交付给 Codex 复核并同步等待
agent-pair-ipc codex -w -f docs/reports/REVIEW_REQUEST.md
```
*此时终端会实时打印对方的进度日志（如 `Session is running tool: bash...`），人类在旁边泡杯咖啡即可，无需任何操作。*

### 第 4 步：结论归纳与裁决闭环
脚本退出并捕获返回的审查结论：
* **机器可读契约**：审查方在回复末尾附带固定的裁决行：
  - `VERDICT: SIGN_OFF` ➡️ 门禁全部跑绿，测试与安全审计通过。
  - `VERDICT: CHANGES_REQUESTED` ➡️ 指出 P0/P1 问题，附带具体修复建议。
* **分级收敛处理**：
  - 若指出 **P0/P1 缺陷**：实施方自动针对整改建议修改代码，重新跑测试，再次回传直到对方给出通过。
  - 若获得 **Sign-off（通过）**：实施方整理双边评审摘要，汇报给人类架构师，完成代码提交并进入下一阶段。

---

## 五、 高效自动化的 6 条核心心法

1. **复用“带上下文的活会话”，而非冷启动无状态 Agent**
   市面上的 API 框架每次都要从零传一堆代码上下文，耗费大量 Token 且毫无项目历史记忆。本方案的核心威力在于：**复用开发者本地已经在使用的、拥有完整项目心智的 Claude / Codex 活跃会话**，并且人类在桌面端能直接看到审查过程并随时介入。

2. **绝对禁止“口水话沟通”，坚持“文件契约交付”**
   AI 之间对话最怕无休止的“好的、请稍等、我认为”。所有交付必须落在 `.md` 文件上，包含清晰的目标、改动文件列表、跑过的命令与具体期望。文件是唯一的客观信任载体。

3. **双向固化契约：输入靠请求模板，输出靠 VERDICT 标识**
   不仅要固化发出去的交付包格式（`REVIEW_REQUEST.md`），还要约定好收回来的结论格式（`VERDICT: SIGN_OFF | CHANGES_REQUESTED`）。这样才能让整个流水线通过进程退出码（Exit Code）实现真正的工业级自动化。

4. **必须赋予审查方“执行工具”的权力**
   审查方绝不能只当“脑补型评论员”。交给 Claude Code 或 Codex 时，必须给它权限去真正执行 `bash`、运行测试脚本、读取本地源文件。只有当它亲自把测试跑通，得出的“通过”结论才具有工程价值。

5. **控制交互粒度：单次闭环控制在 1~3 个关键文件**
   自动化最容易翻车的原因是“一次喂太多”。不要把涉及 30 个文件的重大重构一次性丢给对方复核。将大任务切成 Phase 1（核心接口）、Phase 2（业务实现）、Phase 3（边缘测试），单点推进收敛速度最快。

6. **显式超时、Nonce 防并发与人类终审权**
   - 必须为单次审查设置硬超时限制（默认 300 秒）。
   - 每次请求注入唯一的 `Req-ID`（Nonce），杜绝在复杂并发场景下将历史旧回复当成新结果。
   - 保留人类的“一票否决权”：双 Agent 自动跑迭代自纠错，但大里程碑终点必须由人类敲下回车确认。
