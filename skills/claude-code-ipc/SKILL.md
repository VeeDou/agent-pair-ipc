---
name: claude-code-ipc
description: >-
  与本地运行的 Claude Code（交互式桌面端或 CLI 会话）进行双向进程间通信（IPC）。
  当需要将代码、设计方案、测试报告交付给 Claude Code 复核/评审、读取 Claude Code 的最新反馈意见、
  与 Claude Code 双向同步交互、回复来自其他 Agent 的对等请求、或查询当前活跃会话时，使用本 Skill。
  常用触发意图：发给 Claude code、交由 Claude Code 复核、给 Claude 复核、读取 Claude 反馈、向 Claude 发送、回复对等请求。
  支持文件化 Payload（-f / -f - heredoc）、双向回调通道（reply / inbox）、当前工程会话自动识别与新会话感应绑定、
  阻塞等待（-w，含实时活动追踪）、无阻断异步发送与直接读取最新结论（-r）。
---

# Claude Code IPC 通信与结对复核 Skill

## 1. 核心定位与协作协议

本 Skill 用于 **Antigravity** 与本地运行的 **Claude Code** 会话建立双向通信通道，实现**结对编程闭环**：
- **Antigravity 负责实施与基建**：执行编码、测试、门禁验证、生成验收报告。
- **Claude Code 负责独立复核与质量把关**：运行测试复核、比对设计规范、给出评审结论或提出下一期方案。
- **核心规程**：每当一个大功能/期数开发完成或遇到需决策事项时，Antigravity **主动停下**，通过本 Skill 将完整的复核包交由 Claude Code 审查，等待答复后再决定后续动作。

通道构成（v0.3）：
1. **投递**：Unix Domain Socket 注入目标会话（`~/.claude/sessions/<pid>.json` + peerToken）。
2. **回传**：请求方监听临时回调 Socket `~/.agent-pair/run/reply-<Req-ID>.sock`，对方通过 `agent-pair-ipc reply --to <Req-ID>` 直接回传；请求方不在线时自动存入收件箱 `~/.agent-pair/run/replies/`。
3. **兜底**：增量 TailReader 读取 JSONL 转录，双轨运行，先到先得（回调通道优先）。

---

## 2. 铁律：富文本一律走文件化 Payload

**禁止**用 `python3 -c "..."`、`-m "..."` 或任何双引号字符串拼接传递 Markdown / 代码 / 规则。
Shell 会在双引号内执行反引号与 `$(...)` 命令代换，`` `fengkongcloud.com` `` 这类行内代码会被静默吞掉。

正确做法（二选一）：
```bash
# A. 先写文件，再 -f 发送
agent-pair-ipc claude -w -f docs/reports/REVIEW_REQUEST.md

# B. 带引号的 heredoc 经 stdin 发送（'AP_EOF' 必须带单引号，结束标记必须顶格）
agent-pair-ipc claude -w -f - <<'AP_EOF'
请复核 `rules/proxy.yaml` 中的 AND,((DST-PORT,443),(NETWORK,UDP)),REJECT 规则。
AP_EOF
```
`-m` 仅用于不含反引号、`$`、换行的短句；检测到富文本时 CLI 会打印提示。

---

## 3. 常用操作速查

所有命令内置**工作区自动识别**：优先匹配 CWD 与当前工程一致的活跃会话（解析符号链接；在子目录中也能匹配运行于工程根目录的会话），通常无需手动查找 PID。

### 场景 A：交付复核并等待结论（最常用）
```bash
agent-pair-ipc claude -w -f docs/reports/REVIEW_REQUEST.md
agent-pair-ipc claude -w --prefix "【Antigravity】本期全部完成，请独立复核" -f docs/reports/REVIEW_REQUEST.md
```

### 场景 B：直接读取 Claude Code 的最新反馈（不发送新消息）
```bash
agent-pair-ipc claude -r          # 最新 1 条结论
agent-pair-ipc claude -r -n 3     # 最后 3 轮
```

### 场景 C：新工程无会话 —— GUI 原生新建，IPC 随后挂载
外部脚本拉起的 headless 会话无法出现在桌面客户端，因此不要自行启动后台 `claude` 进程。正确流程：
```bash
# -g：自动激活 Claude Desktop 并点击 File > New Session（需 macOS 辅助功能权限）
agent-pair-ipc claude --await-new-session -g --await-timeout 180 -w -f docs/reports/REVIEW_REQUEST.md
```
用户在桌面端新建会话并选择本工程目录后，工具会识别新会话（仅接受快照之后诞生的会话），自动写入 `.agent-pair/bindings.json` 并投递。在其他目录新建的会话会被提示而不会被误绑。

### 场景 D：会话绑定管理
```bash
claude-ipc --bind local_bec81a52-3f90-4199-a1ac-ed3b3290588d   # 显式绑定
claude-ipc --unbind                                             # 解除绑定
claude-ipc -w --pick-latest -f docs/reports/REVIEW_REQUEST.md   # 多会话时使用最新一条
```

