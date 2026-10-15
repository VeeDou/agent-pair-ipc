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
