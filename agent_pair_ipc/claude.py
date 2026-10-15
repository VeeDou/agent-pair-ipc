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
    EXIT_NO_VERDICT,
    EXIT_SIGN_OFF,
    TailReader,
    attach_request_header,
    extract_nonce,
    extract_verdict,
    generate_nonce,
    load_project_bindings,
    resolve_message_payload,
    save_project_binding,
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
    debug: bool = False,
) -> dict | None:
    """
    Match a session from candidates by target query:
    - host_session_id (exact or prefix >= 6 chars)
    - session_id (exact or prefix >= 6 chars)
    - pid (exact string)
    - name (exact or case-insensitive substring)
    
    If target is not given, matches session by current project CWD.
    If no session matches CWD, does NOT blindly guess: returns None to prevent misdelivery.
    """
    if candidates is None:
        candidates = get_active_sessions(exclude_self=True, debug=debug)

    if not candidates:
        return None

    resolved_target, _ = resolve_target_session(target)

    if resolved_target:
        target_str = str(resolved_target).strip()
        # 1. Exact match on host_session_id
        for c in candidates:
            if c["host_session_id"] == target_str:
                return c
        # 2. Exact match on session_id
        for c in candidates:
            if c["session_id"] == target_str:
                return c
        # 3. Exact match on PID
        for c in candidates:
            if str(c["pid"]) == target_str:
                return c
        # 4. Exact match on name
        for c in candidates:
            if c["name"] == target_str:
                return c
        # 5. Prefix match on host_session_id (>= 6 chars)
        if len(target_str) >= 6:
            for c in candidates:
                if c["host_session_id"].startswith(target_str):
                    return c
        # 6. Prefix match on session_id (>= 6 chars)
        if len(target_str) >= 6:
            for c in candidates:
                if c["session_id"].startswith(target_str):
                    return c
        # 7. Substring match on name (case-insensitive)
        for c in candidates:
            if target_str.lower() in c["name"].lower():
                return c
        return None

    if not allow_cwd_fallback:
        return None

    # Match by current working directory (CWD)
    current_cwd = os.getcwd()
    cwd_matches = [c for c in candidates if c.get("cwd") == current_cwd]
    if cwd_matches:
        # Prioritize interactive sessions in current CWD
        interactive = [c for c in cwd_matches if c["kind"] == "interactive"]
        return interactive[0] if interactive else cwd_matches[0]

    # No session found matching current CWD: Do NOT guess!
    return None


def send_message_to_claude(message: str, target: str | None = None, debug: bool = False) -> bool:
    """Send message to target Claude Code session via Unix Domain Socket."""
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    if not candidates:
        print("[Claude Code IPC] Error: No connectable Claude Code sessions found.", file=sys.stderr)
        return False

    session = match_session(target=target, candidates=candidates, debug=debug)
    if not session:
        if target:
            print(f"[Claude Code IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
        else:
            print(f"[Claude Code IPC] Error: No active session matches current directory '{os.getcwd()}'.", file=sys.stderr)
            print("Please specify a target session with `-s <session_id|name|pid>` or run `claude-ipc --list`.", file=sys.stderr)
        return False

    sock_path = session["socket_path"]
    token = session["token"]

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


def read_latest_feedback(target: str | None = None, n: int = 1, only_final: bool = True) -> list[dict]:
    """Fetch the latest assistant turns from the target session."""
    candidates = get_active_sessions(exclude_self=False)
    session = match_session(target=target, candidates=candidates)
    if not session:
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
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Claude Code with request correlation nonce and wait synchronously.
    Uses incremental tailing (seeking to offset) to eliminate massive multi-megabyte re-reads.
    
    Returns:
        tuple[response_text, verdict]
    """
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    if not candidates:
        print("[Claude Code IPC] Error: No connectable Claude Code sessions found.", file=sys.stderr)
        return None, None

    session = match_session(target=target, candidates=candidates, debug=debug)
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

    # Attach unique correlation nonce and sender identity
    nonce = generate_nonce()
    full_message = attach_request_header(message, nonce=nonce, sender=sender_name)

    ok = send_message_to_claude(full_message, target=session["host_session_id"] or str(session["pid"]), debug=debug)
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

        try:
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

        except Exception as e:
            if debug:
                print(f"[Claude Code IPC Debug] Error in polling loop: {e}", file=sys.stderr)

    print(f"[Claude Code IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
    return None, None


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

  # 2. Deliver file content and wait for response (Auto-detects current project session!):
  claude-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 3. Explicitly send to session with -m:
  claude-ipc -w -m "Please run tests." sessA

  # 4. Read latest assistant response:
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
    parser.add_argument("-l", "--list", action="store_true", help="List all currently active and connectable Claude Code sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from the target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate tool-use turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
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

    if args.list:
        list_sessions(debug=args.debug)
        return EXIT_SIGN_OFF

    target = args.session_target

    if args.read:
        target = target or args.target_pos or args.message
        feedback = read_latest_feedback(target=target, n=args.number, only_final=not args.all_turns)
        if not feedback:
            return EXIT_ERROR
        for i, turn in enumerate(feedback):
            if len(feedback) > 1:
                print(f"--- Turn {i+1} [{turn.get('timestamp')}] ---")
            print(turn["text"])
        return EXIT_SIGN_OFF

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
        resp, verdict = send_and_wait(msg_text, target=target, timeout=args.timeout, debug=args.debug)
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

    ok = send_message_to_claude(msg_text, target=target, debug=args.debug)
    return EXIT_SIGN_OFF if ok else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
