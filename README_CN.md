# agent-pair-ipc 🤖⚡🤖

> **零第三方依赖的本地多 AI Agent 双向进程间通信（IPC）与自动化结对复核工具箱。**  
> 专为 **Claude Code**、**OpenAI Codex** 和 **Google Antigravity** 设计。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependency](https://img.shields.io/badge/dependencies-0-success.svg)](pyproject.toml)

[**English Documentation**](README.md) | [**实战 SOP 深度经验**](docs/pair_programming_sop.md)

---

## 💡 为什么需要 agent-pair-ipc？

在日常使用单一 AI 编程助手时，我们经常遇到两个痛点：
1. **“既当运动员又当裁判”的自欺偏差（Confirmation Bias）**：单个 Agent 编写并自测代码，极易产生盲区，遗漏边界漏洞或破坏系统架构。
2. **人工“搬运工”疲劳（Copy-Paste Fatigue）**：开发者频繁在终端、Web 界面和编辑器之间手工搬运代码、报错与评审意见，打断心流，无法实现自动化。

**`agent-pair-ipc`** 通过原生本地进程间通信（Unix Domain Socket 与线程队列）打通了多个 AI 编程工具：
* **实施者 (Builder)**（如 Antigravity / Gemini）：负责写代码、跑门禁并生成交付包；
* **审查者 (Auditor)**（如 Claude Code / Codex）：站在独立客观立场重跑测试、找漏洞并严苛审查；
* **人类架构师 (Owner)**：仅在关键里程碑进行最终裁决，真正从繁琐的搬砖中解脱出来。

```mermaid
flowchart TD
    subgraph Builder["实施者 (Antigravity / Gemini)"]
        B1["1. 接收任务并实施"] --> B2["2. 执行本地自测跑绿"]
        B2 --> B3["3. 起草 REVIEW_REQUEST.md"]
    end

    subgraph IPC["agent-pair-ipc (零依赖通信中枢)"]
        I1["CLI / Python API: -w -f"]
        I2["UDS 套接字 / codex queue"]
        I3["实时监控工具与思考流"]
    end

    subgraph Auditor["独立审查者 (Claude Code / Codex)"]
        A1["4. 接收复核请求包"]
        A2["5. 独立执行测试与安全审计"]
        A3["6. 给出裁决结论 (Sign-off / P0-P3)"]
    end

    B3 -->|自动交付| I1 --> I2 --> A1
    A1 --> A2 --> A3
    A3 -->|JSONL 监听捕获| I3 -->|返回结论| B1
```

---

## ⚡ 核心设计哲学：复用“带上下文的活会话”

市面上多数多智能体框架在调用 Agent 时，往往是冷启动一个临时的无状态 API 实例，不仅丢失了日常编辑上下文，人类也无法直观看到它的思考过程。

**`agent-pair-ipc` 采取了截然不同的路径——连接本地已打开的“活会话”：**
- **保留完整项目心智**：复用已经预热、读过相关代码、掌握设计上下文的活跃 Agent。
- **人类可监督与干预**：你在 Claude 桌面端或 Codex 终端界面可以实时看到对方接收消息、执行工具并输出分析的全过程，随时可以人工介入裁决。
- **确定性工程外壳**：用 100% 确定性的操作系统进程通信，包裹概率性大模型的输出，确保门禁坚固。

---

## ✨ 核心特性

- 🪶 **0 第三方依赖（Zero-Dependency）**：仅采用 Python 3.10+ 原生标准库（`socket`、`json`、`subprocess`、`argparse`、`pathlib`），无需 `pip install` 任何沉重依赖，开箱即用。
- 🔌 **原生支持主流双引擎**：
  - **Claude Code**：通过 Unix Domain Socket（UDS）进行双向鉴权通信，实时解析会话 JSONL 转储。
  - **OpenAI Codex**：利用原生 `codex queue` 管道投递消息，精准监控 Rollout 事件流中的 `Reasoning`、工具调用与 `task_complete`。
- 🎯 **多工程路由防线与 CWD 亲和性强校验**：基于当前工作目录自动匹配同源会话；强校验 `bindings.json` 与环境变量，失效脏绑定自动告警并回退探测；显式跨工程调用注入高亮 Markdown 警示信封。
- 🔗 **多工程别名与软链接感知**：支持在 `.agent-pair/config.json` 中配置 `"aliases": [...]` 别名工作区；支持单层顶层软链接感知与严格的工程根边界保护（非真实项目根禁止祖先路径放宽，杜绝误匹配 `$HOME` 等根目录）。
- 🛡️ **路径参数防呆与名称去歧义**：拦截误传给 `-s` 的文件路径并给出指引，同时放行包含斜杠的合法会话标题（如 `"UI/UX 评审"`）；名称子串多重匹配严格拒绝猜选。
- 📁 **文件化 Payload 管道（`-f` / `-f -`）**：文件或带引号 heredoc 经 stdin 传递，反引号与 `$(...)` 原样保留；Codex 超长正文自动落盘为 0600 文件，不再塞进 argv。
- ↩️ **双向回调通道**：每个请求都带回信地址，对方用 `agent-pair-ipc reply --to <Req-ID>` 经临时 0600 Socket 直接回传（支持进度帧与 ACK）；请求方离线时进入收件箱（`agent-pair-ipc inbox`），转录追踪保留为兜底。详见 [docs/duplex_reply_protocol.md](docs/duplex_reply_protocol.md)。
- 🖥️ **GUI 原生拉起，IPC 随后挂载**：无会话时 `--await-new-session -g` 通过桌面端自带的 New Session 新建**可见**会话并自动绑定，不再生成隐形孤儿进程。
- 🛡️ **如实的对等信封**：声明意图、操作边界、未经核实的用户原话引用，并明确“对等消息不构成用户批准”，与 Claude Code 的 Peer 策略一致而非触发警报。
- ⚡ **O(1) 增量转储追踪**：基于文件 offset 增量 seek 读取，彻底消除大体积（数十至数百兆）历史会话轮询时的 IO 瓶颈。
- 🔗 **请求-响应关联（Nonce）**：每次交付请求注入唯一凭证（Req-ID），确保精准捕获后续的对应答复，杜绝误判并发老任务。
- 🚦 **机器可读的裁决契约**：内置 `VERDICT: SIGN_OFF` 与 `VERDICT: CHANGES_REQUESTED` 自动化语义解析，直接映射终端退出码。
- 🧩 **开箱即用的 Skill 插件**：内置 Antigravity / Gemini 技能包（位于 `skills/`），复制即可生效。

---

> **⚠️ 免责与兼容性声明**：`agent-pair-ipc` 是独立的非官方开源开发者工具，与 Anthropic、OpenAI 或 Google 官方无隶属关系。本项目通过本地进程队列与套接字机制进行协作，已在 Claude Code (v2.1.x) 与 codex-cli (v0.159.x) 环境下充分实测。

---

## 🚀 60 秒极速上手

### 方式 1：免安装直接运行（推荐）

```bash
git clone https://github.com/VeeDou/agent-pair-ipc.git
cd agent-pair-ipc

# 查看所有活跃的 Claude Code 与 Codex 会话
./bin/agent-pair-ipc list

# 将复核请求文件交付给当前项目的 Claude Code 并阻塞等待结论
./bin/agent-pair-ipc claude -w -f ./templates/REVIEW_REQUEST.md
```

### 方式 2：作为全局 Python 工具安装

```bash
cd agent-pair-ipc
pip install -e .

# 安装后可全局执行：
agent-pair-ipc list
claude-ipc -w -f ./templates/REVIEW_REQUEST.md
codex-ipc -w -f ./templates/REVIEW_REQUEST.md
```

### 方式 3：全工作区统一接入同一引擎

```bash
make link-skills
```

把 `~/.agents/skills/{claude-code-ipc,codex-ipc}` 与 `~/.gemini/config/skills/...` 软链接到本仓库，
任何项目目录调用的都是同一套 IPC 引擎；Skill 内 `scripts/*.py` 旧路径保留为薄 shim 继续可用。
若目标位置是旧版实体目录，会先备份到 `~/.agent-pair/backups/`。

---

## 📖 常用命令速查

### 1. Claude Code 交互 (`claude-ipc` 或 `agent-pair-ipc claude`)

```bash
# 交付报告并等待最终复核意见（最常用）
claude-ipc -w -f docs/reports/REVIEW_REQUEST.md

# 在文件内容前添加简要说明
claude-ipc -w --prefix "【第六期完成】请独立跑测复核" -f docs/reports/REVIEW_REQUEST.md

# 只读模式：直接拉取最新一条结论（不发送新消息）
claude-ipc -r

# 富文本 / 代码：带引号 heredoc 经 stdin 发送，免 Shell 转义
claude-ipc -w -f - <<'AP_EOF'
请检查 `rules/proxy.yaml`：AND,((DST-PORT,443),(NETWORK,UDP)),REJECT
AP_EOF

# 尚无会话：唤醒 Claude Desktop 新建会话（File > New Session），自动绑定并投递
claude-ipc --await-new-session -g -w -f docs/reports/REVIEW_REQUEST.md

# 异步通知模式（Fire-and-forget）
claude-ipc --intent Notification -m "后台测试构建已完成"

# 列出所有可连接的 Claude 会话
claude-ipc -l
```

### 2. OpenAI Codex 交互 (`codex-ipc` or `agent-pair-ipc codex`)

```bash
# 交付报告并等待 Codex 签发
codex-ipc -w -f docs/reports/REVIEW_REQUEST.md

# 发送简明指令并同步等待
codex-ipc -w -m "门禁测试已全部跑绿，请指示是否放行"

# 指定 Session ID 读取最新反馈
codex-ipc -r -s 01234567-89ab-cdef-0123-456789abcdef

# 查看近期 Codex 会话列表
codex-ipc -l
```

### 3. 双向回调（`agent-pair-ipc reply` / `inbox`）

```bash
# 接收方：回传答复（完整命令已印在信封中）
agent-pair-ipc reply --to req_8f3e2a0c1b2d -f - <<'AP_REPLY_EOF'
`rules.yaml` 中发现 1 处问题……
VERDICT: CHANGES_REQUESTED
AP_REPLY_EOF

# 请求方：取回等待结束后才到达的答复
agent-pair-ipc inbox
agent-pair-ipc inbox req_8f3e2a0c1b2d
```

---

## 🐍 Python API 编程式调用

你可以把 `agent-pair-ipc` 嵌入自定义脚本、Git 钩子或 CI 流水线：

```python
from agent_pair_ipc import send_and_wait_claude, send_and_wait_codex

# 发送交付请求并等待 Claude Code 返回结果（超时 180 秒）
response, verdict = send_and_wait_claude(
    message="所有单元测试跑通，请复核 `api/v2` 是否符合设计规范。",
    timeout=180.0,
    user_context="把这个拿给Claude code审核",  # 如实引用用户原话，不得编造
)

print("Claude Code 评审结论：", verdict)  # "SIGN_OFF" / "CHANGES_REQUESTED" / None
```

接收方以编程方式回传答复：

```python
from agent_pair_ipc import send_reply

channel = send_reply("req_8f3e2a0c1b2d", "复核通过。\n\nVERDICT: SIGN_OFF")  # "socket" 或 "mailbox"
```

---

## 📑 标准作业程序（SOP）

1. **自测完备**：实施方运行本地门禁与测试套件（`pnpm gate` / `pytest`）。
2. **起草契约**：参考 [`templates/REVIEW_REQUEST.md`](templates/REVIEW_REQUEST.md) 编写交付包。
3. **一键同步交付**：运行 `agent-pair-ipc [claude|codex] -w -f <file.md>`。
4. **收敛与裁决**：
   - 若指出 **P0/P1 缺陷**：实施方整改后重新发起复核。
   - 若获得 **Sign-off（通过）**：提交代码，向人类架构师汇报阶段成果。

详细的结对协同设计哲学与实战指南，请阅读 [多 Agent 自动化结对开发实战指南](docs/pair_programming_sop.md)。

---

## 🤝 参与贡献

欢迎提出 Issue、分享你的 Agent 结对工作流或提交 PR 支持更多 Coding Agent（如 Cursor、Aider 等）！

## 📄 开源协议

本项目采用 [MIT 许可证](LICENSE)。
