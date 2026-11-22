---
name: antigravity-ipc
description: >-
  与本地运行的 Antigravity Desktop（桌面端会话）进行双向进程间通信（IPC）。
  当 Claude Code、Codex 或其他本地 Agent 需要将代码、设计方案、复核请求交付给 Antigravity Desktop 会话、
  读取 Antigravity 的最新反馈意见、与 Antigravity 双向同步交互、回复对等请求、或列出当前 Antigravity 会话时，使用本 Skill。
  常用触发意图：发给 Antigravity、交由 Antigravity 复核、给 Antigravity 发消息、读取 Antigravity 反馈、查看 Antigravity 会话。
  重要提醒：连接桌面端会话必须使用 `agent-pair-ipc antigravity`（经桌面端 `agentapi` + Language Server 通道），
  严禁使用独立的 `agy` CLI（`agy` 只读 `~/.gemini/antigravity-cli/`，找不到 `~/.gemini/antigravity/` 下的桌面端会话）。
---

# Antigravity Desktop IPC 通信与结对复核 Skill

## 1. 核心定位与通道构成

本 Skill 用于 **Claude Code / Codex** 与本地运行的 **Antigravity Desktop** 会话建立双向通信通道，实现跨 Agent 结对评审与任务协同。

> **铁律：不要用 `agy` 命令行连接桌面端会话！**
> `agy` CLI 使用独立的 `~/.gemini/antigravity-cli/` 数据目录，无法触达 Antigravity Desktop 存于 `~/.gemini/antigravity/` 的会话（强行传桌面端 ID 会报 `trajectory not found`）。给桌面端发消息一律走 `agent-pair-ipc antigravity`（或 `antigravity-ipc`）。

通道构成：
1. **只读发现**：只读读取 `~/.gemini/antigravity/conversation_summaries.db`（WAL 无法直接打开 SHM 时自动做一致性临时快照拷贝，绝不直接写入 SQLite，也绝不删除 `.db-shm` / `.db-wal`）。
2. **投递**：自动发现存活的桌面端 Language Server（`~/.gemini/antigravity/daemon/hub_*.json`），通过桌面端自带的 `agentapi send-message` 将消息安全注入目标原会话（并自动剥离调用方 `ANTIGRAVITY_PROJECT_ID` 等上下文变量避免跨工程鉴权拒收）。
3. **回传与兜底**：优先接收回调 Socket `~/.agent-pair/run/reply-<Req-ID>.sock`（或离线收件箱 `~/.agent-pair/run/replies/`），同时增量追踪 `~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl`（截断时自动回读 `transcript_full.jsonl`）兜底。

---

## 2. 铁律：富文本一律走文件化 Payload

**禁止**用 `-m "..."` 或双引号字符串拼接传递 Markdown / 代码 / 规则，避免反引号与 `$VARS` 被 Shell 吞掉。

正确做法（二选一）：
```bash
# A. 先写文件，再 -f 发送
agent-pair-ipc antigravity -w -f docs/reports/REVIEW_REQUEST.md

# B. 带单引号的 heredoc 经 stdin 发送（'AP_EOF' 必须带单引号，结束标记必须顶格）
agent-pair-ipc antigravity -w -f - <<'AP_EOF'
请复核 `cache/prefix_plan.md`，并在最后一行给出 `VERDICT: SIGN_OFF` 或 `VERDICT: CHANGES_REQUESTED`。
AP_EOF
```
超过 128 KiB 的正文会自动落盘至 `~/.agent-pair/run/ap_payload_*.md`（`0o600`），仅携带信封与文件指针投递。

---

## 3. 常用操作速查

所有命令内置**工作区自动识别**：优先匹配工作区目录与当前工程一致的会话（`exact > linked > ancestor`）；同级多会话时默认拒绝猜测并列出候选，避免串台。

### 场景 A：查看当前 Antigravity Desktop 会话
```bash
agent-pair-ipc antigravity -l
# 标签说明：
# [Current Project] -> 同一物理工作区
# [Linked Project]  -> 通过 .agent-pair/config.json 别名或顶层软链接关联
# [Parent of CWD]   -> 上层父目录工作区
# [Bound Target]    -> 当前 .agent-pair/bindings.json 绑定目标
```

### 场景 B：交付复核并阻塞等待结论（最常用）
```bash
# 自动匹配当前工程会话（或已绑定会话）：
agent-pair-ipc antigravity -w -t 300 -f docs/reports/REVIEW_REQUEST.md

# 指定具体会话 ID 或标题：
agent-pair-ipc antigravity -s <conversation_id> -w -t 300 -f docs/reports/REVIEW_REQUEST.md

# 约定交付物文件（文件落盘稳定且含 VERDICT 行即刻唤醒返回）：
agent-pair-ipc antigravity -w -t 600 --expect-file docs/reports/ANTIGRAVITY_REVIEW.md -f docs/reports/REVIEW_REQUEST.md
```

### 场景 C：直接读取 Antigravity 最新回复（不发送新消息）
```bash
agent-pair-ipc antigravity -r                       # 最新 1 条终结回复
agent-pair-ipc antigravity -r -s <conversation_id>  # 指定会话
agent-pair-ipc antigravity -r -n 3                  # 最近 3 轮
```

### 场景 D：会话绑定管理与多会话消歧
```bash
agent-pair-ipc antigravity --bind <conversation_id>   # 持久化绑定到 .agent-pair/bindings.json
agent-pair-ipc antigravity --unbind                   # 解除绑定
agent-pair-ipc antigravity -w --pick-latest -f review.md  # 同工程多会话时取最近活跃的一条
```

