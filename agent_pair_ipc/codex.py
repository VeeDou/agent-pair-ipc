"""
Codex IPC Communication Utility.
Enables programmatic two-way messaging, real-time activity tracking, and review result retrieval
with active Codex desktop sessions via `codex queue`, the duplex reply channel
(reply socket + mailbox), and session transcript monitoring as fallback.
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
    load_project_bindings,
    open_new_gui_session,
    print_progress_frame,
    resolve_message_payload,
    save_project_binding,
    settle_duplex_reply,
    spill_enveloped_message,
    unbind_project_binding,
    warn_inline_rich_text,
)

DEFAULT_AWAIT_TIMEOUT = 120.0


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

        matched = None
        for c in candidates:
            if c["id"] == target_str:
                matched = enrich_session_info(c)
                break
        if not matched:
            for c in candidates:
                if c["name"] == target_str:
                    matched = enrich_session_info(c)
                    break

        # R3 Path Parameter Guard: only intercept if no exact match and value looks like a filesystem path
        if not matched and source_kind == "cli_flag":
            is_path_like = (
                target_str.startswith(("/", "~", "./", "../"))
                or os.path.isdir(os.path.expanduser(target_str))
            )
            if is_path_like:
                print(f"\n[Codex IPC] Error: Target session '{target_str}' looks like a filesystem path.", file=sys.stderr)
                print("👉 `-s / --session` requires a thread ID, title, or rollout ID.", file=sys.stderr)
                print("   To target a project directory, `cd` into that directory or configure project aliases in `.agent-pair/config.json`.\n", file=sys.stderr)
                return None, "invalid_target"
        if not matched and len(target_str) >= 6:
            for c in candidates:
                if c["id"].startswith(target_str):
                    matched = enrich_session_info(c)
                    break
        if not matched:
            name_matches = [c for c in candidates if target_str.lower() in c["name"].lower()]
            if len(name_matches) == 1:
                matched = enrich_session_info(name_matches[0])
            elif len(name_matches) > 1:
                print(f"\n[Codex IPC] Error: Ambiguous target '{target_str}'! Matches multiple thread names:", file=sys.stderr)
                for m in name_matches:
                    print(f"  • [{m.get('id')}] \"{m.get('name')}\"", file=sys.stderr)
                print("Please specify an exact thread ID with `-s`.\n", file=sys.stderr)
                return None, "ambiguous"

        if not matched and len(target_str) >= 8:
            direct_file = find_session_file(target_str)
            if direct_file:
                c = {"id": target_str, "name": target_str, "updated_at": "", "jsonl_path": str(direct_file), "cwd": None}
                matched = enrich_session_info(c)

        if matched:
            current_cwd = os.getcwd()
            relation = cwd_relation(matched.get("cwd"), current_cwd)
            # R2: CWD Affinity Check on bindings_file and env_var
            if source_kind in ("bindings_file", "env_var"):
                if relation is None:
                    src_label = ".agent-pair/bindings.json" if source_kind == "bindings_file" else "environment variable AGENT_PAIR_CODEX_THREAD"
                    print(f"\n[Codex IPC] ⚠️ Warning: 忽略 {src_label} 中的失效绑定！", file=sys.stderr)
                    print(f"   绑定的会话 [{target_str}] 运行在 '{matched.get('cwd')}'，与当前工程 '{current_cwd}' 无关。", file=sys.stderr)
                    print("   👉 自动回退到当前工程会话探测逻辑。可运行 `agent-pair-ipc codex --unbind` 清理。\n", file=sys.stderr)
                    # Clear resolved_target so execution falls through to allow_cwd_fallback
                    resolved_target = None
                else:
                    return matched, "matched"
            else:
                # source_kind == "cli_flag"
                if relation is None:
                    print(f"[Codex IPC] ⚠️ NOTICE: 跨工程调用警示！目标会话运行在 '{matched.get('cwd')}'，与当前目录 '{current_cwd}' 不一致！", file=sys.stderr)
                return matched, "matched"

        if resolved_target:
            if source_kind == "bindings_file":
                print(f"\n[Codex IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
                print("👉 The bound session may have been deleted or archived.", file=sys.stderr)
                print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)
            return None, "none"

    if not allow_cwd_fallback:
        return None, "none"

    # Auto-match by current project: exact CWD first, then a session running at a parent directory
    current_cwd = os.getcwd()
    interactive = []
    for c in candidates:
        enrich_session_info(c)
        # Rule 1: Exclude CLI offline sessions
        if c.get("source") == "exec":
            continue
        interactive.append(c)
    cwd_matches = filter_project_sessions(interactive, current_cwd)

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
    timeout: float = DEFAULT_AWAIT_TIMEOUT,
    cwd: str | None = None,
    require_tty: bool = True,
    activate_gui: bool = False,
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
        print("👉 Create a new thread for this project in the Codex desktop app, then retry;", file=sys.stderr)
        print("   or run with `--await-new-thread [--await-timeout N]` to wait explicitly, or `-s <session_id>`.", file=sys.stderr)
        raise NeedsNewSessionError("Cannot await new thread in non-TTY environment")

    watch_start = time.time()
    sessions_root = Path.home() / ".codex" / "sessions"

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

    # Snapshot is taken BEFORE asking the GUI to create a thread
    if activate_gui:
        print("[Codex IPC] Activating the Codex desktop app and requesting a new thread...", file=sys.stderr)
        if open_new_gui_session("codex"):
            print("[Codex IPC] New thread requested. Select this project's folder if prompted.", file=sys.stderr)
        else:
            print("[Codex IPC] Could not drive the desktop app automatically (needs macOS Accessibility permission). Please create a new thread manually.", file=sys.stderr)

    print(f"[Codex IPC] 当前工程尚未绑定活跃会话。正在等待 Codex 新建会话: '{target_cwd}'", file=sys.stderr)
    print("[Codex IPC] 请在 Codex 桌面端为本工程新建 Thread，检测到后会自动完成绑定。", file=sys.stderr)

    reported_foreign: set[str] = set()
    tty = sys.stderr.isatty()
    while time.time() - watch_start < timeout:
        time.sleep(0.5)
        elapsed = int(time.time() - watch_start)
        if tty:
            print(f"\r[Codex IPC] [侦测中 {elapsed:02d}s / {int(timeout)}s] 等待 Codex 新建会话...", end="", file=sys.stderr)

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
                    if meta.get("source") == "exec":
                        continue
                    rel = cwd_relation(meta.get("cwd"), target_cwd)
                    if rel is None:
                        if sid not in reported_foreign:
                            reported_foreign.add(sid)
                            if tty:
                                print("", file=sys.stderr)
                            print(f"[Codex IPC] Ignoring new thread [{sid}] created in another folder: {meta.get('cwd')}", file=sys.stderr)
                            print(f"   If it is meant for this project, bind it explicitly: codex-ipc --bind {sid}", file=sys.stderr)
                        continue
                    save_project_binding("codex", sid, cwd=target_cwd)
                    if tty:
                        print("", file=sys.stderr)
                    rel_tag = f" ({rel})" if rel != "exact" else ""
                    print(f"[Codex IPC] 监听到新会话诞生: [{sid}]{rel_tag} -> 自动完成持久绑定！", file=sys.stderr)
                    return {
                        "id": sid,
                        "name": "New Thread",
                        "updated_at": "",
                        "jsonl_path": str(rfile),
                        "cwd": meta.get("cwd") or target_cwd,
                        "source": meta.get("source"),
                        "originator": meta.get("originator"),
                        "mtime": st.st_mtime,
                    }
            except Exception as e:
                if debug:
                    print(f"[Codex IPC Debug] Error polling new thread: {e}", file=sys.stderr)
                continue

    if tty:
        print("", file=sys.stderr)
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
        if status not in ("ambiguous", "invalid_target"):
            print(f"[Codex IPC] No matching session found for target '{target}'.", file=sys.stderr)
        return []

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path:
        print(f"[Codex IPC] No transcript file found for session {session.get('id')}.", file=sys.stderr)
        return []

    return read_transcript_messages(jsonl_path, last_n=n, only_final=only_final)


def _report_missing_session(target: str | None) -> None:
    if target:
        print(f"[Codex IPC] Error: No matching session found for target '{target}'.", file=sys.stderr)
    else:
        print(f"[Codex IPC] Error: No active Codex session matches current working directory '{os.getcwd()}'.", file=sys.stderr)
        print("Please specify a target with `-s <session_id>` or run `codex-ipc --list`.", file=sys.stderr)


def _resolve_or_await_session(
    target: str | None,
    pick_latest: bool,
    auto_await_new: bool,
    activate_gui: bool,
    await_timeout: float,
    debug: bool,
) -> tuple[dict | None, str]:
    """Shared session resolution for send / send-and-wait. Raises NeedsNewSessionError in non-TTY."""
    session, status = match_session(target=target, pick_latest=pick_latest, debug=debug)
    if session or status in ("ambiguous", "invalid_target"):
        return session, status
    resolved_target, _ = resolve_target_session(target)
    if resolved_target is None and auto_await_new:
        session = await_new_thread(timeout=await_timeout, cwd=os.getcwd(), activate_gui=activate_gui, debug=debug)
        return session, ("matched" if session else "none")
    return None, status


def send_message_to_codex(
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
    Send message to Codex session via `codex queue`.
    `codex queue` only accepts the text through argv (no shell is involved, so nothing is
    evaluated), but argv is size-limited: oversized bodies are moved to a 0o600 payload file
    and the queued message carries the envelope plus a pointer to that file.
    """
    session, status = _resolve_or_await_session(target, pick_latest, auto_await_new, activate_gui, await_timeout, debug)
    if not session:
        if status not in ("ambiguous", "invalid_target"):
            _report_missing_session(target)
        return False

    session_id = session["id"]
    display_name = session.get("name") or session_id

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

    message, payload_path = spill_enveloped_message(message)
    if payload_path:
        print(f"[Codex IPC] Large payload moved to file-based pipeline: {payload_path}", file=sys.stderr)

    try:
        cmd = ["codex", "queue", "--thread", session_id, "--message", message]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[Codex IPC] `codex queue` failed (exit {result.returncode}): {result.stderr.strip()}", file=sys.stderr)
            return False

        print(f"[Codex IPC] Message successfully queued to session \"{display_name}\" [{session_id}] (Req-ID: {nonce}).")
        if expected_artifact:
            print(f"[Codex IPC] Handshake: Expected artifact registered -> {expected_artifact}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Codex IPC] Failed to execute `codex queue`: {e}", file=sys.stderr)
        return False


