---
name: codex-ipc
description: >-
  与本地运行的 Codex（交互式桌面端或 CLI 会话）进行双向进程间通信（IPC）。
  当需要将代码、设计方案、测试报告交付给 Codex 复核/评审、读取 Codex 的最新反馈意见、
  与 Codex 双向同步交互、或查询当前活跃会话时，使用本 Skill。
  常用触发意图：发给 Codex、交由 Codex 复核、给 Codex 复核、读取 Codex 反馈、向 Codex 发送。
  支持当前工程会话自动识别、文件直发（-f）、阻塞等待（-w，含实时活动追踪）、无阻断异步发送与直接读取最新结论（-r）。
---

# Codex IPC 通信与结对复核 Skill

## 1. 核心定位与协作协议

本 Skill 用于 **Antigravity** 与本地运行的 **Codex** 会话建立无缝双向通信通道（基于 `codex queue` 与 JSONL 会话实时跟踪），实现**结对编程与独立质检验收闭环**：
- **Antigravity 负责实施与基建**：执行编码、测试、门禁验证、生成验收报告。
- **Codex 负责独立复核与质量把关**：运行测试复核、比对设计规范、给出独立评审结论（P0–P3 问题），指出边界漏洞与回归隐患。
- **核心规程**：每当一个大功能/期数开发完成或遇到需决策事项时，Antigravity **主动停下**，通过本 Skill 将完整的复核包交由 Codex 审查，等待 Codex 答复后再决定后续动作。

---

## 2. 常用操作速查（一键命令）

所有命令内置**工作区自动识别机制**：默认优先匹配 CWD（当前工作目录）与当前工程一致的最活跃会话，通常**无需手动查找或输入 Session ID**。

### 场景 A：交付复核并等待结论（最常用模式）
将写好的报告或说明文件直接发给 Codex，并等待其执行完所有思考与工具后返回最终评审结论（带实时活动跟踪）：
```bash
agent-pair-ipc codex -w -f docs/reports/REVIEW_REQUEST.md
# 或使用独立可执行文件：
codex-ipc -w -f docs/reports/REVIEW_REQUEST.md
```

如需在前附带简短说明：
```bash
agent-pair-ipc codex -w --prefix "【Antigravity】第 3 阶段整改已完成，请独立复核" -f docs/reports/REVIEW_REQUEST.md
```

### 场景 B：发送简短消息并等待回复
```bash
agent-pair-ipc codex -w -m "【Antigravity】门禁测试已全部跑绿，请指示是否可以放行"
```

### 场景 C：直接读取 Codex 的最新反馈或结论（不发送新消息）
读取目标会话最后一条已完成的助手回复（自动过滤中间过程中的思考与工具调用）：
```bash
# 读取当前工程关联会话的最新 1 条结论：
agent-pair-ipc codex -r

# 指定 Session ID 读取：
agent-pair-ipc codex -r -s 01234567-89ab-cdef-0123-456789abcdef

# 读取最后 3 轮对话：
agent-pair-ipc codex -r -n 3
```

### 场景 D：非阻塞异步发送（Fire-and-forget）
只发送通知，不等待对方完成：
```bash
agent-pair-ipc codex -m "【Antigravity】通知：后台门禁正在运行中，预计 2 分钟后交付"
```

### 场景 E：查看当前所有活跃的 Codex 会话
```bash
agent-pair-ipc codex -l
```

---

## 3. 标准结对作业程序（SOP）

当用户要求「拿给 Codex 复核」、「发给 Codex」、「做完一部分后复核」时，执行流程如下：

1. **自测完备**：确保本地测试通过、门禁绿灯、生成对应的报告或证据（如验收命令、日志输出等）。
2. **起草交付包**：
   - 清楚写明：原始目标、已实施内容、客观证据（跑过的命令与输出）、未确定事项、对闭环的推进。
   - 准备复核命令：提供可一键重跑的测试命令。
3. **调用本 Skill 交付**：
   - 优先采用 `-f <报告文件>` 配合 `-w` 发送，避免命令行多行转义问题。
   - 脚本会自动显示 `[Codex IPC Activity]` 监控对方正在调用的工具或推理过程。
4. **获取复核结论**：
   - 当 Codex 响应完成后，脚本输出 `=== [Codex Response] ===`。
   - Antigravity 提炼复核结论（通过 / 退回整改 / P0/P1 问题），向用户清晰汇报，**并停下来等待用户裁决**。

---

## 4. 参数详解

| 参数 | 说明 | 示例 |
|---|---|---|
| `-w`, `--wait` | 阻塞等待 Codex 完成所有思考与工具调用，打印最终结论 | `-w` |
| `-f`, `--file` | 读取指定文件内容作为发送消息（避免 shell 引号转义） | `-f report.md` |
| `-m`, `--msg` | 明确指定发送的文本内容 | `-m "请复核"` |
| `--prefix` | 在文件或 stdin 内容前拼接一段提示前缀 | `--prefix "【提示】"` |
| `-s`, `--session` | 指定目标会话 ID 或标题名称（默认自动选当前目录活跃会话） | `-s 01234567-89ab-cdef-0123-456789abcdef` |
| `-r`, `--read` | 只读模式：获取最新助手回复，不发新消息 | `-r` |
| `-n`, `--number` | 配合 `-r` 使用，读取最近 N 条回复（默认 1） | `-r -n 3` |
| `-a`, `--all-turns` | 配合 `-r` 使用，包含中间工具/过程回复（默认只看最终结论） | `-r -a` |
| `-t`, `--timeout` | 等待超时秒数（默认 300 秒） | `-t 180` |
| `-l`, `--list` | 列出所有最近的 Codex 会话元数据与转储状态 | `-l` |