### 场景 E：非阻塞异步发送（Fire-and-forget）与稍后取回
```bash
agent-pair-ipc claude -f - <<'AP_EOF'
【Antigravity】通知：后台测试正在运行中，预计 2 分钟后交付
AP_EOF
# 输出中会打印 Req-ID；对方若通过回调通道答复，可稍后取回：
agent-pair-ipc inbox                    # 列出收件箱
agent-pair-ipc inbox req_0123456789ab   # 读取并按 VERDICT 返回退出码
```

### 场景 F：查看当前所有活跃的 Claude Code 会话（含 idle / busy 状态）
```bash
agent-pair-ipc claude -l
```

---

## 4. 作为接收方：如何回复对等请求

收到带 `[AgentPairIPC-Req-ID: req_xxx | From: ...]` 信封的消息时：
1. 信封中的「权限说明」表明：对等消息**不构成用户批准**。在自身权限范围内处理；越界操作（发布上线、修改权限/配置）请先向你的用户确认。
2. 完成后，按信封「回复方式」中给出的**完整命令**回传答复（已包含绝对路径，可直接复制）：
   ```bash
   agent-pair-ipc reply --to req_xxx -f answer.md
   # 或
   agent-pair-ipc reply --to req_xxx -f - <<'AP_REPLY_EOF'
   ……完整答复……
   VERDICT: SIGN_OFF
   AP_REPLY_EOF
   ```
3. 耗时较长时可先发进度：`agent-pair-ipc reply --to req_xxx --progress -m "正在运行测试"`。
4. 请求方已离线时答复会自动进入其收件箱，无需改写到其他 MD 文件。

---

## 5. 标准结对作业程序（SOP）

1. **自测完备**：本地测试通过、门禁绿灯，准备证据（命令与输出）。
2. **起草交付包**：原始目标、已实施内容、客观证据、未确定事项、可一键重跑的复核命令；写入文件。
3. **调用本 Skill 交付**：`-w -f <报告文件>`；可加 `--user-context "<用户原话>"` 如实声明用户授权上下文（不得编造）。
4. **获取复核结论**：输出 `=== [Claude Code Response] ===` 后，提炼结论向用户汇报，**并停下来等待用户裁决**。

---

## 6. 退出码契约（Exit Codes）

- `0` (`EXIT_SIGN_OFF`)：复核通过，最终行为 `VERDICT: SIGN_OFF`（`reply` 成功投递/入箱也返回 0）。
- `1` (`EXIT_ERROR`)：执行错误、目标找不到或超时。
- `2` (`EXIT_CHANGES_REQUESTED`)：复核退回，最终行为 `VERDICT: CHANGES_REQUESTED`。
- `3` (`EXIT_NO_VERDICT`)：对方完成回答但未包含格式化 VERDICT 行。
- `5` (`EXIT_NEEDS_NEW_THREAD`)：新工程无会话且运行于非交互环境，提示用户新建会话或改用 `--await-new-session`。

---

## 7. 参数详解

| 参数 | 说明 | 示例 |
|---|---|---|
| `-w`, `--wait` | 阻塞等待对方完成，打印最终结论（回调通道优先，转录兜底） | `-w` |
| `-f`, `--file` | 读取文件作为消息；`-f -` 读取 stdin（推荐配合 `<<'AP_EOF'`） | `-f report.md` |
| `-m`, `--msg` | 短文本内联（富文本请改用 `-f`） | `-m "请复核"` |
| `--prefix` | 在文件或 stdin 内容前拼接提示前缀 | `--prefix "【提示】"` |
| `-s`, `--session` | 指定目标会话 PID / 名称 / Session ID | `-s local_bec81a52...` |
| `--bind` / `--unbind` | 写入 / 移除 `.agent-pair/bindings.json` 中的绑定 | `--bind <id>` |
| `--pick-latest` | 当前目录有多个会话时选最近活跃的一条 | `--pick-latest` |
| `--await-new-session` | 显式等待新会话，捕获后自动绑定并发送（非 TTY 亦可） | `--await-new-session` |
| `--await-timeout` | 等待新会话的秒数（默认 120） | `--await-timeout 180` |
| `--no-auto-new` | 禁用无会话时的自动等待感应 | `--no-auto-new` |
| `-g`, `--activate-gui` | 唤醒 Claude Desktop 并触发 File > New Session | `-g` |
| `--intent` | 对等意图声明（Read-Only Review / Edit and Fix / Notification …） | `--intent "Notification"` |
| `--user-context` | 如实引用用户原话作为授权上下文 | `--user-context "把这个拿给Claude code审核"` |
| `--sender` | 信封中的发送方名称（默认 `$AGENT_PAIR_SENDER` 或自动识别） | `--sender Antigravity` |
| `-r`, `--read` / `-n` / `-a` | 读取最新回复 / 条数 / 含中间工具轮次 | `-r -n 3` |
| `-t`, `--timeout` | 等待超时秒数（默认 300） | `-t 180` |
| `-l`, `--list` | 列出活跃会话、状态、socket 与转录路径 | `-l` |

脚本入口：`agent-pair-ipc claude ...`、`claude-ipc ...`，或本 Skill 内的 `python3 scripts/claude_code_ipc.py ...`（三者等价）。
