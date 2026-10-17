# OpenAI Codex IPC Protocol Reference

## 1. Overview

Codex provides native support for thread queue injection and maintains a local rollout event log on disk, allowing programmatic communication without modifying internal binaries.

## 2. Directory Layout & Metadata

1. **Session Index**:
   `~/.codex/session_index.jsonl`
   Contains records of recent threads:
   ```json
   {"id": "01234567-89ab-cdef-0123-456789abcdef", "thread_name": "...", "updated_at": "..."}
   ```
2. **Session Rollouts**:
   `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl`
   Append-only event log capturing execution, thoughts, and outputs.

## 3. Communication Channel

1. **Input Injection**:
   Using the native Codex CLI command:
   ```bash
   codex queue --thread <session_id> --message <content>
   ```
   The message is passed as a single argv element (no shell involved). Bodies over 128 KiB are moved to a
   0600 payload file, and the queued message keeps the Peer Envelope plus a pointer to that file.
   Answers come back over the duplex reply channel. See [duplex_reply_protocol.md](duplex_reply_protocol.md).
2. **Session Meta Discovery**:
   The first few lines of each rollout file contain a `session_meta` event specifying the working directory (`cwd`):
   ```json
   {"type": "session_meta", "payload": {"cwd": "/path/to/project"}}
   ```

## 4. Rollout Event Stream Monitoring

When waiting synchronously (`-w` mode), the monitor tails the rollout file:
- **Reasoning**: `event_msg -> item_completed -> item.type == "Reasoning"`
- **Tool Execution**: `response_item -> payload.type == "custom_tool_call"`
- **Turn Complete**: `event_msg -> payload.type == "task_complete"` (provides authoritative `last_agent_message`)
- **Final Message**: `response_item -> payload.type == "message" (phase == "final_answer")`
- The rollout is the **fallback** channel. A reply arriving on the duplex reply socket or mailbox wins.
  After a fast-fail (exit 4), a later reply can be collected with `agent-pair-ipc inbox <Req-ID>`.

## 5. New threads (GUI-native creation)

Threads created by `codex exec` are orphans that never appear in the desktop sidebar. `--await-new-thread`
snapshots existing rollouts, optionally (`-g`) activates the desktop app (ChatGPT.app, File > New Chat),
and binds the first new non-`exec` rollout whose `cwd` is this project or one of its parent directories.
