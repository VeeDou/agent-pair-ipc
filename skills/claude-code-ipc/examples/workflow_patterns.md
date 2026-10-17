# Workflow Patterns for Antigravity & Claude Code Collaboration

All payloads go through files or a quoted heredoc (`<<'AP_EOF'`), never through `"..."` strings:
inside double quotes the shell evaluates backticks and `$(...)` and silently drops inline code.

## Pattern 1: Review & Sign-Off Handoff

1. Antigravity writes the review/acceptance report to `docs/reports/REVIEW_REQUEST.md`.
2. Antigravity delivers it and waits for the verdict:
   ```bash
   python3 scripts/claude_code_ipc.py -w -f docs/reports/REVIEW_REQUEST.md
   ```
3. Claude Code answers via `agent-pair-ipc reply --to <Req-ID> ...` (the exact command is in the envelope);
   if it does not, the transcript fallback still returns its final answer. Exit code follows the `VERDICT:` line.

## Pattern 2: Reciprocal Consensus (Two-Agent Planning)

```bash
python3 scripts/claude_code_ipc.py -w -s local_eb736bc4 -f - <<'AP_EOF'
【Antigravity 方案复核意见】关于第 3.4 节，建议把 `rules/quic.yaml` 中的 UDP 443 规则前移……
AP_EOF
```

## Pattern 3: New project without a session

```bash
python3 scripts/claude_code_ipc.py --await-new-session -g -w -f docs/reports/REVIEW_REQUEST.md
```
Claude Desktop is brought to front and File > New Session is triggered; once the user picks this
project folder, the new session is auto-bound in `.agent-pair/bindings.json` and the report is delivered.

## Pattern 4: Listing Active Sessions

```bash
python3 scripts/claude_code_ipc.py --list
```
