"""
Claude Code IPC Communication Utility.
Enables programmatic two-way messaging, live tool tracking, and feedback retrieval
with active Claude Code desktop / CLI sessions via Unix Domain Sockets (UDS) and JSONL transcripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from pathlib import Path

from agent_pair_ipc.common import (
    EXIT_CHANGES_REQUESTED,
    EXIT_ERROR,
    EXIT_NEEDS_NEW_THREAD,
    EXIT_NO_VERDICT,
    EXIT_SIGN_OFF,
    EphemeralReplyServer,
    NeedsNewSessionError,
    TailReader,
    activate_desktop_app,
    attach_request_header,
    create_safe_payload_file,
    extract_nonce,
    extract_peer_envelope,
    extract_verdict,
    format_peer_envelope,
    generate_nonce,
    load_project_bindings,
    resolve_message_payload,
    save_project_binding,
    unbind_project_binding,
)


def resolve_target_session(target: str | None = None, cwd: str | None = None) -> tuple[str | None, str]:
    """
    Resolve target session through explicit binding priority chain:
      1. Explicit CLI argument / flag (`-s <id_or_title>`)
      2. Environment variable `AGENT_PAIR_CLAUDE_SESSION`
      3. Project binding file `.agent-pair/bindings.json`
      4. Auto-match fallback (returns None, "auto")
    """
    if target and target.strip():
        return target.strip(), "cli_flag"

    env_target = os.environ.get("AGENT_PAIR_CLAUDE_SESSION")
    if env_target and env_target.strip():
        return env_target.strip(), "env_var"

    bindings = load_project_bindings(cwd)
    bound = bindings.get("claude") or bindings.get("claude_session")
    if bound and bound.strip():
        return bound.strip(), "bindings_file"

    return None, "auto"


def find_transcript_path(cwd: str, session_id: str) -> str | None:
    """
    Resolve the JSONL transcript file path for a Claude Code session.
    Claude Code sanitizes CWD by replacing all non-alphanumeric characters with '-'.
    """
    if not session_id:
        return None

    # Method 1: Sanitize cwd by replacing all non-alphanumerics with '-'
    sanitized_cwd = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    p1 = Path.home() / ".claude" / "projects" / sanitized_cwd / f"{session_id}.jsonl"
    if p1.is_file():
        return str(p1)

    # Method 2: Glob search in ~/.claude/projects/
    projs = Path.home() / ".claude" / "projects"
    if projs.is_dir():
        matches = list(projs.glob(f"**/{session_id}.jsonl"))
        if matches:
            return str(matches[0])

    return None


def get_self_session_identifiers() -> dict[str, str]:
    """Retrieve identifiers for the current process if running inside Claude Code."""
    return {
        "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "host_session_id": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID", ""),
        "pid": os.environ.get("CLAUDE_PID", ""),
    }


def get_active_sessions(exclude_self: bool = True, debug: bool = False) -> list[dict]:
    """
    Scan ~/.claude/sessions/ for active sessions with living UDS sockets and valid tokens.
    Excludes the invoking session if the caller itself is running inside Claude Code.
    """
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return []

    self_ids = get_self_session_identifiers()
    candidates = []

    for json_file in sessions_dir.glob("*.json"):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            sock = data.get("messagingSocketPath")
            name = data.get("name")
            session_id = data.get("sessionId")
            host_session_id = data.get("hostSessionId")
            kind = data.get("kind", "unknown")
            entrypoint = data.get("entrypoint", "")
            cwd = data.get("cwd", "")
            proc_start = data.get("procStart")
            pid_domain = data.get("pidDomain")

            # Check self-exclusion
            if exclude_self:
                if (
                    (self_ids["pid"] and str(pid) == str(self_ids["pid"]))
                    or (self_ids["session_id"] and session_id == self_ids["session_id"])
                    or (self_ids["host_session_id"] and host_session_id == self_ids["host_session_id"])
                ):
                    if debug:
                        print(f"[Claude Code IPC Debug] Excluding self session PID {pid}", file=sys.stderr)
                    continue

            # Verify socket exists
            if not pid or not sock or not os.path.exists(sock):
                continue

            # Find matching auth key file, checking procStart / pidDomain if present
            key_files = list(sessions_dir.glob(f"{pid}.*.key"))
            token = None
            for kf_path in key_files:
                try:
                    with open(kf_path, "r", encoding="utf-8") as kf:
                        kdata = json.load(kf)
                    if proc_start and kdata.get("procStart") and proc_start != kdata.get("procStart"):
                        continue
                    if pid_domain and kdata.get("pidDomain") and pid_domain != kdata.get("pidDomain"):
                        continue
                    token = kdata.get("peerToken")
                    if token:
                        break
                except Exception:
                    continue

            if not token:
                continue

            transcript_path = find_transcript_path(cwd, session_id)

            item = {
                "pid": pid,
                "name": name or "",
                "session_id": session_id or "",
                "host_session_id": host_session_id or "",
                "kind": kind,
                "entrypoint": entrypoint,
                "cwd": cwd,
                "socket_path": sock,
                "token": token,
                "transcript_path": transcript_path,
                "mtime": json_file.stat().st_mtime,
            }
            candidates.append(item)
        except Exception as e:
            if debug:
                print(f"[Claude Code IPC Debug] Error reading {json_file}: {e}", file=sys.stderr)
            continue

    # Sort candidates by mtime descending (most recently active first)
    candidates.sort(key=lambda x: x["mtime"], reverse=True)
    return candidates


def match_session(
    target: str | None = None,
    candidates: list[dict] | None = None,
    allow_cwd_fallback: bool = True,
    pick_latest: bool = False,
    debug: bool = False,
) -> tuple[dict | None, str]:
    """
    Match a session from candidates by target query:
    - host_session_id (exact or prefix >= 6 chars)
    - session_id (exact or prefix >= 6 chars)
    - pid (exact string)
    - name (exact or case-insensitive substring)
    
    If target is not given, matches session by current project CWD.
    If multiple sessions match CWD:
      - If `pick_latest` is True: picks the latest active session
      - If `pick_latest` is False: REFUSES to guess, prints candidate list, returns (None, "ambiguous")

    Returns:
        tuple[session_dict | None, status_str] where status_str is 'matched', 'ambiguous', or 'none'.
    """
    if candidates is None:
        candidates = get_active_sessions(exclude_self=True, debug=debug)

    if not candidates:
        return None, "none"

    resolved_target, source_kind = resolve_target_session(target)

    if resolved_target:
        target_str = str(resolved_target).strip()
        # 1. Exact match on host_session_id
        for c in candidates:
            if c["host_session_id"] == target_str:
                return c, "matched"
        # 2. Exact match on session_id
        for c in candidates:
            if c["session_id"] == target_str:
                return c, "matched"
        # 3. Exact match on PID
        for c in candidates:
            if str(c["pid"]) == target_str:
                return c, "matched"
        # 4. Exact match on name
        for c in candidates:
            if c["name"] == target_str:
                return c, "matched"
        # 5. Prefix match on host_session_id (>= 6 chars)
        if len(target_str) >= 6:
            for c in candidates:
                if c["host_session_id"].startswith(target_str):
                    return c, "matched"
        # 6. Prefix match on session_id (>= 6 chars)
        if len(target_str) >= 6:
            for c in candidates:
                if c["session_id"].startswith(target_str):
                    return c, "matched"
        # 7. Substring match on name (case-insensitive)
        for c in candidates:
            if target_str.lower() in c["name"].lower():
                return c, "matched"

        if source_kind == "bindings_file":
            print(f"\n[Claude Code IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
            print("👉 The bound session may have ended or been closed.", file=sys.stderr)
            print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)

        return None, "none"

    if not allow_cwd_fallback:
        return None, "none"

    # Match by current working directory (CWD)
    current_cwd = os.getcwd()
    cwd_matches = [c for c in candidates if c.get("cwd") == current_cwd]
    if not cwd_matches:
        return None, "none"

    if len(cwd_matches) == 1:
        return cwd_matches[0], "matched"
    elif pick_latest:
        cwd_matches.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
        chosen = cwd_matches[0]
        sid_display = chosen.get("host_session_id") or chosen.get("session_id") or str(chosen.get("pid"))
        print(f"[Claude Code IPC] Notice: Multiple sessions found for this project. Using latest due to `--pick-latest`: \"{chosen.get('name')}\" [{sid_display}]", file=sys.stderr)
        return chosen, "matched"
    else:
        # Strict disambiguation: refuse to guess!
        print(f"\n[Claude Code IPC] Error: Ambiguous sessions! Multiple active Claude Code sessions found for directory '{current_cwd}':", file=sys.stderr)
        for m in cwd_matches:
            mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m.get('mtime', 0.0))) if m.get('mtime') else 'unknown'
            sid_display = m.get('host_session_id') or m.get('session_id') or str(m.get('pid'))
            print(f"  • [{sid_display}] \"{m.get('name', 'Untitled')}\" (PID: {m.get('pid')}, Kind: {m.get('kind')}, Active: {mtime_str})", file=sys.stderr)
        print("\nPlease specify a target session with `-s <session_id|name|pid>`, set AGENT_PAIR_CLAUDE_SESSION,", file=sys.stderr)
        print("save to .agent-pair/bindings.json, or pass `--pick-latest` to confirm.\n", file=sys.stderr)
        return None, "ambiguous"


def await_new_session(
    timeout: float = 60.0,
    cwd: str | None = None,
    require_tty: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> dict | None:
    """
    Watch for a new Claude Code session matching current CWD.
    Snapshots pre-existing sessions to guarantee older sessions are never misidentified.
    In non-interactive environments (non-TTY), exits immediately instead of blocking.
    When detected, automatically binds hostSessionId/sessionId (never raw PID) to .agent-pair/bindings.json.
    """
    target_cwd = cwd or os.getcwd()
    if require_tty and not sys.stderr.isatty():
        print(f"[Claude Code IPC] Notice: No existing Claude Code session found for project '{target_cwd}'.", file=sys.stderr)
        print("[Claude Code IPC] Cannot interactively wait for new session in non-interactive environment (non-TTY).", file=sys.stderr)
        print("👉 Please open project in Claude Desktop or run `claude` in terminal, or run with -s <session_id>.", file=sys.stderr)
        raise NeedsNewSessionError("Cannot await new session in non-TTY environment")

    if activate_gui:
        print("[Claude Code IPC] Activating Claude Desktop application...", file=sys.stderr)
        if activate_desktop_app("Claude", new_tab=True):
            print("[Claude Code IPC] Activated Claude Desktop with new tab (Cmd+T).", file=sys.stderr)

    watch_start = time.time()
    print(f"[Claude Code IPC] Waiting for Claude Code session for '{target_cwd}'... (Open project in Claude Desktop or run `claude` in terminal)", file=sys.stderr)

    # Snapshot initial sessions
    initial = get_active_sessions(exclude_self=True, debug=debug)
    initial_ids = set()
    for s in initial:
        if s.get("host_session_id"):
            initial_ids.add(s["host_session_id"])
        if s.get("session_id"):
            initial_ids.add(s["session_id"])
        if s.get("pid"):
            initial_ids.add(str(s["pid"]))

    while time.time() - watch_start < timeout:
        time.sleep(1.0)
        elapsed = int(time.time() - watch_start)
        if sys.stderr.isatty():
            print(f"\r[Claude Code IPC] [侦测中 {elapsed:02d}s / {int(timeout)}s] 等待 Claude Desktop 新建会话... (按 Cmd+T 或 New Chat)", end="", file=sys.stderr)

        sessions = get_active_sessions(exclude_self=True, debug=debug)
        for s in sessions:
            if s.get("cwd") == target_cwd:
                sid = s.get("host_session_id") or s.get("session_id")
                pid_str = str(s.get("pid"))
                if (sid and sid in initial_ids) or (pid_str in initial_ids):
                    continue
                # Only bind stable host_session_id or session_id, never raw PID
                bind_id = s.get("host_session_id") or s.get("session_id")
                if bind_id:
                    save_project_binding("claude", bind_id, cwd=target_cwd)
                    if sys.stderr.isatty():
                        print("", file=sys.stderr)
                    print(f"[Claude Code IPC] 监听到新会话诞生: PID {s.get('pid')} [{bind_id}] -> 自动完成持久绑定！", file=sys.stderr)
                return s

    if sys.stderr.isatty():
        print("", file=sys.stderr)
    print(f"[Claude Code IPC] Timed out waiting for Claude Code session after {int(timeout)}s.", file=sys.stderr)
    return None


def send_message_to_claude(
    message: str,
    target: str | None = None,
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> bool:
    """Send message to target Claude Code session via Unix Domain Socket."""
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    session, status = match_session(target=target, candidates=candidates, pick_latest=pick_latest, debug=debug) if candidates else (None, "none")

    if not session:
        if status == "ambiguous":
            return False
        resolved_target, _ = resolve_target_session(target)
        if resolved_target is None and auto_await_new:
            session = await_new_session(timeout=60.0, cwd=os.getcwd(), activate_gui=activate_gui, debug=debug)

    if not session:
        if target:
            print(f"[Claude Code IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
        else:
            print(f"[Claude Code IPC] Error: No active session matches current directory '{os.getcwd()}'.", file=sys.stderr)
            print("Please specify a target session with `-s <session_id|name|pid>` or run `claude-ipc --list`.", file=sys.stderr)
        return False

    sock_path = session["socket_path"]
    token = session["token"]

    # Wrap in Peer Envelope if not already wrapped
    if not extract_nonce(message):
        nonce = generate_nonce()
        message = format_peer_envelope(
            message=message,
            nonce=nonce,
            sender="Antigravity",
            intent=intent,
            user_context=user_context,
        )

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect(sock_path)

        auth_msg = json.dumps({"type": "auth", "token": token}) + "\n"
        user_msg = json.dumps({"type": "user", "message": {"role": "user", "content": message}}) + "\n"

        s.sendall(auth_msg.encode("utf-8"))
        s.sendall(user_msg.encode("utf-8"))

        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass

        time.sleep(0.3)
        s.close()

        display_name = session["name"] or session["session_id"]
        sess_id = session["host_session_id"] or session["session_id"]
        print(f"[Claude Code IPC] Message successfully delivered to session PID {session['pid']} ({display_name}) [ID: {sess_id}].")
        if session.get("entrypoint") == "claude-desktop":
            print("[Claude Code IPC] Tip: Target is Claude Desktop. If the window is inactive, switch to it to wake message processing.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Claude Code IPC] Failed to send message to socket {sock_path}: {e}", file=sys.stderr)
        return False


def send_file(
    file_path: str | Path,
    target: str | None = None,
    prefix: str | None = None,
    wait: bool = False,
    timeout: float = 300.0,
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> bool | tuple[str | None, str | None]:
    """
    Safely send file content directly to Claude Code without command-line string escaping issues.
    If wait is True, synchronously waits for the response and returns (response_text, verdict).
    Otherwise, sends asynchronously and returns True on success.
    """
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        print(f"[Claude Code IPC] Error: file not found: {file_path}", file=sys.stderr)
        return (None, None) if wait else False
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as e:
        print(f"[Claude Code IPC] Error reading {file_path}: {e}", file=sys.stderr)
        return (None, None) if wait else False

    if prefix:
        content = f"{prefix.strip()}\n\n{content}"

    if wait:
        return send_and_wait(
            content,
            target=target,
            timeout=timeout,
            intent=intent,
            user_context=user_context,
            pick_latest=pick_latest,
            auto_await_new=auto_await_new,
            activate_gui=activate_gui,
            debug=debug,
        )
    return send_message_to_claude(
        content,
        target=target,
        intent=intent,
        user_context=user_context,
        pick_latest=pick_latest,
        auto_await_new=auto_await_new,
        activate_gui=activate_gui,
        debug=debug,
    )


def read_transcript_messages(transcript_path: str | None, last_n: int = 1, only_final: bool = True) -> list[dict]:
    """Parse JSONL transcript and extract assistant response turns."""
    if not transcript_path or not os.path.exists(transcript_path):
        return []

    turns = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message")
                    if msg and isinstance(msg, dict) and msg.get("role") == "assistant":
                        stop_reason = msg.get("stop_reason")
                        if only_final and stop_reason == "tool_use":
                            continue
                        content = msg.get("content")
                        texts = []
                        if isinstance(content, list):
                            for part in content:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                        elif isinstance(content, str):
                            texts.append(content)
                        full_text = "\n".join(texts).strip()
                        if full_text:
                            turns.append({
                                "timestamp": entry.get("timestamp"),
                                "text": full_text,
                                "id": msg.get("id"),
                                "stop_reason": stop_reason,
                                "model": msg.get("model", ""),
                            })
                except Exception:
                    continue
    except Exception as e:
        print(f"[Claude Code IPC] Error reading transcript {transcript_path}: {e}", file=sys.stderr)

    return turns[-last_n:]


def read_latest_feedback(target: str | None = None, n: int = 1, only_final: bool = True, pick_latest: bool = False) -> list[dict]:
    """Fetch the latest assistant turns from the target session."""
    candidates = get_active_sessions(exclude_self=False)
    session, status = match_session(target=target, candidates=candidates, pick_latest=pick_latest) if candidates else (None, "none")
    if not session:
        if status != "ambiguous":
            print(f"[Claude Code IPC] No matching session found for target '{target}'.", file=sys.stderr)
        return []

    transcript_path = session.get("transcript_path")
    if not transcript_path:
        print(f"[Claude Code IPC] No transcript file found for session {session.get('pid')}.", file=sys.stderr)
        return []

    return read_transcript_messages(transcript_path, last_n=n, only_final=only_final)


def send_and_wait(
    message: str,
    target: str | None = None,
    timeout: float = 300.0,
    show_progress: bool = True,
    sender_name: str = "Antigravity",
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Claude Code with request correlation nonce and wait synchronously.
    Uses dual-channel listening:
      1. Direct bi-directional Unix Domain Socket callback (~/.agent-pair/run/reply-<nonce>.sock)
      2. Incremental TailReader on JSONL transcript
    
    Returns:
        tuple[response_text, verdict]
    """
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    session, status = match_session(target=target, candidates=candidates, pick_latest=pick_latest, debug=debug) if candidates else (None, "none")

    if not session:
        if status == "ambiguous":
            return None, None
        resolved_target, _ = resolve_target_session(target)
        if resolved_target is None and auto_await_new:
            try:
                session = await_new_session(timeout=60.0, cwd=os.getcwd(), activate_gui=activate_gui, debug=debug)
            except NeedsNewSessionError:
                return None, "NEEDS_NEW_THREAD"

    if not session:
        if target:
            print(f"[Claude Code IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
        else:
            print(f"[Claude Code IPC] Error: No active session matches current directory '{os.getcwd()}'.", file=sys.stderr)
            print("Please specify a target with `-s <session>` or run `claude-ipc --list`.", file=sys.stderr)
        return None, None

    transcript_path = session.get("transcript_path")
    initial_offset = 0
    if transcript_path and os.path.exists(transcript_path):
        initial_offset = os.path.getsize(transcript_path)

    # Attach unique correlation nonce and start ephemeral reply server
    nonce = generate_nonce()
    reply_server = EphemeralReplyServer(nonce=nonce, timeout=timeout)
    reply_sock_path = None
    try:
        reply_sock_path = reply_server.start()
    except Exception as e:
        if debug:
            print(f"[Claude Code IPC Debug] Could not start ephemeral reply server: {e}", file=sys.stderr)

    full_message = format_peer_envelope(
        message=message,
        nonce=nonce,
        sender=sender_name,
        intent=intent,
        user_context=user_context,
        reply_socket=reply_sock_path,
    )

    try:
        ok = send_message_to_claude(
            full_message,
            target=session["host_session_id"] or str(session["pid"]),
            pick_latest=pick_latest,
            auto_await_new=False,
            activate_gui=False,
            debug=debug,
        )
        if not ok:
            return None, None

        print(f"[Claude Code IPC] Waiting for Claude Code response (Req-ID: {nonce}, timeout: {int(timeout)}s)...", file=sys.stderr)

        start_time = time.time()
        seen_tool_ids = set()
        found_request = False
        warned_idle = False
        warned_delivery = False
        tail_reader = TailReader(transcript_path, start_offset=initial_offset)

        while time.time() - start_time < timeout:
            time.sleep(0.5)

            # Dual-Channel 1: Check ephemeral reply socket first (direct bidirectional callback)
            if reply_server:
                r_resp, r_verdict = reply_server.poll_response(timeout=0.0)
                if r_resp is not None:
                    if debug:
                        print(f"[Claude Code IPC Debug] Response received via ephemeral reply socket: {reply_sock_path}", file=sys.stderr)
                    return r_resp, r_verdict

            if not transcript_path or not os.path.exists(transcript_path):
                transcript_path = find_transcript_path(session.get("cwd", ""), session.get("session_id", ""))
                if not transcript_path or not os.path.exists(transcript_path):
                    continue
                session["transcript_path"] = transcript_path
                tail_reader.set_file(transcript_path, start_offset=0)

            elapsed = time.time() - start_time

            if not found_request and elapsed > 25.0 and not warned_delivery:
                warned_delivery = True
                print(f"[Claude Code IPC] Notice: Request nonce {nonce} has not appeared in transcript after 25s. Peer session may be busy or inactive.", file=sys.stderr)

            if session.get("entrypoint") == "claude-desktop" and not warned_idle and elapsed > 8.0 and not found_request:
                warned_idle = True
                print("[Claude Code IPC] Notice: Claude Desktop has not started processing yet. If window is in background, switch to it to wake it up.", file=sys.stderr)

            for line in tail_reader.read_new_lines():
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    role = msg.get("role")

                    # Step 1: Detect user message with our correlation nonce
                    if role == "user":
                        content_val = msg.get("content", "")
                        raw_content = ""
                        if isinstance(content_val, list):
                            for p in content_val:
                                if isinstance(p, dict) and p.get("type") == "text":
                                    raw_content += p.get("text", "")
                        elif isinstance(content_val, str):
                            raw_content = content_val

                        if nonce in raw_content:
                            found_request = True
                            if debug:
                                print(f"[Claude Code IPC Debug] Found correlated user request with nonce {nonce}", file=sys.stderr)

                    # Step 2: Once correlated request is identified, track assistant tools and final completion
                    elif found_request and role == "assistant":
                        # Live tool tracking
                        if show_progress:
                            for c in msg.get("content", []):
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    tid = c.get("id")
                                    tname = c.get("name")
                                    if tid and tid not in seen_tool_ids:
                                        seen_tool_ids.add(tid)
                                        print(f"[Claude Code IPC] Running tool: {tname}...", file=sys.stderr)

                        # Completion check
                        stop_reason = msg.get("stop_reason")
                        if stop_reason and stop_reason != "tool_use":
                            texts = []
                            for part in msg.get("content", []):
                                if isinstance(part, dict) and part.get("type") == "text":
                                    texts.append(part.get("text", ""))
                            full_text = "\n".join(texts).strip()
                            if full_text:
                                verdict = extract_verdict(full_text)
                                return full_text, verdict

                except Exception as parse_err:
                    if debug:
                        print(f"[Claude Code IPC Debug] Error parsing line: {parse_err}", file=sys.stderr)
                    continue

        print(f"[Claude Code IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
        return None, None
    finally:
        if reply_server:
            reply_server.close()


def list_sessions(candidates: list[dict] | None = None, debug: bool = False):
    """Print all currently active and connectable Claude Code sessions."""
    if candidates is None:
        candidates = get_active_sessions(exclude_self=False, debug=debug)

    if not candidates:
        print("No active Claude Code sessions found.")
        return

    current_cwd = os.getcwd()
    self_ids = get_self_session_identifiers()

    print(f"Found {len(candidates)} active Claude Code session(s):\n")
    print(f"{'PID':<7} {'KIND':<18} {'HOST_SESSION_ID':<40} {'NAME / TITLE'}")
    print("-" * 95)
    for c in candidates:
        hid = c["host_session_id"] or c["session_id"]
        kind_entry = f"{c['kind']} ({c['entrypoint']})" if c['entrypoint'] else c['kind']
        is_self = " (Current Session)" if str(c["pid"]) == str(self_ids["pid"]) else ""
        is_cwd = " [Current Project]" if c.get("cwd") == current_cwd else ""
        print(f"{c['pid']:<7} {kind_entry:<18} {hid:<40} {c['name']}{is_self}{is_cwd}")
        print(f"        └─ Socket:     {c['socket_path']}")
        print(f"        └─ CWD:        {c['cwd']}")
        print(f"        └─ Transcript: {c['transcript_path'] or '(not found)'}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-ipc",
        description="Claude Code IPC Communication Utility\nEnables programmatic messaging and response retrieval with active Claude Code sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # 1. List active sessions:
  claude-ipc --list

  # 3. Unbind project session:
  claude-ipc --unbind

  # 4. Deliver file content and wait for response (Auto-detects current project session!):
  claude-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 5. Deliver report picking latest if multiple sessions exist:
  claude-ipc -w --pick-latest -f docs/reports/REVIEW_REQUEST.md

  # 6. Explicitly send to session with -m:
  claude-ipc -w -m "Please run tests." sessA

  # 7. Read latest assistant response:
  claude-ipc -r
  claude-ipc -r -n 3
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target session ID, title, or PID (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent as message")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target session ID, hostSessionId, title/name, or PID")
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target session ID to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project session binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest session if multiple match current project CWD")
    parser.add_argument("--await-new-session", action="store_true", help="Wait for a new Claude Code session for this project, auto-bind and send")
    parser.add_argument("--no-auto-new", action="store_true", help="Disable automatic wait-for-new-session when no session exists for project")
    parser.add_argument("-l", "--list", action="store_true", help="List all currently active and connectable Claude Code sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from the target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate tool-use turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-g", "--activate-gui", action="store_true", help="Activate Claude Desktop application (and open new tab if creating new session)")
    parser.add_argument("--intent", default="Read-Only Review", help="Peer cooperation intent declaration (default: 'Read-Only Review')")
    parser.add_argument("--user-context", default=None, help="User prompt authorization context to pass in peer envelope")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Claude Code's response")
    parser.add_argument("-t", "--timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.bind_target:
        bfile = save_project_binding("claude", args.bind_target)
        print(f"[Claude Code IPC] Bound Claude Code session '{args.bind_target}' to {bfile}")
        return EXIT_SIGN_OFF

    if args.unbind:
        if unbind_project_binding("claude"):
            print("[Claude Code IPC] Removed Claude binding from .agent-pair/bindings.json.")
        else:
            print("[Claude Code IPC] No active Claude binding found to remove.")
        return EXIT_SIGN_OFF

    if args.list:
        list_sessions(debug=args.debug)
        return EXIT_SIGN_OFF

    target = args.session_target

    if args.read:
        target = target or args.target_pos or args.message
        feedback = read_latest_feedback(target=target, n=args.number, only_final=not args.all_turns, pick_latest=args.pick_latest)
        if not feedback:
            return EXIT_ERROR
        for i, turn in enumerate(feedback):
            if len(feedback) > 1:
                print(f"--- Turn {i+1} [{turn.get('timestamp')}] ---")
            print(turn["text"])
        return EXIT_SIGN_OFF

    if args.await_new_session:
        try:
            new_sess = await_new_session(timeout=60.0, require_tty=False, activate_gui=args.activate_gui, debug=args.debug)
            if not new_sess:
                return EXIT_ERROR
            target = new_sess.get("host_session_id") or new_sess.get("session_id") or str(new_sess["pid"])
        except NeedsNewSessionError:
            return EXIT_NEEDS_NEW_THREAD

    msg_text, resolved_target = resolve_message_payload(
        message_arg=args.message,
        msg_flag=args.msg_flag,
        file_path=args.file_path,
        prefix=args.prefix,
        target_pos=args.target_pos,
        session_flag=target,
    )

    if not msg_text:
        parser.print_help()
        return EXIT_ERROR

    target = resolved_target

    if args.wait:
        resp, verdict = send_and_wait(
            msg_text,
            target=target,
            timeout=args.timeout,
            intent=args.intent,
            user_context=args.user_context,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            activate_gui=args.activate_gui,
            debug=args.debug,
        )
        if verdict == "NEEDS_NEW_THREAD":
            return EXIT_NEEDS_NEW_THREAD
        if resp is not None:
            print("\n=== [Claude Code Response] ===")
            print(resp)
            if verdict == "SIGN_OFF":
                return EXIT_SIGN_OFF
            elif verdict == "CHANGES_REQUESTED":
                return EXIT_CHANGES_REQUESTED
            else:
                print("\n[Claude Code IPC] Note: No standardized VERDICT line (SIGN_OFF / CHANGES_REQUESTED) found.", file=sys.stderr)
                return EXIT_NO_VERDICT
        return EXIT_ERROR

    try:
        ok = send_message_to_claude(
            msg_text,
            target=target,
            intent=args.intent,
            user_context=args.user_context,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            activate_gui=args.activate_gui,
            debug=args.debug,
        )
        return EXIT_SIGN_OFF if ok else EXIT_ERROR
    except NeedsNewSessionError:
        return EXIT_NEEDS_NEW_THREAD


if __name__ == "__main__":
    sys.exit(main())
