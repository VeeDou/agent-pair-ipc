# Claude Code IPC Protocol Reference

## 1. Overview

Claude Code running locally (CLI or Desktop app) listens on a Unix Domain Socket (UDS) for local integrations, external tool events, and automated review handoffs.

## 2. File & Directory Layout

- **Session Metadata Directory**: `~/.claude/sessions/`
- **Session JSON**: `~/.claude/sessions/<pid>.json`
  - `pid`: Process ID (e.g. `22286`)
  - `sessionId`: Persistent Claude UUID (e.g. `11223344-5566-7788-99aa-bbccddeeff00`) — survives hibernation and wake-up; preferred for `.agent-pair/bindings.json`
  - `hostSessionId`: Ephemeral host / UI session ID (e.g. `local_12345678-abcd-ef01-2345-6789abcdef01`) — regenerates when Claude Desktop wakes a hibernated session
  - `name`: Session title (e.g. `Architecture-Refactor-Design`)
  - `cwd`: Project working directory
  - `kind`: Session mode (`interactive`, `bg`, etc.)
  - `entrypoint`: Application type (`claude-desktop`, `cli`)
  - `status` / `waitingFor`: Live state (`idle` / `busy` / `waiting`, with `waitingFor: "input needed"` when paused on `AskUserQuestion`)
  - `messagingSocketPath`: Socket URI (e.g. `/tmp/cc-socks/22286.sock`)
- **Peer Authentication Token**: `~/.claude/sessions/<pid>.<hash>.key`
  - Format: `{"peerToken": "3587be69..."}`
  - Required for authenticating with the socket server.

## 3. Communication Protocol

1. **Optional Ephemeral Receipt Listener**: Before connecting, bind a `0o600` Unix socket in the **exact same lexical directory** as `messagingSocketPath` (e.g. `/tmp/cc-socks/ap-<pid>-<rand>.sock` — without resolving `/tmp` symlinks so Claude Code's directory vetting accepts it) and generate a UUID4 `msg_id`.
2. **Connect**: Connect via `AF_UNIX` stream socket to `messagingSocketPath`.
3. **Authentication Frame**:
   ```json
   {"type": "auth", "token": "<peerToken>"}\n
   ```
4. **User Message Frame**:
   ```json
   {"type": "user", "from": "uds:/tmp/cc-socks/ap-<pid>-<rand>.sock", "msg_id": "<uuid4>", "message": {"role": "user", "content": "<text>", "metadata": {...}}}\n
   ```
5. **Half-Close & EOF**: Call `shutdown(SHUT_WR)` and close after server EOF (or short timeout).
6. **Native `peer_message_status` Reverse Receipts**: Claude Code never replies on the inbound socket. Instead, when `from` + UUID4 `msg_id` are provided, Claude Code vets the peer PID/UID and connects back to the receipt socket with a `control` frame:
   ```json
   {"type": "control", "action": "peer_message_status", "status": "held|delivered|refused|dropped|denied|expired", "status_detail": "refused", "reason": "...", "orig_msg_id": "<uuid4>"}
   ```
   - `status: "expired"` with `status_detail: "refused"` is normalized to `"refused"`.
   - `refused` / `dropped` / `denied` / `expired` allow immediate fail-fast without waiting `fast_fail_timeout`, guaranteeing the message was NOT queued and is safe to retry once fixed.
   - `held` confirms the message is parked in Claude Code's inbound gate memory (`Zo().inbound.held`): callers MUST NOT blindly resend.
7. **Desktop vs CLI Hold Behavior & Read-Only `permissionMode` Detection**:
   - Claude Code CLI TUI (`entrypoint: cli`) renders a bottom `"Review it below"` approval card for held messages.
   - Claude Desktop (`entrypoint: claude-desktop`) runs `claude` in headless `stream-json` mode with **no approval card UI**; held messages expire automatically after 5 minutes unless the user switches the Desktop session out of `bypassPermissions` (e.g. `auto`, `default`, `acceptEdits`, which immediately updates `claude-code-sessions/*/*/<hostSessionId>.json` and releases held messages) or `crossSessionInbound` becomes `"accept"`.
   - `agent-pair-ipc` performs strictly read-only `permissionMode` inspection (Desktop local session JSON first, JSONL transcript tail fallback) to surface `Mode:` in `-l` and give accurate guidance.

Answers to the request itself come back over the agent-pair-ipc duplex reply channel
(`agent-pair-ipc reply --to <Req-ID>`, advertised in every Peer Envelope) or the JSONL transcript. See
[duplex_reply_protocol.md](duplex_reply_protocol.md).

Session metadata also exposes `status` (`idle` / `busy`) and `updatedAt`. `claude-ipc --list` shows the status and `permissionMode`,
and the sender is told when its message will queue behind a busy turn.

## 4. Transcript Monitoring

Claude Code records JSONL transcripts under:
`~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl`

When waiting for a response (`-w` mode):
- **Live Tool Tracking**: Detects assistant messages where `content` contains `{"type": "tool_use", "name": "..."}`.
- **Interactive Input Detection**: Detects unanswered `AskUserQuestion` `tool_use` turns (as well as `status == "waiting"` + `waitingFor == "input needed"` in `~/.claude/sessions/<pid>.json`), formats the question and options, and exits immediately with code `6` (`EXIT_NEEDS_USER_INPUT`).
- **Completion Detection**: Waits for an assistant turn where `stop_reason != "tool_use"` (typically `"end_turn"`).
- Extracts clean final text without intermediate tool payloads or `api-request-blob` entries.
- The transcript is the **fallback** channel. A reply arriving on the duplex reply socket or mailbox wins.

When reading (`-r` mode) or listing (`-l` mode):
- If a session has hibernated (no `~/.claude/sessions/<pid>.json` or socket), `-r` automatically falls back to scanning `~/.claude/projects/*/*.jsonl` by `sessionId`, `.agent-pair/bindings.json`, or `custom-title`.
- `-l` displays active sessions (prioritizing persistent `sessionId`) alongside recent hibernated sessions for the current project.

## 5. New sessions (GUI-native creation)

Sessions started by a script are invisible to Claude Desktop, so `--await-new-session` never spawns `claude`.
Instead it snapshots existing sessions, optionally (`-g`) brings Claude Desktop to front and clicks
**File > New Session** (Cmd+N fallback), then binds the first *new* session whose `cwd` is this project
or one of its parent directories. New sessions opened in another folder are reported, not bound.
