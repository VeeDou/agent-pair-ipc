# Antigravity Desktop transport

`antigravity-ipc` discovers Desktop conversations in
`~/.gemini/antigravity/conversation_summaries.db`. It does not resume Desktop
conversations using the independent `agy` CLI conversation store.

Desktop may have multiple language-server daemons. The adapter probes each live
`daemon/hub_*.json` endpoint using `agentapi get-conversation-metadata`, then sends
to endpoints that can see the conversation first. Metadata visibility is not
proof that a trajectory is loaded. Only explicit `trajectory not found` or
`conversation not found` rejections permit trying another endpoint. Timeouts and
other failures stop delivery to avoid duplicate messages.

An explicit `ANTIGRAVITY_LS_ADDRESS` takes precedence over discovery. Its matching
`ANTIGRAVITY_CSRF_TOKEN` must also be supplied. Tokens are never logged.

If SQLite cannot open its shared-memory file, discovery reads a temporary copy
of the database and its WAL, after checking that the source did not change while
copying. The original database is never opened with `immutable=1`, which would
omit recent WAL transactions.

## Connectivity check

Run from this repository in a terminal with access to Desktop's localhost ports:

```sh
python3 -m agent_pair_ipc.antigravity \
  -s <conversation-id> \
  --allow-cross-project=/path/to/target-project \
  --no-auto-new --debug -w -t 60 \
  -m 'IPC connectivity test only. Reply IPC_OK and VERDICT: SIGN_OFF. Do not execute tools or modify files.'
```

Success requires receiving the new correlated response, not merely discovering
the conversation or accepting the send RPC. If every endpoint rejects the
trajectory, open that conversation in Desktop and retry. This adapter does not
yet implement a separate Desktop trajectory-loading RPC.

`connect: operation not permitted` indicates the calling environment cannot
connect to the local service; it does not establish whether Desktop accepts the
conversation. On 2026-11-20 discovery of the test session succeeded, but the
managed execution environment blocked localhost RPCs, so actual delivery and
reply receipt remain unverified.
