# Duplex Reply Protocol (agent-pair-ipc 2.0)

Both upstream channels are one-way: Claude Code's peer socket accepts `auth` + `user` frames and
`codex queue` only enqueues. Before v0.3 the requester had to poll the receiver's private JSONL
transcript to learn the answer, and a receiver that wanted to answer had no address to answer to
("the session that sent this can't be reached, so I wrote it to an MD file").
The duplex reply channel gives every request a return address.

## 1. Lifecycle

```
requester                                   receiver (Claude Code / Codex)
─────────                                   ──────────────────────────────
nonce = req_<12 hex>
bind ~/.agent-pair/run/reply-<nonce>.sock (0600, dir 0700)
send Peer Envelope ───────────────────────► sees [AgentPairIPC-Req-ID: <nonce>] and
  (Req-ID, intent, honest authorization,       the exact `reply --to <nonce>` command
   reply command)                              ...works...
              ◄──────────── progress frame ─── agent-pair-ipc reply --to <nonce> --progress -m "..."
ack ───────────────────────────────────────►
              ◄──────────── reply frame ────── agent-pair-ipc reply --to <nonce> -f - <<'AP_REPLY_EOF'
ack ───────────────────────────────────────►
return (text, verdict)                        ends its turn normally
```

The transcript tail keeps running in parallel. If the receiver never runs the reply command,
its final transcript answer is returned exactly as before. When the transcript finishes first,
the requester gives the socket a short grace period (1 s, or 5 s if it saw the receiver invoke
`reply --to <nonce>`), because the deliberate reply beats a chat summary such as "replied via IPC".

## 2. Frames

One JSON object per connection. The replier sends it, half-closes (`SHUT_WR`), and reads one ack line.

```json
{"x_agent_ipc_version": "2.0", "type": "reply", "correlation_id": "req_8f3e2a0c1b2d",
 "sender": "Claude Code", "content": "...full answer...\nVERDICT: SIGN_OFF"}
```

| `type` | Meaning | Requester behaviour |
|---|---|---|
| `reply` | Final answer | ack, stop waiting, verdict = last line of `content` |
| `progress` | Interim status | ack, print to stderr, keep waiting |

Ack: `{"ok": true, "correlation_id": "<nonce>"}`, or `{"ok": false, "error": "..."}` when the
`correlation_id` does not match or the frame exceeds 32 MiB.
Plain-text frames (v1 replies) are still accepted as final replies.

The verdict is **always** derived from the last line of `content`. A top-level `"verdict"` field is ignored.

## 3. Mailbox (offline delivery)

If nobody is listening (the requester timed out, hit the Codex fast-fail, or sent fire-and-forget),
`reply` stores the final answer atomically in `~/.agent-pair/run/replies/<nonce>.json` (0600).
The requester picks it up with:

```bash
agent-pair-ipc inbox                   # list
agent-pair-ipc inbox req_8f3e2a0c1b2d  # print; exit code follows the VERDICT line
```

A waiting requester also checks the mailbox on every poll, so a reply stored during a brief socket
outage is still delivered. Progress frames are never stored. Files older than three days are
cleaned up opportunistically.

## 4. Safety properties

- The reply socket path is **derived from the nonce** inside the private run dir. It is never taken
  from message text, so a forged envelope cannot redirect a reply to an arbitrary socket.
- Nonces must match `^[A-Za-z0-9_-]{4,64}$`, so they cannot traverse paths.
- A peer message is never user approval. The envelope says so explicitly, which matches the
  receiving agent's own policy instead of fighting it.

## 5. File-based payload pipeline

- Senders: `-f <file>` or `-f - <<'AP_EOF'` (quoted heredoc). Never `"..."` strings, because the shell
  evaluates backticks and `$(...)` inside double quotes before the tool ever sees the text.
- Receivers: the advertised reply command uses the same pipeline (`-f answer.md` / `-f - <<'AP_REPLY_EOF'`).
- `codex queue` only accepts `--message` (argv, size-limited). Bodies over 128 KiB are written to
  `~/.agent-pair/run/ap_payload_*.md` (0600) and the queued message keeps the envelope inline
  plus a pointer to that file.
