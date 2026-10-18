"""
Claude Code IPC Communication Utility.
Enables programmatic two-way messaging, live tool tracking, and feedback retrieval
with active Claude Code desktop / CLI sessions via Unix Domain Sockets (UDS), the duplex
reply channel (reply socket + mailbox), and JSONL transcripts as fallback.
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
    EXIT_QUEUED_UNCONSUMED,
    EXIT_SIGN_OFF,
    EphemeralReplyServer,
    NeedsNewSessionError,
    TailReader,
    build_reply_command,
    check_artifact_stable,
    cleanup_stale_run_files,
    cwd_relation,
    detect_sender,
    extract_nonce,
    filter_project_sessions,
    format_peer_envelope,
    generate_nonce,
    is_pid_alive,
    load_project_bindings,
    open_new_gui_session,
    print_progress_frame,
    resolve_message_payload,
    save_project_binding,
    settle_duplex_reply,
    unbind_project_binding,
    warn_inline_rich_text,
)

DEFAULT_AWAIT_TIMEOUT = 120.0


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
            updated_at_ms = data.get("updatedAt")

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
                "status": data.get("status", ""),
                "peer_features": data.get("peerFeatures") or [],
                # Prefer Claude's own activity timestamp over the metadata file's mtime
                "mtime": (updated_at_ms / 1000.0) if isinstance(updated_at_ms, (int, float)) and updated_at_ms > 0 else json_file.stat().st_mtime,
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

        matched = None
        # 1. Exact match on host_session_id
        for c in candidates:
            if c["host_session_id"] == target_str:
                matched = c
                break
        # 2. Exact match on session_id
        if not matched:
            for c in candidates:
                if c["session_id"] == target_str:
                    matched = c
                    break
        # 3. Exact match on PID
        if not matched:
            for c in candidates:
                if str(c["pid"]) == target_str:
                    matched = c
                    break
        # 4. Exact match on name (supports titles containing slashes like "UI/UX 评审" or "A/B 测试")
        if not matched:
            for c in candidates:
                if c["name"] == target_str:
                    matched = c
                    break

        # R3 Path Parameter Guard: only intercept if no exact match and value looks like a filesystem path
        if not matched and source_kind == "cli_flag":
            is_path_like = (
                target_str.startswith(("/", "~", "./", "../"))
                or os.path.isdir(os.path.expanduser(target_str))
            )
            if is_path_like:
                print(f"\n[Claude Code IPC] Error: Target session '{target_str}' looks like a filesystem path.", file=sys.stderr)
                print("👉 `-s / --session` requires a session ID, hostSessionId, PID, or session title.", file=sys.stderr)
                print("   To target a project directory, `cd` into that directory or configure project aliases in `.agent-pair/config.json`.\n", file=sys.stderr)
                return None, "invalid_target"

        # 5. Prefix match on host_session_id (>= 6 chars)
        if not matched and len(target_str) >= 6:
            for c in candidates:
                if c["host_session_id"].startswith(target_str):
                    matched = c
                    break
        # 6. Prefix match on session_id (>= 6 chars)
        if not matched and len(target_str) >= 6:
            for c in candidates:
                if c["session_id"].startswith(target_str):
                    matched = c
                    break
        # 7. Substring match on name (case-insensitive) - with disambiguation
        if not matched:
            name_matches = [c for c in candidates if target_str.lower() in c["name"].lower()]
            if len(name_matches) == 1:
                matched = name_matches[0]
            elif len(name_matches) > 1:
                print(f"\n[Claude Code IPC] Error: Ambiguous target '{target_str}'! Matches multiple session names:", file=sys.stderr)
                for m in name_matches:
                    print(f"  • [{m.get('host_session_id') or m.get('session_id')}] \"{m.get('name')}\" (PID: {m.get('pid')})", file=sys.stderr)
                print("Please specify an exact session ID or PID with `-s`.\n", file=sys.stderr)
                return None, "ambiguous"

        if matched:
            current_cwd = os.getcwd()
            relation = cwd_relation(matched.get("cwd"), current_cwd)
            # R2: CWD Affinity Check on bindings_file and env_var
            if source_kind in ("bindings_file", "env_var"):
                if relation is None:
                    src_label = ".agent-pair/bindings.json" if source_kind == "bindings_file" else "environment variable AGENT_PAIR_CLAUDE_SESSION"
                    print(f"\n[Claude Code IPC] ⚠️ Warning: 忽略 {src_label} 中的失效绑定！", file=sys.stderr)
                    print(f"   绑定的会话 [{target_str}] 运行在 '{matched.get('cwd')}'，与当前工程 '{current_cwd}' 无关。", file=sys.stderr)
                    print("   👉 自动回退到当前工程会话探测逻辑。可运行 `agent-pair-ipc claude --unbind` 清理。\n", file=sys.stderr)
                    # Clear resolved_target so execution falls through to allow_cwd_fallback
                    resolved_target = None
                else:
                    return matched, "matched"
            else:
                # source_kind == "cli_flag"
                if relation is None:
                    print(f"[Claude Code IPC] ⚠️ NOTICE: 跨工程调用警示！目标会话运行在 '{matched.get('cwd')}'，与当前目录 '{current_cwd}' 不一致！", file=sys.stderr)
                return matched, "matched"

        if resolved_target:
            if source_kind == "bindings_file":
                print(f"\n[Claude Code IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
                print("👉 The bound session may have ended or been closed.", file=sys.stderr)
                print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)
            return None, "none"

    if not allow_cwd_fallback:
        return None, "none"

    # Match by current project: exact CWD first, then a session running at a parent directory
    current_cwd = os.getcwd()
    cwd_matches = filter_project_sessions(candidates, current_cwd)
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


def _session_id_set(sessions: list[dict]) -> set[str]:
    ids = set()
    for s in sessions:
        if s.get("host_session_id"):
            ids.add(s["host_session_id"])
        if s.get("session_id"):
            ids.add(s["session_id"])
        if s.get("pid"):
            ids.add(str(s["pid"]))
    return ids


def await_new_session(
    timeout: float = DEFAULT_AWAIT_TIMEOUT,
    cwd: str | None = None,
    require_tty: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> dict | None:
    """
    "GUI-native creation, IPC attaches afterwards":
    watch for a new Claude Code session (Desktop tab or `claude` in a terminal) for this project.
    - Snapshots pre-existing sessions so older sessions are never misidentified.
    - Accepts a session at the project directory or a parent of it (symlink-resolved).
    - Reports new sessions opened in another folder instead of silently ignoring them.
    - In non-interactive environments (non-TTY) with require_tty, exits immediately instead of blocking.
    When detected, automatically binds hostSessionId/sessionId (never raw PID) to .agent-pair/bindings.json.
    """
    target_cwd = cwd or os.getcwd()
    if require_tty and not sys.stderr.isatty():
        print(f"[Claude Code IPC] Notice: No existing Claude Code session found for project '{target_cwd}'.", file=sys.stderr)
        print("[Claude Code IPC] Cannot interactively wait for new session in non-interactive environment (non-TTY).", file=sys.stderr)
        print("👉 Open the project in Claude Desktop (File > New Session) or run `claude` in a terminal, then retry;", file=sys.stderr)
        print("   or run with `--await-new-session [--await-timeout N]` to wait explicitly, or `-s <session_id>`.", file=sys.stderr)
        raise NeedsNewSessionError("Cannot await new session in non-TTY environment")

    # Snapshot BEFORE asking the GUI to create a session, so the new one is never part of it
    initial_ids = _session_id_set(get_active_sessions(exclude_self=True, debug=debug))
    watch_start = time.time()

    if activate_gui:
        print("[Claude Code IPC] Activating Claude Desktop and requesting File > New Session...", file=sys.stderr)
        if open_new_gui_session("claude"):
            print("[Claude Code IPC] New session requested in Claude Desktop. Pick this project's folder if prompted.", file=sys.stderr)
        else:
            print("[Claude Code IPC] Could not drive Claude Desktop automatically (needs macOS Accessibility permission). Please open a new session manually.", file=sys.stderr)

    print(f"[Claude Code IPC] 当前工程尚未绑定活跃会话。正在等待新会话: '{target_cwd}'", file=sys.stderr)
    print("[Claude Code IPC] 请在 Claude Desktop 中新建会话 (File > New Session / Cmd+N) 并选择本工程目录，或在终端运行 `claude`。", file=sys.stderr)

    reported_foreign: set[str] = set()
    tty = sys.stderr.isatty()
    while time.time() - watch_start < timeout:
        time.sleep(1.0)
        elapsed = int(time.time() - watch_start)
        if tty:
            print(f"\r[Claude Code IPC] [侦测中 {elapsed:02d}s / {int(timeout)}s] 等待新会话诞生...", end="", file=sys.stderr)

        for s in get_active_sessions(exclude_self=True, debug=debug):
            sid = s.get("host_session_id") or s.get("session_id")
            pid_str = str(s.get("pid"))
            if (sid and sid in initial_ids) or (pid_str in initial_ids):
                continue
            if cwd_relation(s.get("cwd"), target_cwd) is None:
                key = sid or pid_str
                if key not in reported_foreign:
                    reported_foreign.add(key)
                    if tty:
                        print("", file=sys.stderr)
                    print(f"[Claude Code IPC] Ignoring new session \"{s.get('name')}\" [{key}] opened in another folder: {s.get('cwd')}", file=sys.stderr)
                    print(f"   If it is meant for this project, bind it explicitly: claude-ipc --bind {key}", file=sys.stderr)
                continue
            # Only bind stable host_session_id or session_id, never raw PID
            bind_id = s.get("host_session_id") or s.get("session_id")
            if bind_id:
                save_project_binding("claude", bind_id, cwd=target_cwd)
                if tty:
                    print("", file=sys.stderr)
                print(f"[Claude Code IPC] 监听到新会话诞生: PID {s.get('pid')} [{bind_id}] -> 自动完成持久绑定！", file=sys.stderr)
            return s

    if tty:
        print("", file=sys.stderr)
    print(f"[Claude Code IPC] Timed out waiting for Claude Code session after {int(timeout)}s.", file=sys.stderr)
    return None


def _report_missing_session(target: str | None) -> None:
    if target:
        print(f"[Claude Code IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
    else:
        print(f"[Claude Code IPC] Error: No active session matches current directory '{os.getcwd()}'.", file=sys.stderr)
        print("Please specify a target session with `-s <session_id|name|pid>` or run `claude-ipc --list`.", file=sys.stderr)


def _resolve_or_await_session(
    target: str | None,
    pick_latest: bool,
    auto_await_new: bool,
    activate_gui: bool,
    await_timeout: float,
    debug: bool,
) -> tuple[dict | None, str]:
    """Shared session resolution for send / send-and-wait. Raises NeedsNewSessionError in non-TTY."""
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    session, status = match_session(target=target, candidates=candidates, pick_latest=pick_latest, debug=debug) if candidates else (None, "none")
    if session or status in ("ambiguous", "invalid_target"):
        return session, status
    resolved_target, _ = resolve_target_session(target)
    if resolved_target is None and auto_await_new:
        session = await_new_session(timeout=await_timeout, cwd=os.getcwd(), activate_gui=activate_gui, debug=debug)
        return session, ("matched" if session else "none")
    return None, status


def send_message_to_claude(
    message: str,
    target: str | None = None,
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    expected_artifact: str | None = None,
    reply_socket: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    sender: str | None = None,
    await_timeout: float = DEFAULT_AWAIT_TIMEOUT,
    debug: bool = False,
) -> bool:
    """
    Send message to target Claude Code session via Unix Domain Socket.
    Messages not yet enveloped are wrapped in a Peer Envelope that tells the peer how to answer
    with `agent-pair-ipc reply --to <nonce>`; a reply sent while nobody waits lands in the mailbox.
    Attaches handshake metadata (ipc_version, request_id, caller, expected_artifact, callback_socket).
    """
    session, status = _resolve_or_await_session(target, pick_latest, auto_await_new, activate_gui, await_timeout, debug)
    if not session:
        if status not in ("ambiguous", "invalid_target"):
            _report_missing_session(target)
        return False

    sock_path = session["socket_path"]
    token = session["token"]

    current_cwd = os.getcwd()
    rel = cwd_relation(session.get("cwd"), current_cwd)

    # Wrap in Peer Envelope if not already wrapped
    nonce = extract_nonce(message)
    if not nonce:
        nonce = generate_nonce()
        message = format_peer_envelope(
            message=message,
            nonce=nonce,
            sender=sender or detect_sender(),
            intent=intent,
            user_context=user_context,
            reply_socket=reply_socket,
            reply_command=build_reply_command(),
            expected_artifact=expected_artifact,
            caller_cwd=current_cwd,
            target_cwd=session.get("cwd"),
            cwd_relation_type=rel,
        )

    # Standardized Handshake Metadata
    caller_name = sender or detect_sender()
    metadata: dict[str, str] = {
        "ipc_version": "2.1",
        "request_id": nonce,
        "caller": caller_name,
    }
    if rel is None and session.get("cwd"):
        metadata["cross_project"] = "true"
        metadata["caller_cwd"] = current_cwd
        metadata["target_cwd"] = str(session.get("cwd"))
    if rel == "linked" and session.get("cwd"):
        metadata["project_relation"] = "linked"
    if expected_artifact:
        metadata["expected_artifact"] = str(expected_artifact)
    if reply_socket:
        metadata["callback_socket"] = str(reply_socket)

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect(sock_path)

        auth_msg = json.dumps({"type": "auth", "token": token}) + "\n"
        user_msg = json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": message,
                "metadata": metadata,
            }
        }) + "\n"

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
        print(f"[Claude Code IPC] Message successfully delivered to session PID {session['pid']} ({display_name}) [ID: {sess_id}] (Req-ID: {nonce}).")
        if expected_artifact:
            print(f"[Claude Code IPC] Handshake: Expected artifact registered -> {expected_artifact}", file=sys.stderr)
        if session.get("status") == "busy":
            print("[Claude Code IPC] Note: Target session is busy; the message will be processed after its current turn.", file=sys.stderr)
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
    fast_fail_timeout: float = 45.0,
    expect_file: str | Path | None = None,
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    sender: str | None = None,
    await_timeout: float = DEFAULT_AWAIT_TIMEOUT,
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

    common_kwargs = dict(
        target=target,
        intent=intent,
        user_context=user_context,
        pick_latest=pick_latest,
        auto_await_new=auto_await_new,
        activate_gui=activate_gui,
        sender=sender,
        await_timeout=await_timeout,
        debug=debug,
    )
    if wait:
        return send_and_wait(
            content,
            timeout=timeout,
            fast_fail_timeout=fast_fail_timeout,
            expect_file=expect_file,
            **common_kwargs
        )
    return send_message_to_claude(
        content,
        expected_artifact=str(expect_file) if expect_file else None,
        **common_kwargs
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
        if status not in ("ambiguous", "invalid_target"):
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
    fast_fail_timeout: float = 45.0,
    expect_file: str | Path | None = None,
    show_progress: bool = True,
    sender_name: str | None = None,
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    activate_gui: bool = False,
    sender: str | None = None,
    await_timeout: float = DEFAULT_AWAIT_TIMEOUT,
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Claude Code with request correlation nonce and wait synchronously.
    Uses multi-channel listening:
      1. Duplex reply channel: ephemeral socket ~/.agent-pair/run/reply-<nonce>.sock (+ mailbox),
         which the peer answers via `agent-pair-ipc reply --to <nonce>` (advertised in the envelope)
      2. Fast-fail checks: target PID liveness & socket liveness
      3. Expected artifact auto-detection: checks if `expect_file` has been written and stabilized
      4. Incremental TailReader on the JSONL transcript with unconsumed fast-fail protection

    Returns:
        tuple[response_text, verdict]
    """
    sender = sender or sender_name or detect_sender()
    try:
        session, status = _resolve_or_await_session(target, pick_latest, auto_await_new, activate_gui, await_timeout, debug)
    except NeedsNewSessionError:
        return None, "NEEDS_NEW_THREAD"

    if not session:
        if status not in ("ambiguous", "invalid_target"):
            _report_missing_session(target)
        return None, None

    cleanup_stale_run_files()

    expect_path: Path | None = None
    initial_mtime = 0.0
    initial_size = 0
    if expect_file:
        expect_path = Path(expect_file).expanduser().resolve()
        if expect_path.is_file():
            try:
                st = expect_path.stat()
                initial_mtime = st.st_mtime
                initial_size = st.st_size
            except Exception:
                pass

    transcript_path = session.get("transcript_path")
    initial_offset = 0
    if transcript_path and os.path.exists(transcript_path):
        try:
            initial_offset = os.path.getsize(transcript_path)
        except OSError:
            initial_offset = 0

    # Attach unique correlation nonce and start ephemeral reply server
    nonce = generate_nonce()
    reply_server = EphemeralReplyServer(
        nonce=nonce,
        timeout=timeout,
        on_progress=print_progress_frame("Claude Code IPC") if show_progress else None,
    )
    reply_sock_path = None
    try:
        reply_sock_path = reply_server.start()
    except Exception as e:
        if debug:
            print(f"[Claude Code IPC Debug] Could not start ephemeral reply server (mailbox still active): {e}", file=sys.stderr)

    current_cwd = os.getcwd()
    rel = cwd_relation(session.get("cwd"), current_cwd)

    full_message = format_peer_envelope(
        message=message,
        nonce=nonce,
        sender=sender,
        intent=intent,
        user_context=user_context,
        reply_socket=reply_sock_path,
        reply_command=build_reply_command(),
        expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
        caller_cwd=current_cwd,
        target_cwd=session.get("cwd"),
        cwd_relation_type=rel,
    )

    try:
        ok = send_message_to_claude(
            full_message,
            target=session["host_session_id"] or str(session["pid"]),
            expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
            reply_socket=reply_sock_path,
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
        is_enqueued = False
        found_request = False
        warned_busy = False
        reply_cmd_seen = False
        warned_idle = False
        warned_delivery = False
        tail_reader = TailReader(transcript_path, start_offset=initial_offset)
        reply_marker = f"reply --to {nonce}"

        while time.time() - start_time < timeout:
            # Channel 1: duplex reply socket / mailbox (also serves as the loop's sleep)
            r_resp, r_verdict = reply_server.poll_response(timeout=0.5)
            if r_resp is not None:
                if debug:
                    print(f"[Claude Code IPC Debug] Response received via duplex channel ({reply_server.channel}).", file=sys.stderr)
                return r_resp, r_verdict

            # Channel 2: Fast-fail checks (PID and socket liveness)
            pid = session.get("pid")
            if pid and not is_pid_alive(pid):
                print(f"\n[Claude Code IPC] Fast-fail: Target Claude process PID {pid} is no longer running.", file=sys.stderr)
                return None, "PROCESS_TERMINATED"

            sock_path = session.get("socket_path")
            if sock_path and not os.path.exists(sock_path):
                print(f"\n[Claude Code IPC] Fast-fail: Target Claude socket disappeared: {sock_path}", file=sys.stderr)
                return None, "SOCKET_DISAPPEARED"

            # Channel 3: Expected deliverable auto-detection
            if expect_path:
                ready, art_content, art_verdict = check_artifact_stable(
                    expect_path,
                    initial_mtime=initial_mtime,
                    initial_size=initial_size,
                    min_stable_seconds=3.0,
                    require_verdict=True,
                )
                if ready and art_content:
                    print(f"\n[Claude Code IPC] Detected completed deliverable with verdict [{art_verdict}]: {expect_path} ({len(art_content)} bytes).", file=sys.stderr)
                    return art_content, art_verdict

            # Channel 4: Incremental JSONL transcript reading
            if not transcript_path or not os.path.exists(transcript_path):
                transcript_path = find_transcript_path(session.get("cwd", ""), session.get("session_id", ""))
                if transcript_path and os.path.exists(transcript_path):
                    session["transcript_path"] = transcript_path
                    tail_reader.set_file(transcript_path, start_offset=0)

            if transcript_path and os.path.exists(transcript_path):
                for line in tail_reader.read_new_lines():
                    # Confirmation of enqueue: Claude Code records queue-operation/enqueue immediately on socket write
                    if not is_enqueued and nonce in line:
                        is_enqueued = True
                        if debug:
                            print(f"[Claude Code IPC Debug] Request confirmed enqueued (nonce {nonce} in transcript).", file=sys.stderr)

                    try:
                        entry = json.loads(line)
                        msg = entry.get("message", {})
                        role = msg.get("role")

                        # Step 1: Detect user message with correlation nonce (turn actually began)
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
                                is_enqueued = True
                                found_request = True
                                if debug:
                                    print(f"[Claude Code IPC Debug] Correlated user turn started for nonce {nonce}", file=sys.stderr)

                        # Step 2: Once correlated request is identified, track assistant tools and final completion
                        elif found_request and role == "assistant":
                            for c in msg.get("content", []):
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    if reply_marker in json.dumps(c.get("input", {}), ensure_ascii=False):
                                        reply_cmd_seen = True
                                    tid = c.get("id")
                                    tname = c.get("name")
                                    if show_progress and tid and tid not in seen_tool_ids:
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
                                    text, verdict, channel = settle_duplex_reply(reply_server, full_text, reply_cmd_seen)
                                    if debug:
                                        print(f"[Claude Code IPC Debug] Final answer taken from {channel}.", file=sys.stderr)
                                    return text, verdict

                    except Exception as parse_err:
                        if debug:
                            print(f"[Claude Code IPC Debug] Error parsing line: {parse_err}", file=sys.stderr)
                        continue

            elapsed = time.time() - start_time

            # Fast-fail: Request was not even enqueued in transcript after fast_fail_timeout
            if not is_enqueued and fast_fail_timeout > 0 and elapsed > fast_fail_timeout:
                print(f"\n[Claude Code IPC] Notice: Message was delivered to socket but not enqueued within {int(fast_fail_timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
                print(f"[Claude Code IPC] Target session \"{session.get('name')}\" [PID: {session['pid']}] may be suspended in background or inactive.", file=sys.stderr)
                print("[Claude Code IPC] NOTE: If the session was asleep, switch to Claude Desktop to wake it up.", file=sys.stderr)
                print(f"[Claude Code IPC] Action: Check later using `agent-pair-ipc inbox {nonce}` or `claude-ipc -r`.\n", file=sys.stderr)
                return None, "QUEUED_UNCONSUMED"

            # Inform if enqueued and waiting for busy previous turn
            if is_enqueued and not found_request and not warned_busy and elapsed > 10.0:
                warned_busy = True
                print(f"[Claude Code IPC] Notice: Message is safely queued in session. Peer is busy processing previous turn; waiting for turn to start...", file=sys.stderr)

            if not is_enqueued and elapsed > 25.0 and not warned_delivery:
                warned_delivery = True
                print(f"[Claude Code IPC] Notice: Request nonce {nonce} has not appeared in transcript after 25s. Peer session may be busy or inactive.", file=sys.stderr)

            if session.get("entrypoint") == "claude-desktop" and not warned_idle and elapsed > 8.0 and not is_enqueued:
                warned_idle = True
                print("[Claude Code IPC] Notice: Claude Desktop has not started processing yet. If window is in background, switch to it to wake it up.", file=sys.stderr)

        print(f"[Claude Code IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
        print(f"[Claude Code IPC] A late reply will be kept in the mailbox: agent-pair-ipc inbox {nonce}", file=sys.stderr)
        return None, None
    finally:
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
    print(f"{'PID':<7} {'KIND':<30} {'STATUS':<7} {'HOST_SESSION_ID':<40} {'NAME / TITLE'}")
    print("-" * 115)
    for c in candidates:
        hid = c["host_session_id"] or c["session_id"]
        kind_entry = f"{c['kind']} ({c['entrypoint']})" if c['entrypoint'] else c['kind']
        is_self = " (Current Session)" if str(c["pid"]) == str(self_ids["pid"]) else ""
        relation = cwd_relation(c.get("cwd"), current_cwd)
        is_cwd = " [Current Project]" if relation == "exact" else (" [Linked Project]" if relation == "linked" else (" [Parent of CWD]" if relation == "ancestor" else ""))
        status = c.get("status") or "-"
        print(f"{c['pid']:<7} {kind_entry:<30} {status:<7} {hid:<40} {c['name']}{is_self}{is_cwd}")
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

  # 2. Unbind project session:
  claude-ipc --unbind

  # 3. Deliver file content and wait for response (Auto-detects current project session!):
  claude-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 4. Rich text without shell escaping: quoted heredoc piped through stdin
  claude-ipc -w -f - <<'AP_EOF'
  Review `proxy.yaml` rule AND,((DOMAIN-SUFFIX,example.com)) and $VARS verbatim.
  AP_EOF

  # 5. Deliver report picking latest if multiple sessions exist:
  claude-ipc -w --pick-latest -f docs/reports/REVIEW_REQUEST.md

  # 6. Explicitly send to session with -m:
  claude-ipc -w -m "Please run tests." sessA

  # 7. Read latest assistant response:
  claude-ipc -r
  claude-ipc -r -n 3

  # 8. No session yet: open one in Claude Desktop (File > New Session) and auto-bind it
  claude-ipc --await-new-session -g -w -f docs/reports/REVIEW_REQUEST.md

  # 9. The peer answers over the duplex channel (command is printed in the envelope):
  agent-pair-ipc reply --to req_0123456789ab -f answer.md
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target session ID, title, or PID (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent as message ('-' reads stdin; recommended for rich text)")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target session ID, hostSessionId, title/name, or PID")
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target session ID to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project session binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest session if multiple match current project CWD")
    parser.add_argument("--await-new-session", "--await-new-thread", dest="await_new_session", action="store_true", help="Wait for a new Claude Code session for this project, auto-bind and send (works in non-TTY)")
    parser.add_argument("--await-timeout", type=float, default=DEFAULT_AWAIT_TIMEOUT, help=f"Seconds to wait for a new session (default: {int(DEFAULT_AWAIT_TIMEOUT)})")
    parser.add_argument("--no-auto-new", action="store_true", help="Disable automatic wait-for-new-session when no session exists for project")
    parser.add_argument("-l", "--list", action="store_true", help="List all currently active and connectable Claude Code sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from the target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate tool-use turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-g", "--activate-gui", action="store_true", help="Activate Claude Desktop and trigger File > New Session when waiting for a new session")
    parser.add_argument("--intent", default="Read-Only Review", help="Peer cooperation intent declaration (default: 'Read-Only Review')")
    parser.add_argument("--user-context", default=None, help="User prompt authorization context to pass in peer envelope")
    parser.add_argument("--sender", default=None, help="Sender name in the peer envelope (default: $AGENT_PAIR_SENDER or auto-detected)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Claude Code response")
    parser.add_argument("-t", "--timeout", "--wait-timeout", dest="timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--expect-file", "--output-file", dest="expect_file", default=None, help="Expected deliverable file path. Auto-detects completion when file settles with verdict.")
    parser.add_argument("--fast-fail-timeout", type=float, default=45.0, help="Fast fail timeout in seconds if request is not even enqueued (default: 45.0, 0 to disable)")
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
            new_sess = await_new_session(timeout=args.await_timeout, require_tty=False, activate_gui=args.activate_gui, debug=args.debug)
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
    if args.msg_flag is not None:
        warn_inline_rich_text(msg_text)

    is_wait = args.wait or bool(args.expect_file)

    if is_wait:
        resp, verdict = send_and_wait(
            msg_text,
            target=target,
            timeout=args.timeout,
            fast_fail_timeout=args.fast_fail_timeout,
            expect_file=args.expect_file,
            intent=args.intent,
            user_context=args.user_context,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            activate_gui=args.activate_gui,
            sender=args.sender,
            await_timeout=args.await_timeout,
            debug=args.debug,
        )
        if verdict == "QUEUED_UNCONSUMED":
            return EXIT_QUEUED_UNCONSUMED
        if verdict == "NEEDS_NEW_THREAD":
            return EXIT_NEEDS_NEW_THREAD
        if verdict in ("PROCESS_TERMINATED", "SOCKET_DISAPPEARED"):
            return EXIT_ERROR
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
            expected_artifact=args.expect_file,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            activate_gui=args.activate_gui,
            sender=args.sender,
            await_timeout=args.await_timeout,
            debug=args.debug,
        )
        return EXIT_SIGN_OFF if ok else EXIT_ERROR
    except NeedsNewSessionError:
        return EXIT_NEEDS_NEW_THREAD


if __name__ == "__main__":
    sys.exit(main())