### 场景 E：当前工程尚无桌面端会话 —— GUI 原生新建并挂载
```bash
# -g：激活 Antigravity Desktop 触发新建，自动侦测新会话并绑定后投递
agent-pair-ipc antigravity --await-new-session -g --await-timeout 180 -w -f docs/reports/REVIEW_REQUEST.md
```

### 场景 F：跨工程显式调用（默认严格阻断）
默认禁止向与当前 CWD 无关的会话投递（防止串线）。确需跨目录通信时，必须显式提供目标工程目录路径：
```bash
agent-pair-ipc antigravity -s <conversation_id> \
  --allow-cross-project=/path/to/target-project \
  --no-auto-new -w -f review.md
```

---

## 4. 作为接收方：回复对等请求与取件

- 回复带有 `[AgentPairIPC-Req-ID: req_xxx]` 的对等请求：
  ```bash
  agent-pair-ipc reply --to req_xxx -f answer.md
  ```
- 若发起方超时退出或使用了非阻塞发送，可从收件箱取回晚到的回复：
  ```bash
  agent-pair-ipc inbox
  agent-pair-ipc inbox req_0123456789ab
  ```

---

## 5. 退出码契约（Exit Codes）

- `0` (`EXIT_SIGN_OFF`)：复核通过（最后一行 `VERDICT: SIGN_OFF`），或 `-l` / `-r` / `--bind` / 非阻塞发送成功。
- `1` (`EXIT_ERROR`)：执行错误、目标不存在、投递失败或等待超时。
- `2` (`EXIT_CHANGES_REQUESTED`)：复核退回（最后一行 `VERDICT: CHANGES_REQUESTED`）。
- `3` (`EXIT_NO_VERDICT`)：对方完成回复但未包含标准 `VERDICT` 行。
- `4` (`EXIT_QUEUED_UNCONSUMED`)：消息在 `--fast-fail`（默认 15s）内未在转录中看到消费迹象，请检查桌面端会话是否忙碌或等待授权，并可稍后用 `-r` 或 `agent-pair-ipc inbox <Req-ID>` 取件。
- `5` (`EXIT_NEEDS_NEW_THREAD`)：当前工程无会话且运行于非交互环境（non-TTY），提示先在 Antigravity Desktop 新建会话或改用 `--await-new-session`。

---

## 6. 参数详解

| 参数 | 说明 | 示例 |
|---|---|---|
| `-w`, `--wait` | 阻塞等待对方完成并打印结论（回调优先，转录兜底） | `-w` |
| `-t`, `--timeout`, `--wait-timeout` | 等待超时秒数（默认 300） | `-t 600` |
| `--expect-file` / `--output-file` | 期望产物文件路径；稳定且含 `VERDICT` 时提前返回 | `--expect-file review.md` |
| `--fast-fail` | 未检测到消费时的快速失败秒数（默认 15） | `--fast-fail 30` |
| `-f`, `--file` | 读取文件发送；`-f -` 读取 stdin（推荐配合 `<<'AP_EOF'`） | `-f report.md` |
| `-m`, `--msg` | 短文本内联（富文本请改用 `-f`） | `-m "ping"` |
| `--prefix` | 在文件/stdin 正文前拼接前缀说明 | `--prefix "【复核】"` |
| `-s`, `--session` | 指定目标会话 UUID 或标题 | `-s f06a8716-...` |
| `--bind` / `--unbind` | 写入 / 移除 `.agent-pair/bindings.json` 中的绑定 | `--bind <id>` |
| `--pick-latest` | 当前工程多会话时自动选最近活跃的一条 | `--pick-latest` |
| `--await-new-session` | 等待当前工程新会话出现并自动绑定后发送 | `--await-new-session` |
| `--no-auto-new` | 无会话时不自动进入等待新建流程 | `--no-auto-new` |
| `-g`, `--activate-gui` | 等待新会话时自动唤起 Antigravity Desktop | `-g` |
| `--allow-cross-project=DIR` | 显式允许跨工程投递至指定目录 `DIR` | `--allow-cross-project=/path/to/repo` |
| `--intent` / `--user-context` / `--sender` | 对等意图声明、如实的用户原话引用、发送方名称 | `--intent "Read-Only Review"` |
| `-r`, `--read` / `-n` / `-a` | 读取最新回复 / 条数 / 含中间工具调用轮次 | `-r -n 3` |
| `-l`, `--list` | 列出最近 30 条 Antigravity Desktop 会话 | `-l` |
| `--debug` | 输出详细调试日志 | `--debug` |

---

## 7. 常见问题排查

1. **报错 `trajectory not found` / `conversation not found`**：所有存活的桌面端 Language Server 均未加载该轨迹；请在 Antigravity Desktop 中点开该会话窗口后重试。
2. **报错 `connect: operation not permitted`**：调用方所在沙箱阻断了本地 `localhost` 端口访问；请为当前命令申请放行本地网络/沙箱权限后重试。
3. **不要触碰桌面端 SQLite 附属文件**：`~/.gemini/antigravity/conversations/<id>.db-shm` 与 `-wal` 为 SQLite WAL 正常文件，切勿手动删除。

脚本入口：`agent-pair-ipc antigravity ...`、`antigravity-ipc ...`，或本 Skill 内的 `python3 scripts/antigravity_ipc.py ...`（三者等价）。
