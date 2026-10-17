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

The peer socket is one-way. Answers come back over the agent-pair-ipc duplex reply channel
(`agent-pair-ipc reply --to <Req-ID>`, advertised in every Peer Envelope). See
[duplex_reply_protocol.md](duplex_reply_protocol.md).

Session metadata also exposes `status` (`idle` / `busy`) and `updatedAt`. `claude-ipc --list` shows the status,
and the sender is told when its message will queue behind a busy turn.

## 4. Transcript Monitoring

Claude Code records JSONL transcripts under:
`~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`

When waiting for a response (`-w` mode):
- **Live Tool Tracking**: Detects assistant messages where `content` contains `{"type": "tool_use", "name": "..."}`.
- **Completion Detection**: Waits for an assistant turn where `stop_reason != "tool_use"` (typically `"end_turn"`).
- Extracts clean final text without intermediate tool payloads.
- The transcript is the **fallback** channel. A reply arriving on the duplex reply socket or mailbox wins.

## 5. New sessions (GUI-native creation)

Sessions started by a script are invisible to Claude Desktop, so `--await-new-session` never spawns `claude`.
Instead it snapshots existing sessions, optionally (`-g`) brings Claude Desktop to front and clicks
**File > New Session** (Cmd+N fallback), then binds the first *new* session whose `cwd` is this project
or one of its parent directories. New sessions opened in another folder are reported, not bound.