def send_file(
    file_path: str | Path,
    target: str | None = None,
    prefix: str | None = None,
    wait: bool = False,
    timeout: float = 300.0,
    fast_fail_timeout: float = 15.0,
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
    Safely send file content directly to Codex without command-line string escaping issues.
    If wait is True, synchronously waits for the response and returns (response_text, verdict).
    Otherwise, queues message asynchronously and returns True on success.
    """
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        print(f"[Codex IPC] Error: file not found: {file_path}", file=sys.stderr)
        return (None, None) if wait else False
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception as e:
        print(f"[Codex IPC] Error reading {file_path}: {e}", file=sys.stderr)
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
    return send_message_to_codex(
        content,
        expected_artifact=str(expect_file) if expect_file else None,
        **common_kwargs
    )


def send_and_wait(
    message: str,
    target: str | None = None,
    timeout: float = 300.0,
    fast_fail_timeout: float = 15.0,
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
    Send message to Codex with correlation nonce and wait synchronously.
    Uses multi-channel listening:
      1. Duplex reply channel: ephemeral socket ~/.agent-pair/run/reply-<nonce>.sock (+ mailbox),
         which the peer answers via `agent-pair-ipc reply --to <nonce>` (advertised in the envelope)
      2. Expected deliverable auto-detection: checks if `expect_file` has been written and stabilized
      3. Incremental TailReader on JSONL rollout transcript with fast-fail detection

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

    jsonl_path = session.get("jsonl_path")
    if not jsonl_path or not os.path.exists(jsonl_path):
        jsonl_path = str(find_session_file(session["id"]) or "")
        session["jsonl_path"] = jsonl_path

    initial_offset = 0
    if jsonl_path and os.path.exists(jsonl_path):
        try:
            initial_offset = os.path.getsize(jsonl_path)
        except OSError:
            initial_offset = 0

    nonce = generate_nonce()
    reply_server = EphemeralReplyServer(
        nonce=nonce,
        timeout=timeout,
        on_progress=print_progress_frame("Codex IPC Activity") if show_progress else None,
    )
    reply_sock_path = None
    try:
        reply_sock_path = reply_server.start()
    except Exception as e:
        if debug:
            print(f"[Codex IPC Debug] Could not start ephemeral reply server (mailbox still active): {e}", file=sys.stderr)

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
        ok = send_message_to_codex(
            full_message,
            target=session["id"],
            expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
            reply_socket=reply_sock_path,
            pick_latest=pick_latest,
            auto_await_new=False,
            activate_gui=False,
            debug=debug,
        )
        if not ok:
            return None, None

        print(f"[Codex IPC] Waiting for Codex response (Req-ID: {nonce}, timeout: {int(timeout)}s)...", file=sys.stderr)
        start_time = time.time()
        seen_tool_ids = set()
        found_request = False
        reply_cmd_seen = False
        tail_reader = TailReader(jsonl_path, start_offset=initial_offset)
        reply_marker = f"reply --to {nonce}"

        while time.time() - start_time < timeout:
            # Channel 1: duplex reply socket / mailbox (also serves as the loop's sleep)
            r_resp, r_verdict = reply_server.poll_response(timeout=0.5)
            if r_resp is not None:
                if debug:
                    print(f"[Codex IPC Debug] Response received via duplex channel ({reply_server.channel}).", file=sys.stderr)
                return r_resp, r_verdict

            # Channel 2: Expected deliverable auto-detection
            if expect_path:
                ready, art_content, art_verdict = check_artifact_stable(
                    expect_path,
                    initial_mtime=initial_mtime,
                    initial_size=initial_size,
                    min_stable_seconds=3.0,
                    require_verdict=True,
                )
                if ready and art_content:
                    print(f"\n[Codex IPC] Detected completed deliverable with verdict [{art_verdict}]: {expect_path} ({len(art_content)} bytes).", file=sys.stderr)
                    return art_content, art_verdict

            # Channel 3: Incremental JSONL transcript reading
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
                print(f"[Codex IPC] Action: Switch to Codex Desktop and activate session \"{session.get('name')}\", then collect the answer later with", file=sys.stderr)
                print(f"           `agent-pair-ipc inbox {nonce}` (duplex reply) or `codex-ipc -r` (transcript).\n", file=sys.stderr)
                return None, "QUEUED_UNCONSUMED"

            new_lines = tail_reader.read_new_lines()
            for line in new_lines:
                try:
                    entry = json.loads(line)
                    etype = entry.get("type")
                    payload = entry.get("payload", {})
                    ptype = payload.get("type")

                    # Step 1: Detect user queue/input message containing nonce
                    if not found_request and nonce in line:
                        found_request = True
                        if debug:
                            print(f"[Codex IPC Debug] Detected correlated request with nonce {nonce}", file=sys.stderr)
                        continue

                    # Step 2: Track activity once request is queued
                    if found_request and etype == "response_item" and ptype in ("custom_tool_call", "function_call", "local_shell_call"):
                        if reply_marker in line:
                            reply_cmd_seen = True
                        call_id = payload.get("call_id") or payload.get("id")
                        tool_name = payload.get("name", "tool")
                        if show_progress and call_id and call_id not in seen_tool_ids:
                            seen_tool_ids.add(call_id)
                            print(f"[Codex IPC Activity] Running tool: {tool_name}...", file=sys.stderr)
                    elif found_request and show_progress and ptype == "item_completed":
                        item = payload.get("item", {})
                        if item.get("type") == "Reasoning":
                            rid = item.get("id")
                            if rid and rid not in seen_tool_ids:
                                seen_tool_ids.add(rid)
                                print("[Codex IPC Activity] Reasoning in progress...", file=sys.stderr)

                    # Step 3: Detect completion
                    if found_request:
                        final_text = None
                        if etype == "event_msg" and ptype == "task_complete":
                            final_text = (payload.get("last_agent_message") or "").strip()
                        elif etype == "response_item" and ptype == "message":
                            if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                                content = payload.get("content", [])
                                texts = [c.get("text", "") for c in content if c.get("type") in ("output_text", "Text")]
                                final_text = "\n".join(texts).strip()
                        if final_text:
                            text, verdict, channel = settle_duplex_reply(reply_server, final_text, reply_cmd_seen)
                            if debug:
                                print(f"[Codex IPC Debug] Final answer taken from {channel}.", file=sys.stderr)
                            return text, verdict

                except Exception as parse_err:
                    if debug:
                        print(f"[Codex IPC Debug] Parse error: {parse_err}", file=sys.stderr)
                    continue

        print(f"[Codex IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
        print(f"[Codex IPC] A late reply will be kept in the mailbox: agent-pair-ipc inbox {nonce}", file=sys.stderr)
        return None, None
    finally:
        reply_server.close()


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
        relation = cwd_relation(s.get("cwd"), current_cwd)
        if relation == "exact":
            tags.append("Current Project")
        elif relation == "linked":
            tags.append("Linked Project")
        elif relation == "ancestor":
            tags.append("Parent of CWD")
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
  codex-ipc --bind 01234567-89ab-cdef-0123-456789abcdef

  # 3. Unbind project session:
  codex-ipc --unbind

  # 4. Wait for new thread in Desktop app, auto-bind and deliver report:
  codex-ipc --await-new-thread -w -f docs/reports/REVIEW_REQUEST.md

  # 5. Deliver report file and wait for review completion:
  codex-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 6. Deliver report picking latest if multiple sessions exist:
  codex-ipc -w --pick-latest -f docs/reports/REVIEW_REQUEST.md

  # 7. Rich text without shell escaping: quoted heredoc piped through stdin
  codex-ipc -w -f - <<'AP_EOF'
  Review `proxy.yaml` rule AND,((DOMAIN-SUFFIX,example.com)) and $VARS verbatim.
  AP_EOF

  # 8. Collect a reply that arrived after the wait ended (e.g. after fast-fail):
  agent-pair-ipc inbox req_0123456789ab
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target session ID or title (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent as message ('-' reads stdin; recommended for rich text)")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target session ID or title")
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target session ID to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project session binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest session if multiple match current project CWD")
    parser.add_argument("--await-new-thread", "--await-new-session", dest="await_new_thread", action="store_true", help="Wait for a new thread to be created in Codex Desktop, auto-bind and send (works in non-TTY)")
    parser.add_argument("--await-timeout", type=float, default=DEFAULT_AWAIT_TIMEOUT, help=f"Seconds to wait for a new thread (default: {int(DEFAULT_AWAIT_TIMEOUT)})")
    parser.add_argument("-l", "--list", action="store_true", help="List recent Codex sessions")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from target session")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-g", "--activate-gui", action="store_true", help="Activate the Codex desktop app and trigger its new-thread action when waiting for a new thread")
    parser.add_argument("--intent", default="Read-Only Review", help="Peer cooperation intent declaration (default: 'Read-Only Review')")
    parser.add_argument("--user-context", default=None, help="User prompt authorization context to pass in peer envelope")
    parser.add_argument("--sender", default=None, help="Sender name in the peer envelope (default: $AGENT_PAIR_SENDER or auto-detected)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Codex response")
    parser.add_argument("-t", "--timeout", "--wait-timeout", dest="timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--expect-file", "--output-file", dest="expect_file", default=None, help="Expected deliverable file path. Auto-detects completion when file settles with verdict.")
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
            new_sess = await_new_thread(timeout=args.await_timeout, require_tty=False, activate_gui=args.activate_gui, debug=args.debug)
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
    if args.msg_flag is not None:
        warn_inline_rich_text(msg_text)

    is_wait = args.wait or bool(args.expect_file)

    if is_wait:
        resp, verdict = send_and_wait(
            msg_text,
            target=target,
            timeout=args.timeout,
            fast_fail_timeout=args.fast_fail,
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
