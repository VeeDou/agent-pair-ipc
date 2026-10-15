"""
Codex IPC Communication Utility.
Enables programmatic two-way messaging, real-time activity tracking, and review result retrieval
with active Codex desktop / CLI sessions via session transcript monitoring and `codex queue`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_pair_ipc.common import (
    attach_request_header,
    extract_nonce,
    extract_verdict,
    generate_nonce,
    resolve_message_payload,
)


def find_session_file(session_id: str) -> Path | None:
    """Find the JSONL rollout file corresponding to a given session ID."""
    if not session_id:
        return None
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.is_dir():
        return None

    matches = list(sessions_root.glob(f"**/rollout-*{session_id}*.jsonl"))
    if matches:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0]

    return None


def get_all_sessions(limit: int = 50, debug: bool = False) -> list[dict]:
    """Scan ~/.codex/session_index.jsonl to discover recent unique Codex sessions."""
    index_file = Path.home() / ".codex" / "session_index.jsonl"
    if not index_file.is_file():
        return []

    sessions = []
    seen_ids = set()
    try:
        with open(index_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                sid = data.get("id")
                name = data.get("thread_name", "")
                updated_at = data.get("updated_at", "")
                if sid and sid not in seen_ids:
                    seen_ids.add(sid)
                    sessions.append({
                        "id": sid,
                        "name": name,
                        "updated_at": updated_at,
                        "jsonl_path": None,
                        "cwd": None,
                    })
            except Exception as e:
                if debug:
                    print(f"[Codex IPC Debug] Error parsing line in index: {e}", file=sys.stderr)
                continue
            if len(sessions) >= limit:
                break
    except Exception as e:
        if debug:
            print(f"[Codex IPC Debug] Warning reading session_index.jsonl: {e}", file=sys.stderr)

    return sessions


def enrich_session_info(session: dict) -> dict:
    """Populate jsonl_path and cwd for a session if not already done."""
    if session.get("jsonl_path") is None:
        p = find_session_file(session["id"])
        session["jsonl_path"] = str(p) if p else ""
        if p and p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for _ in range(25):
                        line = f.readline()
                        if not line:
                            break
                        d = json.loads(line)
                        if d.get("type") == "session_meta":
                            session["cwd"] = d.get("payload", {}).get("cwd")
                            break
            except Exception:
                pass
    return session


def match_session(
    target: str | None = None,
    candidates: list[dict] | None = None,
    allow_cwd_fallback: bool = True,
    debug: bool = False,
) -> dict | None:
    """
    Match a session from candidates by target query:
    - ID exact match
    - ID prefix match (>= 6 chars)
    - Name exact or case-insensitive substring match
    
    If target is not given, prioritizes session matching current CWD.
    If no session matches CWD, does NOT guess an unrelated session: returns None.
    """
    if candidates is None:
        candidates = get_all_sessions(limit=35, debug=debug)

    if not candidates:
        return None

    if target:
        target_str = str(target).strip()
        for c in candidates:
            if c["id"] == target_str:
                return enrich_session_info(c)
        for c in candidates:
            if c["name"] == target_str:
                return enrich_session_info(c)
        if len(target_str) >= 6:
            for c in candidates:
                if c["id"].startswith(target_str):
                    return enrich_session_info(c)
        for c in candidates:
            if target_str.lower() in c["name"].lower():
                return enrich_session_info(c)
        if len(target_str) >= 8:
            direct_file = find_session_file(target_str)
            if direct_file:
                c = {"id": target_str, "name": target_str, "updated_at": "", "jsonl_path": str(direct_file), "cwd": None}
                return enrich_session_info(c)
        return None

    if not allow_cwd_fallback:
        return None

    current_cwd = os.getcwd()
    for c in candidates:
        enrich_session_info(c)
        if c.get("cwd") == current_cwd:
            return c

    # Does NOT guess an arbitrary project's session!
    return None


def read_transcript_messages(jsonl_path: str | None, last_n: int = 1, only_final: bool = True) -> list[dict]:
    """Parse JSONL transcript and extract assistant response turns."""
    if not jsonl_path or not os.path.exists(jsonl_path):
        return []

    turns = []
    seen_ids = set()

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    etype = entry.get("type")

                    if etype == "event_msg" and entry.get("payload", {}).get("type") == "task_complete":
                        payload = entry["payload"]
                        last_msg = payload.get("last_agent_message")
                        turn_id = payload.get("turn_id")
                        key = f"tc_{turn_id}" if turn_id else last_msg
                        if last_msg and key not in seen_ids:
                            seen_ids.add(key)
                            turns.append({
                                "timestamp": entry.get("timestamp"),
                                "turn_id": turn_id,
                                "text": last_msg.strip(),
                                "type": "task_complete",
                            })

                    elif etype == "response_item" and entry.get("payload", {}).get("type") == "message":
                        payload = entry["payload"]
                        if payload.get("role") == "assistant":
                            phase = payload.get("phase", "")
                            if only_final and phase != "final_answer":
                                continue
                            content = payload.get("content", [])
                            texts = [c.get("text", "") for c in content if c.get("type") in ("output_text", "Text")]
                            full_text = "\n".join(texts).strip()
                            mid = payload.get("id") or full_text
                            if full_text and mid not in seen_ids:
                                seen_ids.add(mid)
                                turns.append({
                                    "timestamp": entry.get("timestamp"),
                                    "id": mid,
                                    "text": full_text,
                                    "type": "message",
                                })

                except Exception:
                    continue
    except Exception as e:
        print(f"[Codex IPC] Error reading transcript {jsonl_path}: {e}", file=sys.stderr)

    return turns[-last_n:]


def read_latest_feedback(target: str | None = None, n: int = 1, only_final: bool = True) -> list[dict]:
    """Fetch the latest assistant turns from target session."""
    session = match_session(target=target)
    if not session:
        print(f"[Codex IPC] No matching session found for target '{target}'.", file=sys.stderr)
        return []

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path:
        print(f"[Codex IPC] No transcript file found for session {session.get('id')}.", file=sys.stderr)
        return []

    return read_transcript_messages(jsonl_path, last_n=n, only_final=only_final)


def send_message_to_codex(message: str, target: str | None = None, debug: bool = False) -> bool:
    """Send message to Codex session via `codex queue`."""
    session = match_session(target=target, debug=debug)
    if not session:
        if target:
            print(f"[Codex IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
        else:
            print(f"[Codex IPC] Error: No active Codex session matches current working directory '{os.getcwd()}'.", file=sys.stderr)
            print("Please specify a target with `-s <session_id>` or run `codex-ipc --list`.", file=sys.stderr)
        return False

    session_id = session["id"]
    display_name = session.get("name") or session_id

    try:
        cmd = ["codex", "queue", "--thread", session_id, "--message", message]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[Codex IPC] `codex queue` failed (exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
            return False

        print(f"[Codex IPC] Message successfully queued to session '{display_name}' [{session_id}].")
        return True
    except Exception as e:
        print(f"[Codex IPC] Failed to execute `codex queue`: {e}", file=sys.stderr)
        return False


def send_and_wait(
    message: str,
    target: str | None = None,
    timeout: float = 300.0,
    show_progress: bool = True,
    sender_name: str = "Antigravity",
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Codex with correlation nonce and wait synchronously.
    Uses incremental tailing (seeking to offset) to eliminate multi-megabyte re-reads.
    
    Returns:
        tuple[response_text, verdict]
    """
    session = match_session(target=target, debug=debug)
    if not session:
        if target:
            print(f"[Codex IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
        else:
            print(f"[Codex IPC] Error: No active Codex session matches current working directory '{os.getcwd()}'.", file=sys.stderr)
            print("Please specify a target with `-s <session_id>` or run `codex-ipc --list`.", file=sys.stderr)
        return None, None

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path or not os.path.exists(jsonl_path):
        jsonl_path = str(find_session_file(session["id"]) or "")
        session["jsonl_path"] = jsonl_path

    initial_offset = 0
    if jsonl_path and os.path.exists(jsonl_path):
        initial_offset = os.path.getsize(jsonl_path)

    nonce = generate_nonce()
    full_message = attach_request_header(message, nonce=nonce, sender=sender_name)

    ok = send_message_to_codex(full_message, target=session["id"], debug=debug)
    if not ok:
        return None, None

    print(f"[Codex IPC] Waiting for Codex response (Req-ID: {nonce}, timeout: {int(timeout)}s)...", file=sys.stderr)
    start_time = time.time()
    current_offset = initial_offset
    seen_tool_ids = set()
    found_request = False
    warned_delivery = False
    byte_buffer = b""

    while time.time() - start_time < timeout:
        time.sleep(0.6)
        if not jsonl_path or not os.path.exists(jsonl_path):
            p = find_session_file(session["id"])
            if p:
                jsonl_path = str(p)
                session["jsonl_path"] = jsonl_path
            else:
                continue

        try:
            file_size = os.path.getsize(jsonl_path)
            elapsed = time.time() - start_time

            if not found_request and elapsed > 25.0 and not warned_delivery:
                warned_delivery = True
                print(f"[Codex IPC] Notice: Request nonce {nonce} has not appeared in rollout after 25s. Peer session may be busy or inactive.", file=sys.stderr)

            if file_size <= current_offset:
                continue

            with open(jsonl_path, "rb") as f:
                f.seek(current_offset)
                chunk = f.read()
                if not chunk:
                    continue

                current_offset += len(chunk)
                combined = byte_buffer + chunk
                raw_lines = combined.split(b"\n")
                byte_buffer = raw_lines[-1]

                for rline in raw_lines[:-1]:
                    rline = rline.strip()
                    if not rline:
                        continue
                    try:
                        line = rline.decode("utf-8", errors="replace")
                        entry = json.loads(line)
                        etype = entry.get("type")
                        payload = entry.get("payload", {})

                        # Step 1: Detect user queue/input message containing nonce
                        if nonce in line:
                            found_request = True
                            if debug:
                                print(f"[Codex IPC Debug] Detected correlated request with nonce {nonce}", file=sys.stderr)

                        # Step 2: Track activity once request is queued
                        if found_request and show_progress:
                            # Tool call
                            ptype = payload.get("type")
                            if etype == "response_item" and ptype in ("custom_tool_call", "function_call"):
                                call_id = payload.get("call_id") or payload.get("id")
                                tool_name = payload.get("name", "tool")
                                if call_id and call_id not in seen_tool_ids:
                                    seen_tool_ids.add(call_id)
                                    print(f"[Codex IPC Activity] Running tool: {tool_name}...", file=sys.stderr)
                            elif payload.get("type") == "item_completed":
                                item = payload.get("item", {})
                                if item.get("type") == "Reasoning":
                                    rid = item.get("id")
                                    if rid and rid not in seen_tool_ids:
                                        seen_tool_ids.add(rid)
                                        print("[Codex IPC Activity] Reasoning in progress...", file=sys.stderr)

                        # Step 3: Detect completion
                        if found_request:
                            if etype == "event_msg" and payload.get("type") == "task_complete":
                                last_msg = payload.get("last_agent_message")
                                if last_msg:
                                    verdict = extract_verdict(last_msg)
                                    return last_msg.strip(), verdict

                            elif etype == "response_item" and payload.get("type") == "message":
                                if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                                    content = payload.get("content", [])
                                    texts = [c.get("text", "") for c in content if c.get("type") in ("output_text", "Text")]
                                    full_text = "\n".join(texts).strip()
                                    if full_text:
                                        verdict = extract_verdict(full_text)
                                        return full_text, verdict

                    except Exception as parse_err:
                        if debug:
                            print(f"[Codex IPC Debug] Parse error: {parse_err}", file=sys.stderr)
                        continue

        except Exception as e:
            if debug:
                print(f"[Codex IPC Debug] Loop error: {e}", file=sys.stderr)

    print(f"[Codex IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
    return None, None


def list_sessions(debug: bool = False):
    """List recent active Codex sessions with duplicates removed."""
    sessions = get_all_sessions(limit=30, debug=debug)
    if not sessions:
        print("No Codex sessions found in ~/.codex/session_index.jsonl.")
        return

    current_cwd = os.getcwd()
    print(f"Found {len(sessions)} recent Codex session(s):\n")
    print(f"{'ID':<38} {'UPDATED_AT':<26} {'NAME / TITLE'}")
    print("-" * 95)
    for s in sessions:
        enrich_session_info(s)
        is_cwd = " [Current Project]" if s.get("cwd") == current_cwd else ""
        print(f"{s['id']:<38} {s.get('updated_at', ''):<26} {s.get('name', '')}{is_cwd}")
        if s.get("cwd"):
            print(f"        └─ CWD:        {s['cwd']}")
        if s.get("jsonl_path"):
            print(f"        └─ Transcript: {s['jsonl_path']}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-ipc",
        description="Codex IPC Communication Utility\nEnables programmatic messaging, response retrieval, and sync wait with Codex sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # 1. List recent Codex sessions:
  codex-ipc --list

  # 2. Read latest assistant response:
  codex-ipc -r
  codex-ipc -r -s 01234567-89ab-cdef-0123-456789abcdef

  # 3. Deliver report file and wait for review completion:
  codex-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 4. Explicit send with -m:
  codex-ipc -w -m "Please run the benchmark suite." my-thread-title
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target session ID or title (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent as message")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target session ID or title")
    parser.add_argument("-l", "--list", action="store_true", help="List recent Codex sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Codex response")
    parser.add_argument("-t", "--timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        list_sessions(debug=args.debug)
        return 0

    target = args.session_target

    if args.read:
        target = target or args.target_pos or args.message
        feedback = read_latest_feedback(target=target, n=args.number, only_final=not args.all_turns)
        if not feedback:
            return 1
        for i, turn in enumerate(feedback):
            if len(feedback) > 1:
                print(f"--- Turn {i+1} [{turn.get('timestamp')}] ---")
            print(turn["text"])
        return 0

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
        return 1

    target = resolved_target

    if args.wait:
        resp, verdict = send_and_wait(msg_text, target=target, timeout=args.timeout, debug=args.debug)
        if resp is not None:
            print("\n=== [Codex Response] ===")
            print(resp)
            if verdict == "SIGN_OFF":
                return 0
            elif verdict == "CHANGES_REQUESTED":
                return 2
            else:
                print("\n[Codex IPC] Note: No standardized VERDICT line (SIGN_OFF / CHANGES_REQUESTED) found.", file=sys.stderr)
                return 3
        return 1

    ok = send_message_to_codex(msg_text, target=target, debug=args.debug)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
