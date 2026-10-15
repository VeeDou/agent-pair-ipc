"""
Codex IPC Communication Utility.
Enables programmatic two-way messaging, real-time activity tracking, and review result retrieval
with active Codex desktop sessions via session transcript monitoring and `codex queue`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import datetime
from pathlib import Path

from agent_pair_ipc.common import (
    EXIT_CHANGES_REQUESTED,
    EXIT_ERROR,
    EXIT_NEEDS_NEW_THREAD,
    EXIT_NO_VERDICT,
    EXIT_QUEUED_UNCONSUMED,
    EXIT_SIGN_OFF,
    NeedsNewSessionError,
    TailReader,
    attach_request_header,
    extract_verdict,
    generate_nonce,
    load_project_bindings,
    resolve_message_payload,
    save_project_binding,
    unbind_project_binding,
)


def get_session_meta_from_file(rollout_path: Path | str) -> dict:
    """Read line 1 of rollout JSONL to extract session_meta payload."""
    try:
        with open(rollout_path, "r", encoding="utf-8") as f:
            line = f.readline()
            if line:
                d = json.loads(line)
                if d.get("type") == "session_meta":
                    return d.get("payload", {})
    except Exception:
        pass
    return {}


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
                        "source": None,
                        "originator": None,
                        "mtime": 0.0,
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
    """Populate jsonl_path, cwd, source, originator, and mtime for a session."""
    if session.get("jsonl_path") is None:
        p = find_session_file(session["id"])
        session["jsonl_path"] = str(p) if p else ""
        if p and p.is_file():
            try:
                session["mtime"] = p.stat().st_mtime
                meta = get_session_meta_from_file(p)
                session["cwd"] = meta.get("cwd")
                session["source"] = meta.get("source")
                session["originator"] = meta.get("originator")
            except Exception:
                pass
    return session


def resolve_target_session(target: str | None = None, cwd: str | None = None) -> tuple[str | None, str]:
    """
    Resolve target session through explicit binding priority chain:
      1. Explicit CLI argument / flag (`-s <id_or_title>`)
      2. Environment variable `AGENT_PAIR_CODEX_THREAD`
      3. Project binding file `.agent-pair/bindings.json`
      4. Auto-match fallback (returns None, "auto")
    
    Returns:
        tuple[resolved_target_string_or_none, resolution_source]
    """
    if target and target.strip():
        return target.strip(), "cli_flag"

    env_target = os.environ.get("AGENT_PAIR_CODEX_THREAD")
    if env_target and env_target.strip():
        return env_target.strip(), "env_var"

    bindings = load_project_bindings(cwd)
    bound = bindings.get("codex") or bindings.get("codex_thread")
    if bound and bound.strip():
        return bound.strip(), "bindings_file"

    return None, "auto"


def match_session(
    target: str | None = None,
    candidates: list[dict] | None = None,
    allow_cwd_fallback: bool = True,
    pick_latest: bool = False,
    debug: bool = False,
) -> tuple[dict | None, str]:
    """
    Match a session from candidates by target query:
    - ID exact match
    - ID prefix match (>= 6 chars)
    - Name exact or case-insensitive substring match
    
    If no explicit target is bound, filters by current CWD:
    - Automatically excludes `source == 'exec'` (CLI sessions)
    - If multiple sessions match CWD:
        - If `pick_latest` is True: picks the one with most recent rollout mtime
        - If `pick_latest` is False: REFUSES to guess, prints candidate list, returns (None, "ambiguous")
    - Warns if target session was updated > 30 minutes ago

    Returns:
        tuple[session_dict | None, status_str] where status_str is 'matched', 'ambiguous', or 'none'.
    """
    resolved_target, source_kind = resolve_target_session(target)

    if candidates is None:
        candidates = get_all_sessions(limit=40, debug=debug)

    if not candidates:
        return None, "none"

    if resolved_target:
        target_str = str(resolved_target).strip()
        for c in candidates:
            if c["id"] == target_str:
                return enrich_session_info(c), "matched"
        for c in candidates:
            if c["name"] == target_str:
                return enrich_session_info(c), "matched"
        if len(target_str) >= 6:
            for c in candidates:
                if c["id"].startswith(target_str):
                    return enrich_session_info(c), "matched"
        for c in candidates:
            if target_str.lower() in c["name"].lower():
                return enrich_session_info(c), "matched"
        if len(target_str) >= 8:
            direct_file = find_session_file(target_str)
            if direct_file:
                c = {"id": target_str, "name": target_str, "updated_at": "", "jsonl_path": str(direct_file), "cwd": None}
                return enrich_session_info(c), "matched"

        if source_kind == "bindings_file":
            print(f"\n[Codex IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
            print("👉 The bound session may have been deleted or archived.", file=sys.stderr)
            print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)

        return None, "none"

    if not allow_cwd_fallback:
        return None, "none"

    # Auto-match by current working directory (CWD)
    current_cwd = os.getcwd()
    cwd_matches = []
    for c in candidates:
        enrich_session_info(c)
        # Rule 1: Exclude CLI offline sessions
        if c.get("source") == "exec":
            continue
        if c.get("cwd") == current_cwd:
            cwd_matches.append(c)

    if not cwd_matches:
        return None, "none"

    if len(cwd_matches) == 1:
        chosen = cwd_matches[0]
    elif pick_latest:
        cwd_matches.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
        chosen = cwd_matches[0]
        print(f"[Codex IPC] Notice: Multiple sessions found for this project. Using latest due to `--pick-latest`: \"{chosen.get('name')}\" [{chosen['id'][:8]}...]", file=sys.stderr)
    else:
        # Strict disambiguation: refuse to guess!
        print(f"\n[Codex IPC] Error: Ambiguous sessions! Multiple active Codex sessions found for directory '{current_cwd}':", file=sys.stderr)
        for m in cwd_matches:
            mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m.get('mtime', 0.0))) if m.get('mtime') else m.get('updated_at', 'unknown')
            origin = m.get("originator") or "Codex Desktop"
            print(f"  • [{m['id']}] \"{m.get('name', 'Untitled')}\" (Updated: {mtime_str}, {origin})", file=sys.stderr)
        print("\nPlease specify a target session with `-s <id_or_title>`, set AGENT_PAIR_CODEX_THREAD,", file=sys.stderr)
        print("save to .agent-pair/bindings.json, or pass `--pick-latest` to confirm.\n", file=sys.stderr)
        return None, "ambiguous"

    # Staleness check
    mtime = chosen.get("mtime", 0.0)
    if mtime and (time.time() - mtime > 1800.0):
        mins = int((time.time() - mtime) / 60)
        print(f"[Codex IPC] Warning: Target session \"{chosen.get('name')}\" [{chosen['id'][:8]}...] was last updated {mins} minutes ago and may be sleeping in background.", file=sys.stderr)

    return chosen, "matched"


def await_new_thread(
    timeout: float = 60.0,
    cwd: str | None = None,
    require_tty: bool = True,
    debug: bool = False,
) -> dict | None:
    """
    Watch ~/.codex/sessions for a newly created session rollout matching current CWD and source != 'exec'.
    Snapshots pre-existing sessions to guarantee older or touched sessions are never misidentified.
    In non-interactive environments (non-TTY), exits immediately instead of blocking.
    When detected, automatically binds it to .agent-pair/bindings.json and returns the session info.
    """
    target_cwd = cwd or os.getcwd()
    if require_tty and not sys.stderr.isatty():
        print(f"[Codex IPC] Notice: No existing Codex session found for project '{target_cwd}'.", file=sys.stderr)
        print("[Codex IPC] Cannot interactively wait for new thread in non-interactive environment (non-TTY).", file=sys.stderr)
        print("👉 Please create a new thread in Codex Desktop, or run with -s <session_id>.", file=sys.stderr)
        raise NeedsNewSessionError("Cannot await new thread in non-TTY environment")

    watch_start = time.time()
    sessions_root = Path.home() / ".codex" / "sessions"
    print(f"[Codex IPC] Waiting for new thread in Codex Desktop for '{target_cwd}'... (Click 'New Thread' in Desktop app)", file=sys.stderr)

    today_dt = datetime.date.today()
    yesterday_dt = today_dt - datetime.timedelta(days=1)
    date_subdirs = [
        sessions_root / today_dt.strftime("%Y/%m/%d"),
        sessions_root / yesterday_dt.strftime("%Y/%m/%d"),
    ]

    existing_files = set()
    initial_session_ids = set()
    for d in date_subdirs:
        if d.is_dir():
            for rfile in d.glob("rollout-*.jsonl"):
                existing_files.add(str(rfile.resolve()))
                meta = get_session_meta_from_file(rfile)
                sid = meta.get("id")
                if sid:
                    initial_session_ids.add(sid)

    while time.time() - watch_start < timeout:
        time.sleep(0.5)
        for d in date_subdirs:
            if not d.is_dir():
                continue
            try:
                for rfile in d.glob("rollout-*.jsonl"):
                    rpath = str(rfile.resolve())
                    if rpath in existing_files:
                        continue
                    st = rfile.stat()
                    if st.st_ctime < watch_start - 0.5 and st.st_mtime < watch_start - 0.5:
                        continue
                    meta = get_session_meta_from_file(rfile)
                    sid = meta.get("id")
                    if not sid or sid in initial_session_ids:
                        continue
                    if meta.get("cwd") == target_cwd and meta.get("source") != "exec":
                        save_project_binding("codex", sid, cwd=target_cwd)
                        print(f"[Codex IPC] Detected new thread [{sid}]! Automatically bound to .agent-pair/bindings.json.", file=sys.stderr)
                        return {
                            "id": sid,
                            "name": "New Thread",
                            "updated_at": "",
                            "jsonl_path": str(rfile),
                            "cwd": target_cwd,
                            "source": meta.get("source"),
                            "originator": meta.get("originator"),
                            "mtime": st.st_mtime,
                        }
            except Exception as e:
                if debug:
                    print(f"[Codex IPC Debug] Error polling new thread: {e}", file=sys.stderr)
                continue

    print(f"[Codex IPC] Timed out waiting for new thread after {int(timeout)}s.", file=sys.stderr)
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
                                    "phase": phase,
                                })

                except Exception:
                    continue
    except Exception as e:
        print(f"[Codex IPC] Error reading transcript {jsonl_path}: {e}", file=sys.stderr)

    return turns[-last_n:]


def read_latest_feedback(target: str | None = None, n: int = 1, only_final: bool = True, pick_latest: bool = False) -> list[dict]:
    """Fetch the latest assistant turns from target session."""
    session, status = match_session(target=target, pick_latest=pick_latest)
    if not session:
        if status != "ambiguous":
            print(f"[Codex IPC] No matching session found for target '{target}'.", file=sys.stderr)
        return []

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path:
        print(f"[Codex IPC] No transcript file found for session {session.get('id')}.", file=sys.stderr)
        return []

    return read_transcript_messages(jsonl_path, last_n=n, only_final=only_final)


def send_message_to_codex(
    message: str,
    target: str | None = None,
    pick_latest: bool = False,
    auto_await_new: bool = True,
    debug: bool = False,
) -> bool:
    """Send message to Codex session via `codex queue`."""
    session, status = match_session(target=target, pick_latest=pick_latest, debug=debug)
    if not session:
        if status == "ambiguous":
            return False
        resolved_target, _ = resolve_target_session(target)
        if resolved_target is None and auto_await_new:
            session = await_new_thread(timeout=60.0, cwd=os.getcwd(), debug=debug)

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

        print(f"[Codex IPC] Message successfully queued to session \"{display_name}\" [{session_id}].")
        return True
    except Exception as e:
        print(f"[Codex IPC] Failed to execute `codex queue`: {e}", file=sys.stderr)
        return False


def send_and_wait(
    message: str,
    target: str | None = None,
    timeout: float = 300.0,
    fast_fail_timeout: float = 15.0,
    show_progress: bool = True,
    sender_name: str = "Antigravity",
    pick_latest: bool = False,
    auto_await_new: bool = True,
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Codex with correlation nonce and wait synchronously.
    Uses TailReader to eliminate multi-megabyte re-reads.
    Implements fast fail: if nonce is not consumed after fast_fail_timeout,
    exits with QUEUED_UNCONSUMED to prevent hanging on inactive desktop tabs.
    
    Returns:
        tuple[response_text, verdict]
    """
    session, status = match_session(target=target, pick_latest=pick_latest, debug=debug)
    if not session:
        if status == "ambiguous":
            return None, None
        resolved_target, _ = resolve_target_session(target)
        if resolved_target is None and auto_await_new:
            try:
                session = await_new_thread(timeout=60.0, cwd=os.getcwd(), debug=debug)
            except NeedsNewSessionError:
                return None, "NEEDS_NEW_THREAD"

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

    ok = send_message_to_codex(full_message, target=session["id"], pick_latest=pick_latest, auto_await_new=False, debug=debug)
    if not ok:
        return None, None

    print(f"[Codex IPC] Waiting for Codex response (Req-ID: {nonce}, timeout: {int(timeout)}s)...", file=sys.stderr)
    start_time = time.time()
    seen_tool_ids = set()
    found_request = False
    tail_reader = TailReader(jsonl_path, start_offset=initial_offset)

    while time.time() - start_time < timeout:
        time.sleep(0.5)
        if not jsonl_path or not os.path.exists(jsonl_path):
            p = find_session_file(session["id"])
            if p:
                jsonl_path = str(p)
                session["jsonl_path"] = jsonl_path
                tail_reader.set_file(jsonl_path, start_offset=0)
            else:
                continue

        elapsed = time.time() - start_time

        # Fast Fail: Nonce not consumed after fast_fail_timeout
        if not found_request and elapsed > fast_fail_timeout:
            print(f"\n[Codex IPC] Notice: Message is queued but was not consumed within {int(fast_fail_timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
            print(f"[Codex IPC] Target session \"{session.get('name')}\" [{session['id']}] might be inactive or busy executing another task.", file=sys.stderr)
            print("[Codex IPC] NOTE: The message remains safely in Codex's internal queue; do NOT resend duplicate messages.", file=sys.stderr)
            print(f"[Codex IPC] Action: Switch to Codex Desktop and activate session \"{session.get('name')}\", or run `codex-ipc -r` later to check for completion.\n", file=sys.stderr)
            return None, "QUEUED_UNCONSUMED"

        new_lines = tail_reader.read_new_lines()
        for line in new_lines:
            try:
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

    print(f"[Codex IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
    return None, None


def list_sessions(debug: bool = False):
    """List recent active Codex sessions with duplicates removed and metadata displayed."""
    sessions = get_all_sessions(limit=30, debug=debug)
    if not sessions:
        print("No Codex sessions found in ~/.codex/session_index.jsonl.")
        return

    current_cwd = os.getcwd()
    bound_id = load_project_bindings().get("codex")

    print(f"Found {len(sessions)} recent Codex session(s):\n")
    print(f"{'ID':<38} {'UPDATED_AT':<26} {'NAME / TITLE'}")
    print("-" * 95)
    for s in sessions:
        enrich_session_info(s)
        tags = []
        if s.get("cwd") == current_cwd:
            tags.append("Current Project")
        if bound_id and (s["id"] == bound_id or s["id"].startswith(bound_id)):
            tags.append("Bound Target")
        tag_str = f" [{', '.join(tags)}]" if tags else ""

        print(f"{s['id']:<38} {s.get('updated_at', ''):<26} {s.get('name', '')}{tag_str}")
        if s.get("originator"):
            print(f"        └─ Client:     {s['originator']} (source: {s.get('source')})")
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

  # 2. Bind current project to specific thread:
  codex-ipc --bind 01a13f4a-64e9-7243-a6a2-f2dc809f7b02

  # 3. Unbind project session:
  codex-ipc --unbind

  # 4. Wait for new thread in Desktop app, auto-bind and deliver report:
  codex-ipc --await-new-thread -w -f docs/reports/REVIEW_REQUEST.md

  # 5. Deliver report file and wait for review completion:
  codex-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 6. Deliver report picking latest if multiple sessions exist:
  codex-ipc -w --pick-latest -f docs/reports/REVIEW_REQUEST.md
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target session ID or title (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent as message")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target session ID or title")
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target session ID to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project session binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest session if multiple match current project CWD")
    parser.add_argument("--await-new-thread", action="store_true", help="Wait for a new thread to be created in Codex Desktop, auto-bind and send")
    parser.add_argument("-l", "--list", action="store_true", help="List recent Codex sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Codex response")
    parser.add_argument("-t", "--timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--fast-fail", type=float, default=15.0, help="Fast fail timeout in seconds if nonce not consumed (default: 15.0)")
    parser.add_argument("--no-auto-new", action="store_true", help="Disable automatic wait-for-new-thread when no session exists for project")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.bind_target:
        bfile = save_project_binding("codex", args.bind_target)
        print(f"[Codex IPC] Bound Codex session '{args.bind_target}' to {bfile}")
        return EXIT_SIGN_OFF

    if args.unbind:
        if unbind_project_binding("codex"):
            print("[Codex IPC] Removed Codex binding from .agent-pair/bindings.json.")
        else:
            print("[Codex IPC] No active Codex binding found to remove.")
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

    if args.await_new_thread:
        try:
            new_sess = await_new_thread(timeout=60.0, require_tty=False, debug=args.debug)
            if not new_sess:
                return EXIT_ERROR
            target = new_sess["id"]
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
            fast_fail_timeout=args.fast_fail,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            debug=args.debug,
        )
        if verdict == "QUEUED_UNCONSUMED":
            return EXIT_QUEUED_UNCONSUMED
        if verdict == "NEEDS_NEW_THREAD":
            return EXIT_NEEDS_NEW_THREAD
        if resp is not None:
            print("\n=== [Codex Response] ===")
            print(resp)
            if verdict == "SIGN_OFF":
                return EXIT_SIGN_OFF
            elif verdict == "CHANGES_REQUESTED":
                return EXIT_CHANGES_REQUESTED
            else:
                print("\n[Codex IPC] Note: No standardized VERDICT line (SIGN_OFF / CHANGES_REQUESTED) found.", file=sys.stderr)
                return EXIT_NO_VERDICT
        return EXIT_ERROR

    try:
        ok = send_message_to_codex(
            msg_text,
            target=target,
            pick_latest=args.pick_latest,
            auto_await_new=not args.no_auto_new,
            debug=args.debug,
        )
        return EXIT_SIGN_OFF if ok else EXIT_ERROR
    except NeedsNewSessionError:
        return EXIT_NEEDS_NEW_THREAD


if __name__ == "__main__":
    sys.exit(main())
