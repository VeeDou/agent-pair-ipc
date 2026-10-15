# Claude Code IPC Protocol Reference

## 1. Overview

Claude Code running locally (CLI or Desktop app) listens on a Unix Domain Socket (UDS) for local integrations, external tool events, and automated review handoffs.

## 2. File & Directory Layout

- **Session Metadata Directory**: `~/.claude/sessions/`
- **Session JSON**: `~/.claude/sessions/<pid>.json`
  - `pid`: Process ID (e.g. `22286`)
  - `sessionId`: Internal Claude UUID (e.g. `11223344-5566-7788-99aa-bbccddeeff00`)
  - `hostSessionId`: Host / UI display session ID (e.g. `local_12345678-abcd-ef01-2345-6789abcdef01`)
  - `name`: Session title (e.g. `Architecture-Refactor-Design`)
  - `cwd`: Project working directory
  - `kind`: Session mode (`interactive`, `bg`, etc.)
  - `entrypoint`: Application type (`claude-desktop`, `cli`)
  - `messagingSocketPath`: Socket URI (e.g. `/tmp/cc-socks/22286.sock`)
- **Peer Authentication Token**: `~/.claude/sessions/<pid>.<hash>.key`
  - Format: `{"peerToken": "3587be69..."}`
  - Required for authenticating with the socket server.

## 3. Communication Protocol

1. **Connect**: Connect via `AF_UNIX` stream socket to `messagingSocketPath`.
2. **Authentication Frame**:
   ```json
   {"type": "auth", "token": "<peerToken>"}\n
   ```
3. **User Message Frame**:
   ```json
   {"type": "user", "message": {"role": "user", "content": "<text>"}}\n
   ```
4. **Close**: Close connection after frames are flushed.

## 4. Transcript Monitoring

Claude Code records JSONL transcripts under:
`~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`

When waiting for a response (`-w` mode):
- **Live Tool Tracking**: Detects assistant messages where `content` contains `{"type": "tool_use", "name": "..."}`.
- **Completion Detection**: Waits for an assistant turn where `stop_reason != "tool_use"` (typically `"end_turn"`).
- Extracts clean final text without intermediate tool payloads.
