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
import select
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agent_pair_ipc.common import (
    EXIT_CHANGES_REQUESTED,
    EXIT_ERROR,
    EXIT_NEEDS_NEW_THREAD,
    EXIT_NEEDS_USER_INPUT,
    EXIT_NO_VERDICT,
    EXIT_QUEUED_UNCONSUMED,
    EXIT_SIGN_OFF,
    EphemeralReplyServer,
    NeedsNewSessionError,
    TailReader,
    activate_desktop_app,
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
    load_project_aliases,
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
_IDLE_WARN_SECONDS = 8.0
_WAITING_FLUSH_GRACE_SECONDS = 1.5
_NON_WAIT_RECEIPT_GRACE_SECONDS = 0.4
_RECEIPT_MAX_BYTES = 65536
_HOST_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,128}$")


def _default_desktop_sessions_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    return Path.home() / ".config" / "Claude" / "claude-code-sessions"


def _extract_last_transcript_permission_mode(
    jsonl_path: Path,
    tail_bytes: int = 262144,
    max_tail_bytes: int = 1048576,
) -> str | None:
    """Read the latest permissionMode from a Claude Code JSONL transcript (bounded tail-first, read-only)."""
    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return None
    if size <= 0:
        return None

    def last_mode_in_lines(lines) -> str | None:
        latest: str | None = None
        for line in lines:
            if not line or b'"permissionMode"' not in line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            if isinstance(obj, dict):
                pm = obj.get("permissionMode")
                if isinstance(pm, str) and pm.strip():
                    latest = pm.strip()
        return latest

    try:
        with open(jsonl_path, "rb") as f:
            if size <= tail_bytes:
                return last_mode_in_lines(f.read().splitlines())
            f.seek(size - tail_bytes)
            tail_lines = f.read(tail_bytes).splitlines()
            if len(tail_lines) > 1:
                found = last_mode_in_lines(tail_lines[1:])
                if found:
                    return found
            if max_tail_bytes > tail_bytes:
                read_len = min(size, max_tail_bytes)
                f.seek(size - read_len)
                wider_lines = f.read(read_len).splitlines()
                lines_to_scan = wider_lines if size <= max_tail_bytes else wider_lines[1:]
                return last_mode_in_lines(lines_to_scan)
            return None
    except OSError:
        return None


def detect_session_permission_mode(
    session: dict,
    desktop_sessions_root: Path | None = None,
) -> tuple[str | None, str]:
    """
    Strictly read-only detection of the target Claude Code session's current permissionMode.
    Priority:
      1. Live Claude Desktop local session JSON (<hostSessionId>.json) — updates immediately on UI mode switch.
      2. JSONL transcript tail (`permissionMode` field on user turns).
      3. Pre-populated `session["permission_mode"]` metadata (if any).
    """
    host_sid = str(session.get("host_session_id") or "").strip()
    target_sid = str(session.get("session_id") or "").strip()
    if host_sid and _HOST_SESSION_ID_RE.match(host_sid):
        root = desktop_sessions_root or _default_desktop_sessions_root()
        try:
            if root.is_dir():
                matches = []
                for p in root.glob(f"*/*/{host_sid}.json"):
                    if not p.is_file():
                        continue
                    try:
                        mtime = p.stat().st_mtime
                        with open(p, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    pm = data.get("permissionMode")
                    if not (isinstance(pm, str) and pm.strip()):
                        continue
                    cli_match = bool(target_sid and data.get("cliSessionId") == target_sid)
                    matches.append((cli_match, mtime, pm.strip()))
                if matches:
                    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
                    return matches[0][2], "desktop_session"
        except Exception:
            pass

    transcript_path = session.get("transcript_path")
    if transcript_path:
        p_tr = Path(transcript_path)
        if p_tr.is_file():
            pm = _extract_last_transcript_permission_mode(p_tr)
            if pm:
                return pm, "transcript"

    raw_pm = session.get("permission_mode")
    if isinstance(raw_pm, str) and raw_pm.strip():
        return raw_pm.strip(), "session"

    return None, ""


def _preferred_session_id(session: dict) -> str:
    """Prefer persistent sessionId (survives hibernation) over ephemeral hostSessionId."""
    return str(session.get("session_id") or session.get("host_session_id") or "")


def _sanitize_project_dir_name(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


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
    if isinstance(bound, str) and bound.strip():
        return bound.strip(), "bindings_file"

    return None, "auto"


def find_transcript_path(cwd: str, session_id: str, projects_dir: Path | None = None) -> str | None:
    """
    Resolve the JSONL transcript file path for a Claude Code session.
    Claude Code sanitizes CWD by replacing all non-alphanumeric characters with '-'.
    """
    if not session_id:
        return None
    if any(ch in session_id for ch in ("/", "\\", "*", "?", "[", "]")) or ".." in session_id:
        return None

    projs = Path(projects_dir) if projects_dir else (Path.home() / ".claude" / "projects")
    if not projs.is_dir():
        return None

    # Method 1: Sanitize cwd by replacing all non-alphanumerics with '-'
    if cwd:
        sanitized_cwd = _sanitize_project_dir_name(cwd)
        p1 = projs / sanitized_cwd / f"{session_id}.jsonl"
        if p1.is_file():
            return str(p1)

    # Method 2: Top-level session JSONL under ~/.claude/projects/<project>/ (avoids subagents/)
    matches = list(projs.glob(f"*/{session_id}.jsonl"))
    if matches:
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(matches[0])

    # Method 3: Deep fallback if ever nested elsewhere
    deep_matches = list(projs.glob(f"**/{session_id}.jsonl"))
    if deep_matches:
        return str(deep_matches[0])

    return None


def _extract_transcript_metadata(
    jsonl_path: Path,
    head_bytes: int = 65536,
    tail_bytes: int = 262144,
) -> dict | None:
    """
    Two-stage metadata extractor for an offline/hibernated Claude Code JSONL transcript:
      1. Fast pass reads head + tail chunks with byte-level prefiltering.
      2. Exact streaming fallback runs only if title/cwd was not found in head+tail
         or if the tail landed inside a single oversized line (e.g. >256KB api-request-blob).
    """
    try:
        st = jsonl_path.stat()
    except OSError:
        return None

    size = st.st_size
    mtime = st.st_mtime
    session_id = jsonl_path.stem
    cwd = ""
    custom_title = ""
    agent_name = ""

    def scan_lines(lines, update_cwd_only_if_empty: bool = False) -> None:
        nonlocal cwd, custom_title, agent_name
        for line in lines:
            if not line:
                continue
            if b'"custom-title"' in line:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict) and obj.get("type") == "custom-title" and obj.get("customTitle"):
                        custom_title = str(obj["customTitle"])
                except Exception:
                    pass
            elif b'"agent-name"' in line:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict) and obj.get("type") == "agent-name" and obj.get("agentName"):
                        agent_name = str(obj["agentName"])
                except Exception:
                    pass
            elif b'"relocatedCwd"' in line:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict) and obj.get("relocatedCwd"):
                        cwd = str(obj["relocatedCwd"])
                except Exception:
                    pass
            elif (not update_cwd_only_if_empty or not cwd) and b'"cwd"' in line:
                try:
                    obj = json.loads(line.decode("utf-8", errors="replace"))
                    if isinstance(obj, dict) and obj.get("cwd"):
                        cwd = str(obj["cwd"])
                except Exception:
                    pass

    try:
        with open(jsonl_path, "rb") as f:
            if size <= head_bytes + tail_bytes:
                scan_lines(f.read().splitlines())
            else:
                scan_lines(f.read(head_bytes).splitlines())
                f.seek(size - tail_bytes)
                tail_lines = f.read(tail_bytes).splitlines()
                tail_truncated_single_line = len(tail_lines) <= 1
                if not tail_truncated_single_line:
                    scan_lines(tail_lines[1:])

                # Stage 2: exact streaming byte-prefilter scan if head+tail missed title/cwd or hit huge single line
                if (not custom_title and not agent_name) or not cwd or tail_truncated_single_line:
                    cwd, custom_title, agent_name = "", "", ""
                    f.seek(0)
                    scan_lines(f, update_cwd_only_if_empty=True)
    except OSError:
        return None

    return {
        "pid": None,
        "name": custom_title or agent_name,
        "session_id": session_id,
        "host_session_id": "",
        "kind": "hibernated",
        "entrypoint": "",
        "cwd": cwd,
        "socket_path": "",
        "token": "",
        "transcript_path": str(jsonl_path),
        "status": "hibernated",
        "waiting_for": "",
        "peer_features": [],
        "mtime": mtime,
        "session_file": "",
        "is_active": False,
    }


def get_offline_sessions(
    cwd: str | None = None,
    exclude_session_ids: set[str] | None = None,
    limit: int = 0,
    projects_dir: Path | None = None,
    debug: bool = False,
) -> list[dict]:
    """
    Discover hibernated/offline Claude Code sessions from ~/.claude/projects/*/*.jsonl.
    Prioritizes transcripts belonging to the current project (and its aliases/parents).
    When limit == 0 (default), scans all top-level transcripts without truncation.
    """
    projs = Path(projects_dir) if projects_dir else (Path.home() / ".claude" / "projects")
    if not projs.is_dir():
        return []

    excluded = exclude_session_ids or set()
    target_cwd = cwd or os.getcwd()
    priority_dir_names: set[str] = set()
    try:
        cur_p = Path(target_cwd).resolve()
        priority_dir_names.add(_sanitize_project_dir_name(str(cur_p)))
        priority_dir_names.add(_sanitize_project_dir_name(target_cwd))
        for alias in load_project_aliases(str(cur_p)):
            priority_dir_names.add(_sanitize_project_dir_name(alias))
        home_p = Path.home().resolve()
        for parent in cur_p.parents:
            if parent == home_p or parent == parent.parent:
                break
            priority_dir_names.add(_sanitize_project_dir_name(str(parent)))
    except Exception:
        pass

    priority_entries: list[tuple[float, Path]] = []
    other_entries: list[tuple[float, Path]] = []
    for p in projs.glob("*/*.jsonl"):
        if not p.is_file() or p.stem in excluded:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if p.parent.name in priority_dir_names:
            priority_entries.append((mtime, p))
        else:
            other_entries.append((mtime, p))

    priority_entries.sort(key=lambda x: x[0], reverse=True)
    other_entries.sort(key=lambda x: x[0], reverse=True)

    selected = priority_entries + other_entries
    if limit > 0:
        selected = selected[:limit]

    sessions: list[dict] = []
    for _, path in selected:
        try:
            meta = _extract_transcript_metadata(path)
            if meta and meta["session_id"] not in excluded:
                sessions.append(meta)
        except Exception as e:
            if debug:
                print(f"[Claude Code IPC Debug] Error reading offline transcript {path}: {e}", file=sys.stderr)
            continue

    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    return sessions


def _read_live_session_state(session: dict) -> dict[str, str]:
    """Read live status / waitingFor from ~/.claude/sessions/<pid>.json if available."""
    sf = session.get("session_file")
    if sf:
        path = Path(sf)
    elif session.get("pid"):
        path = Path.home() / ".claude" / "sessions" / f"{session['pid']}.json"
    else:
        return {}
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {
            "status": str(data.get("status") or ""),
            "waiting_for": str(data.get("waitingFor") or ""),
        }
    except Exception:
        return {}


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
                "waiting_for": data.get("waitingFor") or "",
                "peer_features": data.get("peerFeatures") or [],
                "session_file": str(json_file),
                "is_active": True,
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


def _is_path_like_target(target_str: str) -> bool:
    return (
        target_str.startswith(("/", "~", "./", "../"))
        or os.path.isdir(os.path.expanduser(target_str))
    )


def _disambiguate_matches(
    matches: list[dict],
    target_str: str,
    match_field: str,
    current_cwd: str | None = None,
    allow_cross_project: bool | str = False,
) -> tuple[dict | None, str, str]:
    if not matches:
        return None, "none", ""
    if len(matches) == 1:
        return matches[0], "matched", match_field

    narrowed = matches
    if isinstance(allow_cross_project, str) and allow_cross_project not in ("", "*"):
        norm_allowed = os.path.realpath(os.path.expanduser(allow_cross_project))
        by_allowed = [m for m in matches if m.get("cwd") and os.path.realpath(m["cwd"]) == norm_allowed]
        if by_allowed:
            narrowed = by_allowed
    elif current_cwd:
        in_project = [m for m in matches if cwd_relation(m.get("cwd"), current_cwd) is not None]
        if in_project:
            narrowed = in_project

    if len(narrowed) == 1:
        return narrowed[0], "matched", match_field

    print(f"\n[Claude Code IPC] Error: Ambiguous target '{target_str}'! Matches multiple sessions:", file=sys.stderr)
    for m in narrowed:
        sid_display = _preferred_session_id(m) or str(m.get("pid") or "")
        meta = f"PID: {m.get('pid')}" if m.get("pid") is not None else "Status: hibernated"
        print(f"  • [{sid_display}] \"{m.get('name') or 'Untitled'}\" ({meta})", file=sys.stderr)
    print("Please specify an exact session ID or PID with `-s`.\n", file=sys.stderr)
    return None, "ambiguous", ""


def _find_candidate_by_query(
    target_str: str,
    candidates: list[dict],
    source_kind: str = "cli_flag",
    current_cwd: str | None = None,
    allow_cross_project: bool | str = False,
) -> tuple[dict | None, str, str]:
    """
    Match a single candidate by target query string with strict disambiguation.
    Precedence:
      1. Exact session_id (persistent UUID)
      2. Exact host_session_id (ephemeral local_<uuid>, backward compat)
      3. Exact PID (active sessions only)
      4. Exact name (supports titles containing slashes like "UI/UX 评审"; disambiguated if duplicated)
      [R3 Path Parameter Guard for cli_flag]
      5. Prefix session_id (>= 6 chars; disambiguated)
      6. Prefix host_session_id (>= 6 chars; disambiguated)
      7. Substring name (case-insensitive; disambiguated)

    Returns:
      (matched_session_or_None, status, match_field)
    """
    # 1. Exact match on session_id (persistent)
    sid_exact = [c for c in candidates if c.get("session_id") and c["session_id"] == target_str]
    if sid_exact:
        return _disambiguate_matches(sid_exact, target_str, "session_id", current_cwd, allow_cross_project)
    # 2. Exact match on host_session_id (ephemeral, backward compat)
    hid_exact = [c for c in candidates if c.get("host_session_id") and c["host_session_id"] == target_str]
    if hid_exact:
        return _disambiguate_matches(hid_exact, target_str, "host_session_id", current_cwd, allow_cross_project)
    # 3. Exact match on PID
    for c in candidates:
        if c.get("pid") is not None and str(c["pid"]) == target_str:
            return c, "matched", "pid"
    # 4. Exact match on name (supports titles containing slashes like "UI/UX 评审" or "A/B 测试")
    name_exact = [c for c in candidates if c.get("name") and c["name"] == target_str]
    if name_exact:
        return _disambiguate_matches(name_exact, target_str, "name", current_cwd, allow_cross_project)

    # R3 Path Parameter Guard: only intercept if no exact match and value looks like a filesystem path
    if source_kind == "cli_flag" and _is_path_like_target(target_str):
        print(f"\n[Claude Code IPC] Error: Target session '{target_str}' looks like a filesystem path.", file=sys.stderr)
        print("👉 `-s / --session` requires a session ID, hostSessionId, PID, or session title.", file=sys.stderr)
        print("   To target a project directory, `cd` into that directory or configure project aliases in `.agent-pair/config.json`.\n", file=sys.stderr)
        return None, "invalid_target", ""

    # 5. Prefix match on session_id (>= 6 chars)
    if len(target_str) >= 6:
        sid_prefix = [c for c in candidates if (c.get("session_id") or "").startswith(target_str)]
        if sid_prefix:
            return _disambiguate_matches(sid_prefix, target_str, "session_id_prefix", current_cwd, allow_cross_project)
    # 6. Prefix match on host_session_id (>= 6 chars)
    if len(target_str) >= 6:
        hid_prefix = [c for c in candidates if (c.get("host_session_id") or "").startswith(target_str)]
        if hid_prefix:
            return _disambiguate_matches(hid_prefix, target_str, "host_session_id_prefix", current_cwd, allow_cross_project)
    # 7. Substring match on name (case-insensitive) - with disambiguation
    name_matches = [c for c in candidates if target_str.lower() in (c.get("name") or "").lower()]
    if name_matches:
        return _disambiguate_matches(name_matches, target_str, "name_substring", current_cwd, allow_cross_project)

    return None, "none", ""


def match_session(
    target: str | None = None,
    candidates: list[dict] | None = None,
    allow_cwd_fallback: bool = True,
    pick_latest: bool = False,
    allow_cross_project: bool | str = False,
    quiet_not_found: bool = False,
    debug: bool = False,
) -> tuple[dict | None, str]:
    """
    Match a session from candidates by target query:
    - session_id (exact or prefix >= 6 chars; persistent across hibernation)
    - host_session_id (exact or prefix >= 6 chars; backward compat)
    - pid (exact string, active sessions only)
    - name (exact or case-insensitive substring)

    If target is not given, matches session by current project CWD.
    If multiple sessions match CWD:
      - If `pick_latest` is True: picks the latest active session
      - If `pick_latest` is False: REFUSES to guess, prints candidate list, returns (None, "ambiguous")

    Returns:
        tuple[session_dict | None, status_str] where status_str is 'matched', 'ambiguous', 'invalid_target', 'cross_project_rejected', or 'none'.
    """
    if candidates is None:
        candidates = get_active_sessions(exclude_self=True, debug=debug)

    if not candidates:
        return None, "none"

    resolved_target, source_kind = resolve_target_session(target)

    if resolved_target:
        target_str = str(resolved_target).strip()
        current_cwd = os.getcwd()

        matched, q_status, match_field = _find_candidate_by_query(
            target_str,
            candidates,
            source_kind=source_kind,
            current_cwd=current_cwd,
            allow_cross_project=allow_cross_project,
        )
        if q_status in ("invalid_target", "ambiguous"):
            return None, q_status

        if matched:
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
                    if (
                        source_kind == "bindings_file"
                        and match_field in ("host_session_id", "host_session_id_prefix")
                        and matched.get("session_id")
                    ):
                        print(
                            f"[Claude Code IPC] Tip: .agent-pair/bindings.json binds ephemeral hostSessionId '{target_str}', "
                            f"which resets after hibernation. Upgrade to persistent sessionId with: "
                            f"agent-pair-ipc claude --bind {matched['session_id']}",
                            file=sys.stderr,
                        )
                    return matched, "matched"
            else:
                # source_kind == "cli_flag"
                if relation is None:
                    is_pid_target = target_str.isdigit() or (
                        matched.get("pid") is not None and str(matched.get("pid")) == target_str
                    )
                    if is_pid_target:
                        print(f"\n[Claude Code IPC] ❌ Error: 拒绝跨工程串线投递！跨工程投递严禁使用 PID [{target_str}] 作为目标！", file=sys.stderr)
                        print("   PID 会被操作系统频繁回收和复用，在跨工程调用中极易引发严重的串线事故。", file=sys.stderr)
                        print("   如确实需要跨工程通信，请使用稳定的会话 ID（sessionId / hostSessionId）或会话标题，并配合 `--allow-cross-project`。\n", file=sys.stderr)
                        return None, "cross_project_rejected"

                    target_cwd = matched.get("cwd") or ""
                    status_info = f" (状态: {matched.get('status')})" if matched.get("status") else ""
                    title_info = matched.get("name") or _preferred_session_id(matched)

                    if not allow_cross_project or allow_cross_project is True or allow_cross_project == "*":
                        print(f"\n[Claude Code IPC] ❌ Error: 拒绝跨工程串线投递！", file=sys.stderr)
                        print(f"   指定的目标会话 \"{title_info}\"{status_info} 运行在 '{target_cwd}'，与当前工程 '{current_cwd}' 完全无关！", file=sys.stderr)
                        print("👉 为防止污染无关项目的上下文与聊天记录，工具默认严格阻断跨目录调用。", file=sys.stderr)
                        print("   如确实需要跨目录调用，请显式追加 `--allow-cross-project=<目标工程路径>` 明确确认目标工程路径，且不得使用 PID。\n", file=sys.stderr)
                        return None, "cross_project_rejected"

                    norm_allowed = os.path.realpath(os.path.expanduser(str(allow_cross_project)))
                    norm_target = os.path.realpath(target_cwd) if target_cwd else ""
                    if norm_allowed != norm_target:
                        print(f"\n[Claude Code IPC] ❌ Error: `--allow-cross-project` 指定的目标工程路径 '{allow_cross_project}' 与目标会话实际运行路径 '{target_cwd}' 不一致！\n", file=sys.stderr)
                        return None, "cross_project_rejected"

                    if matched.get("status") == "busy":
                        print(f"[Claude Code IPC] ⚠️ Warning: 目标跨工程会话当前处于 busy 忙碌状态，可能正在执行其他工程任务。", file=sys.stderr)
                    print(f"[Claude Code IPC] ⚠️ NOTICE: 跨工程调用已显式允许。目标会话运行在 '{target_cwd}'，与当前目录 '{current_cwd}' 不一致！", file=sys.stderr)
                return matched, "matched"

        if resolved_target:
            if source_kind == "bindings_file" and not quiet_not_found:
                print(f"\n[Claude Code IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
                print("👉 The bound session may have ended, hibernated, or been closed.", file=sys.stderr)
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
        sid_display = _preferred_session_id(chosen) or str(chosen.get("pid") or "")
        print(f"[Claude Code IPC] Notice: Multiple sessions found for this project. Using latest due to `--pick-latest`: \"{chosen.get('name')}\" [{sid_display}]", file=sys.stderr)
        return chosen, "matched"
    else:
        # Strict disambiguation: refuse to guess!
        is_offline = all(m.get("is_active") is False for m in cwd_matches)
        scope_label = "hibernated" if is_offline else "active"
        print(f"\n[Claude Code IPC] Error: Ambiguous sessions! Multiple {scope_label} Claude Code sessions found for directory '{current_cwd}':", file=sys.stderr)
        for m in cwd_matches:
            mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(m.get('mtime', 0.0))) if m.get('mtime') else 'unknown'
            sid_display = _preferred_session_id(m) or str(m.get('pid') or '')
            if m.get("pid") is not None:
                detail = f"PID: {m.get('pid')}, Kind: {m.get('kind')}, Active: {mtime_str}"
            else:
                detail = f"Status: hibernated, Last Active: {mtime_str}"
            print(f"  • [{sid_display}] \"{m.get('name') or 'Untitled'}\" ({detail})", file=sys.stderr)
        print("\nPlease specify a target session with `-s <session_id|name|pid>`, set AGENT_PAIR_CLAUDE_SESSION,", file=sys.stderr)
        print("save to .agent-pair/bindings.json, or pass `--pick-latest` to confirm.\n", file=sys.stderr)
        return None, "ambiguous"


def resolve_bind_target(target: str, cwd: str | None = None, debug: bool = False) -> tuple[str | None, str]:
    """
    Resolve a `--bind <target>` argument into a persistent sessionId.
    - Accepts sessionId, hostSessionId (`local_*`), active PID, or session title.
    - Prioritizes sessions belonging to the current project (active then hibernated)
      before considering sessions from other projects.
    - Enforces path-parameter guard even when no sessions exist, and refuses raw numeric PIDs.
    """
    target_str = (target or "").strip()
    if not target_str:
        print("[Claude Code IPC] Error: --bind target cannot be empty.", file=sys.stderr)
        return None, "empty"

    current_cwd = cwd or os.getcwd()
    active = get_active_sessions(exclude_self=False, debug=debug)
    active_ids = {c["session_id"] for c in active if c.get("session_id")}

    offline = get_offline_sessions(cwd=current_cwd, exclude_session_ids=active_ids, debug=debug)
    direct_path = find_transcript_path(current_cwd, target_str)
    if direct_path:
        direct_meta = _extract_transcript_metadata(Path(direct_path))
        if (
            direct_meta
            and direct_meta["session_id"] not in active_ids
            and not any(o.get("session_id") == direct_meta["session_id"] for o in offline)
        ):
            offline.insert(0, direct_meta)

    in_proj_active = [c for c in active if cwd_relation(c.get("cwd"), current_cwd) is not None]
    in_proj_offline = [c for c in offline if cwd_relation(c.get("cwd"), current_cwd) is not None]
    other_active = [c for c in active if c not in in_proj_active]
    other_offline = [c for c in offline if c not in in_proj_offline]

    search_tiers = [
        (in_proj_active, False, "active"),
        (in_proj_offline, False, "hibernated"),
        (other_active, True, "active"),
        (other_offline, True, "hibernated"),
    ]
    for pool, is_foreign, state_label in search_tiers:
        if not pool:
            continue
        matched, status, _ = _find_candidate_by_query(
            target_str,
            pool,
            source_kind="cli_flag",
            current_cwd=current_cwd,
        )
        if status in ("ambiguous", "invalid_target"):
            return None, status
        if matched:
            resolved_id = _preferred_session_id(matched)
            if resolved_id:
                label = matched.get("name") or resolved_id
                if is_foreign:
                    print(
                        f"[Claude Code IPC] ⚠️ Warning: Matched session \"{label}\" [{resolved_id}] belongs to another directory ('{matched.get('cwd')}').",
                        file=sys.stderr,
                    )
                if state_label == "active":
                    return resolved_id, f"active session \"{label}\" (PID {matched.get('pid')})"
                return resolved_id, f"hibernated session \"{label}\""

    # R3 Path Parameter Guard even when active & offline pools are empty
    if _is_path_like_target(target_str):
        print(f"\n[Claude Code IPC] Error: Target session '{target_str}' looks like a filesystem path.", file=sys.stderr)
        print("👉 `--bind` requires a session ID, hostSessionId, active PID, or session title (not a directory path).\n", file=sys.stderr)
        return None, "invalid_target"

    # Unresolved fallback: never persist a raw numeric PID
    if target_str.isdigit():
        print(
            f"[Claude Code IPC] Error: Refusing to bind raw numeric PID '{target_str}' (no active session matched).",
            file=sys.stderr,
        )
        print("👉 PIDs are ephemeral and reused by the OS. Pass a persistent sessionId, session title, or an active PID.", file=sys.stderr)
        return None, "invalid_pid"

    if target_str.startswith("local_"):
        print(
            f"[Claude Code IPC] ⚠️ Warning: '{target_str}' looks like an ephemeral hostSessionId that resets after hibernation "
            "and did not match any active session. Prefer binding the persistent sessionId (UUID).",
            file=sys.stderr,
        )
    return target_str, "explicit"


def _session_id_set(sessions: list[dict]) -> set[str]:
    ids = set()
    for s in sessions:
        if s.get("session_id"):
            ids.add(s["session_id"])
        if s.get("host_session_id"):
            ids.add(s["host_session_id"])
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
    watch for a new or newly woken Claude Code session (Desktop tab or `claude` in a terminal) for this project.
    - Snapshots pre-existing active sessions so older active sessions are never misidentified.
    - Accepts a session at the project directory or a parent of it (symlink-resolved).
    - Reports new sessions opened in another folder instead of silently ignoring them.
    - In non-interactive environments (non-TTY) with require_tty, exits immediately instead of blocking.
    When detected, automatically binds persistent sessionId (falling back to hostSessionId, never raw PID)
    to .agent-pair/bindings.json.
    """
    target_cwd = cwd or os.getcwd()
    if require_tty and not sys.stderr.isatty():
        print(f"[Claude Code IPC] Notice: No existing Claude Code session found for project '{target_cwd}'.", file=sys.stderr)
        print("[Claude Code IPC] Cannot interactively wait for new session in non-interactive environment (non-TTY).", file=sys.stderr)
        print("👉 Open the project in Claude Desktop (File > New Session or click a hibernated session to wake it) or run `claude` in a terminal, then retry;", file=sys.stderr)
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

    print(f"[Claude Code IPC] 当前工程尚未绑定活跃会话。正在等待新会话或休眠会话唤醒: '{target_cwd}'", file=sys.stderr)
    print("[Claude Code IPC] 请在 Claude Desktop 中点击已有休眠会话唤醒、新建会话 (File > New Session / Cmd+N)，或在终端运行 `claude`。", file=sys.stderr)

    reported_foreign: set[str] = set()
    tty = sys.stderr.isatty()
    while time.time() - watch_start < timeout:
        time.sleep(1.0)
        elapsed = int(time.time() - watch_start)
        if tty:
            print(f"\r[Claude Code IPC] [侦测中 {elapsed:02d}s / {int(timeout)}s] 等待会话激活...", end="", file=sys.stderr)

        for s in get_active_sessions(exclude_self=True, debug=debug):
            sid = _preferred_session_id(s)
            pid_str = str(s.get("pid"))
            if (
                (s.get("session_id") and s["session_id"] in initial_ids)
                or (s.get("host_session_id") and s["host_session_id"] in initial_ids)
                or (pid_str in initial_ids)
            ):
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
            # Prefer persistent sessionId (survives hibernation), fallback to hostSessionId, never raw PID
            bind_id = _preferred_session_id(s)
            if bind_id:
                save_project_binding("claude", bind_id, cwd=target_cwd)
                if tty:
                    print("", file=sys.stderr)
                print(f"[Claude Code IPC] 监听到会话就绪: PID {s.get('pid')} [{bind_id}] -> 自动完成持久绑定！", file=sys.stderr)
            return s

    if tty:
        print("", file=sys.stderr)
    print(f"[Claude Code IPC] Timed out waiting for Claude Code session after {int(timeout)}s.", file=sys.stderr)
    return None


def _find_hibernated_hint(target: str | None, debug: bool = False) -> dict | None:
    """
    Best-effort offline lookup restricted to non-active transcripts belonging to the current project.
    Never reports an active (including self) session as hibernated, and never leaks cross-project sessions.
    """
    resolved_target, source_kind = resolve_target_session(target)
    if resolved_target and source_kind == "cli_flag" and _is_path_like_target(resolved_target):
        return None

    current_cwd = os.getcwd()
    active_ids = {
        c["session_id"]
        for c in get_active_sessions(exclude_self=False, debug=debug)
        if c.get("session_id")
    }
    offline = get_offline_sessions(cwd=current_cwd, exclude_session_ids=active_ids, debug=debug)
    proj_offline = filter_project_sessions(offline, current_cwd)
    if not proj_offline:
        return None

    if resolved_target:
        matched, status, _ = _find_candidate_by_query(
            resolved_target,
            proj_offline,
            source_kind=source_kind,
            current_cwd=current_cwd,
        )
        if status == "matched" and matched:
            return matched
        return None
    return proj_offline[0]


def _report_missing_session(target: str | None, debug: bool = False) -> None:
    resolved_target, source_kind = resolve_target_session(target)
    hibernated = _find_hibernated_hint(target, debug=debug)

    if hibernated:
        sid = hibernated.get("session_id") or ""
        title = hibernated.get("name") or "Untitled"
        mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(hibernated.get("mtime", 0.0))) if hibernated.get("mtime") else "unknown"
        if resolved_target:
            src_note = " (from .agent-pair/bindings.json)" if source_kind == "bindings_file" else ""
            print(
                f"[Claude Code IPC] Error: Target session \"{title}\" [{sid}]{src_note} is currently hibernated (no active socket, last active: {mtime_str}).",
                file=sys.stderr,
            )
        else:
            print(
                f"[Claude Code IPC] Error: No active Claude Code session for '{os.getcwd()}'. Most recent hibernated session: \"{title}\" [{sid}] (last active: {mtime_str}).",
                file=sys.stderr,
            )
        print("👉 Click this session in Claude Desktop to wake it up, then retry sending;", file=sys.stderr)
        print(f"   or read its latest transcript offline: agent-pair-ipc claude -r -s {sid}", file=sys.stderr)
        return

    if source_kind == "bindings_file" and resolved_target:
        print(f"\n[Claude Code IPC] Error: Bound session '{resolved_target}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
        if resolved_target.startswith("local_"):
            print("👉 This binding uses an ephemeral hostSessionId (`local_*`), which resets after Claude Desktop hibernates.", file=sys.stderr)
            print("   Run `agent-pair-ipc claude -l` to see persistent sessionIds, then re-bind with `--bind <sessionId>` (or `--unbind`).\n", file=sys.stderr)
        else:
            print("👉 The bound session may have ended, hibernated, or been closed.", file=sys.stderr)
            print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)
    elif target:
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
    allow_cross_project: bool | str = False,
    debug: bool = False,
) -> tuple[dict | None, str]:
    """Shared session resolution for send / send-and-wait. Raises NeedsNewSessionError in non-TTY."""
    candidates = get_active_sessions(exclude_self=True, debug=debug)
    session, status = (
        match_session(
            target=target,
            candidates=candidates,
            pick_latest=pick_latest,
            allow_cross_project=allow_cross_project,
            quiet_not_found=True,
            debug=debug,
        )
        if candidates
        else (None, "none")
    )
    if session or status in ("ambiguous", "invalid_target", "cross_project_rejected"):
        return session, status
    resolved_target, _ = resolve_target_session(target)
    if resolved_target is None and auto_await_new:
        session = await_new_session(timeout=await_timeout, cwd=os.getcwd(), activate_gui=activate_gui, debug=debug)
        return session, ("matched" if session else "none")
    return None, status


def _read_inbound_setting_from_file(p: Path) -> tuple[bool, str | None, str | None]:
    """
    Safely reads 'crossSessionInbound' from JSON file p (read-only).
    Returns (file_exists_and_valid_json, raw_str_value, error_note).
    """
    if not p.is_file():
        return False, None, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return True, None, "root is not a JSON object"
        raw = data.get("crossSessionInbound")
        if raw is None:
            return True, None, None
        return True, str(raw).strip(), None
    except Exception as e:
        return True, None, f"JSON parse error: {e}"


def resolve_cross_session_inbound_policy(
    target_cwd: str | Path | None = None,
    home_dir: str | Path | None = None,
    managed_path: str | Path | None = None,
) -> tuple[str, str]:
    """
    Resolve the effective crossSessionInbound policy for a target Claude Code session
    using Claude Code 2.1+ configuration cascade (strictly read-only).

    Cascade precedence:
      1. Managed settings (cannot be overridden by user or repo):
         - macOS: /Library/Application Support/ClaudeCode/managed-settings.json
         - Linux: /etc/claude-code/managed-settings.json
      2. Repository settings in target_cwd (can ONLY tighten to 'hold' or 'refuse'; cannot widen to 'accept'):
         - <target_cwd>/.claude/settings.local.json
         - <target_cwd>/.claude/settings.json
      3. User global settings:
         - ~/.claude/settings.local.json
         - ~/.claude/settings.json

    Returns:
      (inferred_policy, source_description)
    """
    valid_policies = {"accept", "hold", "refuse"}

    # 1. Managed settings
    if managed_path is not None:
        managed_candidates = [Path(managed_path)]
    else:
        managed_candidates = [
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
            Path("/etc/claude-code/managed-settings.json"),
        ]

    for cand in managed_candidates:
        exists, raw, _ = _read_inbound_setting_from_file(cand)
        if exists and raw is not None:
            norm = raw.lower()
            if norm not in valid_policies:
                return "hold", f"组织托管配置中有未识别值: {cand} (取值: '{raw}')"
            return norm, f"组织托管配置: {cand} (不可被用户或仓库覆盖)"

    # 2. Repository settings (if target_cwd is provided; repo may ONLY tighten, never widen to accept)
    ignored_repo_accept: Path | None = None
    if target_cwd:
        try:
            cwd_path = Path(target_cwd).resolve()
            repo_candidates = [
                cwd_path / ".claude" / "settings.local.json",
                cwd_path / ".claude" / "settings.json",
            ]
            for cand in repo_candidates:
                exists, raw, _ = _read_inbound_setting_from_file(cand)
                if exists and raw is not None:
                    norm = raw.lower()
                    if norm not in valid_policies:
                        return "hold", f"仓库配置中有未识别值: {cand} (取值: '{raw}')"
                    if norm in ("hold", "refuse"):
                        return norm, f"仓库收紧配置: {cand} (用户配置无法突破)"
                    if norm == "accept" and ignored_repo_accept is None:
                        ignored_repo_accept = cand
        except Exception:
            pass

    # 3. User global settings
    hdir = Path(home_dir).resolve() if home_dir else Path.home()
    user_candidates = [
        hdir / ".claude" / "settings.local.json",
        hdir / ".claude" / "settings.json",
    ]
    for cand in user_candidates:
        exists, raw, _ = _read_inbound_setting_from_file(cand)
        if exists and raw is not None:
            norm = raw.lower()
            if norm not in valid_policies:
                return "hold", f"用户配置中有未识别值: {cand} (取值: '{raw}')"
            return norm, f"用户全局配置: {cand}"

    # 4. Default policy (repo 'accept' cannot loosen parity_check)
    if ignored_repo_accept is not None:
        return (
            "parity_check",
            f"未显式配置用户级 crossSessionInbound (默认按模式对齐判定；注意 {ignored_repo_accept} 中的 'accept' 无效，仓库级配置仅可收紧不可放宽)",
        )
    return "parity_check", "未显式配置 crossSessionInbound (默认按模式对齐判定)"


def _format_inbound_guidance(
    policy: str,
    policy_src: str,
    include_summary: bool = True,
    entrypoint: str = "",
    permission_mode: str | None = None,
) -> list[str]:
    """
    Build strictly read-only, policy-specific configuration & recovery guidance
    respecting the SECURITY.md §6 safety hierarchy and Desktop vs CLI UI differences.
    """
    lines = [f"  • 当前推断生效的入站配置: [策略: {policy}] (来源: {policy_src})"] if include_summary else []
    if permission_mode and include_summary:
        lines.append(f"  • 目标会话当前权限模式: {permission_mode}")
    if "未识别值" in policy_src:
        lines.append("  • [修复非法值] 配置文件中的 crossSessionInbound 取值无效（合法值为 accept / hold / refuse），修正前所有入站消息会被扣留。")
        return lines

    if policy in ("hold", "refuse"):
        if "组织托管配置" in policy_src:
            lines.append("  • [受组织策略约束] 该策略由组织托管配置强制下发，用户或仓库配置无法覆盖；如需调整请联系管理员。")
        elif "仓库收紧配置" in policy_src:
            lines.append("  • [受仓库配置收紧] 目标仓库配置将入站策略收紧，用户全局 accept 无法突破；请检查该仓库的 .claude/settings*.json。")
        else:
            lines.append("  • [用户配置] 请检查 ~/.claude/settings.local.json 或 ~/.claude/settings.json 中的 crossSessionInbound 字段。")
        if policy == "hold":
            if entrypoint == "claude-desktop":
                lines.append("  • [Claude Desktop 说明] 桌面端运行于 headless 模式，界面无「待审核卡片」UI；暂留消息将在 5 分钟后过期，需调整上述配置或改用 CLI 端会话。")
            else:
                lines.append("  • [首选] 切换到 Claude Code 目标会话，查看底部「待审核卡片」（Review it below）并点击批准。")
        return lines

    if policy == "accept":
        lines.append("  • 当前推断策略已为 accept；若仍未入队，请检查目标会话是否正停在 AskUserQuestion/权限弹窗，或被 --setting-sources / MDM 策略覆盖。")
        return lines

    # Default: parity_check
    if entrypoint == "claude-desktop":
        if permission_mode and permission_mode != "bypassPermissions":
            lines.extend([
                f"  1. [Claude Desktop 说明] 目标会话当前探测模式为 `{permission_mode}`（非 `bypassPermissions`），且桌面端界面无「待审核卡片」UI；若仍未入队，请检查会话是否正在忙碌/等待输入，或核对 `--setting-sources` / 尝试切换一次模式（勿重复重发）；",
                "  2. [全局放行·需自行权衡] Claude Code 的仓库级 `.claude/settings.json` 仅可收紧（hold/refuse）不可单项目放行；仅当你完全信任本机所有同用户进程时，可手动在 `~/.claude/settings.json`（或 `~/.claude/settings.local.json`）加入 `\"crossSessionInbound\": \"accept\"`（⚠️ 全局生效，将跳过含 bypassPermissions 在内所有会话的入站审核；本工具只读不代写）；",
                "  3. 了解更多跨 Agent 自动化配置与安全风险，请查阅 SECURITY.md §6。",
            ])
        else:
            lines.extend([
                "  1. [首选·Claude Desktop] Claude Desktop 运行于 headless 模式，界面无「待审核卡片」UI，暂留消息将在 5 分钟后自动过期；请将目标会话左下角权限模式从 `bypassPermissions` 切为 `Auto` / `Default` / `Accept Edits`（5 分钟内切换会自动放行暂留消息，请勿重复重发）；",
                "  2. [全局放行·需自行权衡] Claude Code 的仓库级 `.claude/settings.json` 仅可收紧（hold/refuse）不可单项目放行；仅当你完全信任本机所有同用户进程时，可手动在 `~/.claude/settings.json`（或 `~/.claude/settings.local.json`）加入 `\"crossSessionInbound\": \"accept\"`（⚠️ 全局生效，将跳过含 bypassPermissions 在内所有会话的入站审核；本工具只读不代写）；",
                "  3. 了解更多跨 Agent 自动化配置与安全风险，请查阅 SECURITY.md §6。",
            ])
    elif entrypoint == "cli":
        lines.extend([
            "  1. [首选·Claude Code CLI] 请在终端底部查看「待审核卡片」（Review it below），点击批准即可放行；",
            "  2. [推荐] 建议目标会话使用默认交互模式或 auto 模式（已观察到与外部 Agent 的 prompting 模式对齐时可直达；bypassPermissions 会话默认触发扣留审核）；",
            "  3. [全局放行·需自行权衡] Claude Code 的仓库级 `.claude/settings.json` 仅可收紧（hold/refuse）不可单项目放行；仅当你完全信任本机所有同用户进程时，可手动在 `~/.claude/settings.json`（或 `~/.claude/settings.local.json`）加入 `\"crossSessionInbound\": \"accept\"`（⚠️ 全局生效，将跳过含 bypassPermissions 在内所有会话的入站审核；本工具只读不代写）；",
            "  4. 了解更多跨 Agent 自动化配置与安全风险，请查阅 SECURITY.md §6。",
        ])
    else:
        lines.extend([
            "  1. [首选·按端侧区分] Claude Desktop 无「待审核卡片」UI（暂留消息 5 分钟自动过期），请将目标会话从 `bypassPermissions` 切为 `Auto` / `Default` / `Accept Edits`；Claude Code CLI（终端）请在底部查看「待审核卡片」（Review it below）并点击批准放行；",
            "  2. [推荐] 建议目标会话使用默认交互模式或 auto 模式（已观察到与外部 Agent 的 prompting 模式对齐时可直达；bypassPermissions 会话默认触发扣留审核）；",
            "  3. [全局放行·需自行权衡] Claude Code 的仓库级 `.claude/settings.json` 仅可收紧（hold/refuse）不可单项目放行；仅当你完全信任本机所有同用户进程时，可手动在 `~/.claude/settings.json`（或 `~/.claude/settings.local.json`）加入 `\"crossSessionInbound\": \"accept\"`（⚠️ 全局生效，将跳过含 bypassPermissions 在内所有会话的入站审核；本工具只读不代写）；",
            "  4. 了解更多跨 Agent 自动化配置与安全风险，请查阅 SECURITY.md §6。",
        ])
    return lines


def _format_receipt_event(
    receipt: dict,
    nonce: str,
    entrypoint: str,
    session_name: str,
    target_sid: str,
    policy: str | None = None,
    policy_src: str | None = None,
    permission_mode: str | None = None,
) -> list[str]:
    """Format native Claude Code `peer_message_status` receipt events for stderr."""
    status = str(receipt.get("status") or "").lower()
    reason = str(receipt.get("reason") or "").strip()
    drop_reason = str(receipt.get("drop_reason") or "").strip()
    sess_label = session_name or target_sid

    if status == "held":
        lines = [
            f"[Claude Code IPC] Delivery status: HELD (Req-ID: {nonce}) — 对端入站网关已暂留该消息，请勿重复重发。",
        ]
        if entrypoint == "claude-desktop":
            if policy == "hold" or (policy_src and "未识别值" in policy_src):
                lines.append(
                    f"[Claude Code IPC] 提示: Claude Desktop 无「待审核卡片」UI，暂留消息将在 5 分钟后过期。"
                    f"当前暂留由配置 ({policy_src or 'crossSessionInbound=hold'}) 触发，切换权限模式无法放行，需调整入站配置或改用 CLI 端会话。"
                )
            elif permission_mode and permission_mode != "bypassPermissions":
                lines.append(
                    f"[Claude Code IPC] 提示: Claude Desktop 无「待审核卡片」UI，暂留消息将在 5 分钟后过期。"
                    f"目标会话当前探测模式为 {permission_mode}，请运行 `--doctor` 排查是否受配置或会话模式切换过程影响。"
                )
            else:
                lines.append(
                    f"[Claude Code IPC] 提示: Claude Desktop 无「待审核卡片」UI，暂留消息将在 5 分钟后自动过期。"
                    f"请在桌面端会话 \"{sess_label}\" 将左下角权限模式从 bypassPermissions 切为 Auto / Default / Accept Edits（5 分钟内切换会自动放行该消息）。"
                )
        else:
            lines.append(
                f"[Claude Code IPC] 提示: 请在目标会话 \"{sess_label}\" 底部查看「待审核卡片」（Review it below）并点击批准放行。"
            )
        return lines

    if status == "delivered":
        return [
            f"[Claude Code IPC] Delivery status: DELIVERED (Req-ID: {nonce}) — 此前暂留的消息已获放行并进入工作队列。"
        ]

    if status in ("refused", "dropped", "denied", "expired"):
        detail = ""
        if status == "refused" and policy and policy_src:
            detail = f" (推断策略: {policy}, 来源: {policy_src})"
        elif status == "dropped" and drop_reason:
            detail = f" (drop_reason: {drop_reason})"
        elif reason:
            detail = f" ({reason})"
        lines = [
            f"[Claude Code IPC] Delivery status: {status.upper()} (Req-ID: {nonce}){detail}",
            f"[Claude Code IPC] 确认该消息未进入目标会话 \"{sess_label}\" 工作队列；排除原因后可安全重发。",
        ]
        if status == "refused" and policy and policy_src:
            lines.extend(_format_inbound_guidance(policy, policy_src, include_summary=False, entrypoint=entrypoint))
        return lines

    suffix = f" ({reason})" if reason else ""
    return [f"[Claude Code IPC] Delivery status: {status.upper()} (Req-ID: {nonce}){suffix}"]


class PeerReceiptListener:
    """
    Ephemeral same-directory Unix Domain Socket listener for Claude Code 2.1+
    native `peer_message_status` reverse receipts (`held`, `delivered`, `refused`,
    `dropped`, `denied`, `expired`).

    Claude Code's UDS vetting (`r9i`) requires the reply address to live in the
    exact same lexical directory as `messagingSocketPath` (e.g. `/tmp/cc-socks`
    without resolving `/tmp` -> `/private/tmp`) and verifies peer PID/UID.
    """

    def __init__(self, target_socket_path: str, msg_uuid: str | None = None):
        self.target_socket_path = target_socket_path
        self.msg_uuid = msg_uuid or str(uuid.uuid4())
        self.socket_path: str | None = None
        self.server_sock: socket.socket | None = None
        self._closed = False

    @property
    def from_address(self) -> str | None:
        return f"uds:{self.socket_path}" if self.socket_path else None

    def start(self) -> str | None:
        if os.name == "nt" or not hasattr(socket, "AF_UNIX") or not self.target_socket_path:
            return None
        # Keep lexical dirname so /tmp/cc-socks matches Claude Code's same-dir vetting.
        sock_dir = os.path.dirname(self.target_socket_path)
        if not sock_dir:
            return None
        try:
            st = os.lstat(sock_dir)
        except OSError:
            return None
        if not stat.S_ISDIR(st.st_mode):
            return None
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return None
        if (st.st_mode & 0o002) != 0:
            return None

        cand = os.path.join(sock_dir, f"ap-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
        if len(cand.encode("utf-8")) > 103:
            return None

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            old_umask = os.umask(0o177)
            try:
                sock.bind(cand)
                bound = True
            finally:
                os.umask(old_umask)
            os.chmod(cand, 0o600)
            sock.listen(8)
            sock.setblocking(False)
            self.socket_path = cand
            self.server_sock = sock
            self._closed = False
            return cand
        except OSError:
            try:
                sock.close()
            except Exception:
                pass
            if bound:
                try:
                    os.unlink(cand)
                except Exception:
                    pass
            return None

    def _read_client_frames(self, conn: socket.socket) -> list[dict]:
        buf = bytearray()
        deadline = time.monotonic() + 0.5
        saw_newline = False
        while len(buf) < _RECEIPT_MAX_BYTES:
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            try:
                # Avoid blocking for Claude Code's 150ms macOS close delay once a full NDJSON line arrives.
                conn.settimeout(min(0.05, rem) if saw_newline else min(0.5, rem))
                chunk = conn.recv(min(4096, _RECEIPT_MAX_BYTES - len(buf)))
            except (socket.timeout, TimeoutError, BlockingIOError, OSError):
                break
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk:
                saw_newline = True

        frames: list[dict] = []
        for raw_line in bytes(buf).decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if d.get("type") != "control" or d.get("action") != "peer_message_status":
                continue

            raw_status = str(d.get("status") or "").strip().lower()
            detail = str(d.get("status_detail") or "").strip().lower()
            eff_status = "refused" if (raw_status == "expired" and detail == "refused") else raw_status
            if not eff_status:
                continue

            orig_id = d.get("orig_msg_id")
            dropped_ids = d.get("dropped_msg_ids")
            if orig_id is not None:
                if orig_id != self.msg_uuid and not (
                    eff_status == "dropped" and isinstance(dropped_ids, list) and self.msg_uuid in dropped_ids
                ):
                    continue
            elif isinstance(dropped_ids, list) and len(dropped_ids) > 0:
                if not (eff_status == "dropped" and self.msg_uuid in dropped_ids):
                    continue
            else:
                continue

            frames.append({
                "status": eff_status,
                "reason": str(d.get("reason") or ""),
                "drop_reason": str(d.get("drop_reason") or ""),
            })
        return frames

    def poll(self, timeout: float = 0.0) -> list[dict]:
        if self.server_sock is None or self._closed:
            return []
        new_receipts: list[dict] = []
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            rem = max(0.0, deadline - time.monotonic())
            try:
                readable, _, _ = select.select([self.server_sock], [], [], rem)
            except (OSError, ValueError):
                break
            if readable:
                while True:
                    try:
                        conn, _ = self.server_sock.accept()
                    except (BlockingIOError, OSError):
                        break
                    try:
                        new_receipts.extend(self._read_client_frames(conn))
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass
                if new_receipts:
                    break
            if timeout <= 0 or time.monotonic() >= deadline:
                break
        return new_receipts

    def close(self) -> None:
        self._closed = True
        sock = self.server_sock
        self.server_sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        sp = self.socket_path
        self.socket_path = None
        if sp:
            try:
                os.unlink(sp)
            except Exception:
                pass


def wrap_cross_session_xml(content: str, sender: str = "ext:Antigravity", mode: str = "prompting") -> str:
    """
    Wrap message in Claude Code 2.1+ native cross-session XML envelope with honest mode assertion.
    External callers are hardcoded to from-mode="prompting" to eliminate permission spoofing.
    Prefix sender with 'ext:' to prevent spoofing internal session names.
    Strictly follows Claude Code regex:
      ^<cross-session-message(?: from="...")?(?: from-name="...")?(?: from-mode="...")?>\\n([\\s\\S]*)\\n</cross-session-message>$
    """
    if content.strip().startswith("<cross-session-message"):
        return content

    clean_sender = re.sub(r'["<>\n\r]', '', sender or "Antigravity").strip() or "Antigravity"
    if not clean_sender.startswith("ext:"):
        clean_sender = f"ext:{clean_sender}"
    clean_mode = "prompting"
    escaped = re.sub(r"</(\s*cross-session-message)", r"<\\/\1", content, flags=re.IGNORECASE)
    return f'<cross-session-message from="{clean_sender}" from-name="{clean_sender}" from-mode="{clean_mode}">\n{escaped}\n</cross-session-message>'


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
    allow_cross_project: bool | str = False,
    debug: bool = False,
    receipt_listener: PeerReceiptListener | None = None,
) -> bool:
    """
    Send message to target Claude Code session via Unix Domain Socket.
    Messages not yet enveloped are wrapped in a Peer Envelope that guides the peer
    to answer via conversation transcript or deliverable artifact (zero-click).
    Attaches handshake metadata (ipc_version, request_id, caller, caller_mode, expected_artifact).
    """
    session, status = _resolve_or_await_session(target, pick_latest, auto_await_new, activate_gui, await_timeout, allow_cross_project, debug)
    if not session:
        if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
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
            reply_command=None,
            expected_artifact=expected_artifact,
            caller_cwd=current_cwd,
            target_cwd=session.get("cwd"),
            cwd_relation_type=rel,
        )

    # Standardized Handshake Metadata & Caller Mode Declaration (honest prompting mode)
    caller_name = sender or detect_sender()
    caller_mode = "prompting"
    metadata: dict[str, str] = {
        "ipc_version": "2.1",
        "request_id": nonce,
        "caller": caller_name,
        "caller_mode": caller_mode,
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

    final_payload = wrap_cross_session_xml(message, sender=caller_name, mode=caller_mode)

    owned_listener: PeerReceiptListener | None = None
    active_listener = receipt_listener
    try:
        if active_listener is None:
            owned_listener = PeerReceiptListener(sock_path)
            owned_listener.start()
            active_listener = owned_listener

        user_payload: dict = {
            "type": "user",
            "message": {
                "role": "user",
                "content": final_payload,
                "metadata": metadata,
            },
        }
        if active_listener and active_listener.from_address:
            user_payload["from"] = active_listener.from_address
            user_payload["msg_id"] = active_listener.msg_uuid

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10.0)
        s.connect(sock_path)

        auth_msg = json.dumps({"type": "auth", "token": token}) + "\n"
        user_msg = json.dumps(user_payload) + "\n"

        s.sendall(auth_msg.encode("utf-8"))
        s.sendall(user_msg.encode("utf-8"))

        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass

        try:
            s.settimeout(0.3)
            s.recv(1)
        except Exception:
            pass
        s.close()

        display_name = session["name"] or session["session_id"]
        sess_id = _preferred_session_id(session)
        entrypoint = str(session.get("entrypoint") or "")
        rejection_statuses = {"refused", "dropped", "denied", "expired"}

        if owned_listener is not None:
            immediate_receipts = (
                owned_listener.poll(timeout=_NON_WAIT_RECEIPT_GRACE_SECONDS)
                if owned_listener.socket_path
                else []
            )
            rejected = any(r.get("status") in rejection_statuses for r in immediate_receipts)
            if rejected:
                pol, pol_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                perm_mode, _ = detect_session_permission_mode(session)
                for receipt in immediate_receipts:
                    r_pol, r_src = (pol, pol_src) if receipt.get("status") in ("refused", "held") else (None, None)
                    for line in _format_receipt_event(
                        receipt,
                        nonce,
                        entrypoint,
                        display_name,
                        sess_id,
                        policy=r_pol,
                        policy_src=r_src,
                        permission_mode=perm_mode,
                    ):
                        print(line, file=sys.stderr)
                return False

            print(f"[Claude Code IPC] Message successfully delivered to session PID {session['pid']} ({display_name}) [ID: {sess_id}] (Req-ID: {nonce}, Mode: {caller_mode}).")
            if expected_artifact:
                print(f"[Claude Code IPC] Handshake: Expected artifact registered -> {expected_artifact}", file=sys.stderr)
            if session.get("status") == "busy":
                print("[Claude Code IPC] Note: Target session is busy; the message will be processed after its current turn.", file=sys.stderr)
            elif session.get("status") == "waiting":
                wf = session.get("waiting_for") or "user input"
                print(f"[Claude Code IPC] Note: Target session is currently waiting ({wf}); check Claude Desktop to unblock it.", file=sys.stderr)

            if immediate_receipts:
                pol, pol_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                perm_mode, _ = detect_session_permission_mode(session)
                for receipt in immediate_receipts:
                    for line in _format_receipt_event(
                        receipt,
                        nonce,
                        entrypoint,
                        display_name,
                        sess_id,
                        policy=pol,
                        policy_src=pol_src,
                        permission_mode=perm_mode,
                    ):
                        print(line, file=sys.stderr)
            else:
                perm_mode, _ = detect_session_permission_mode(session)
                if perm_mode == "bypassPermissions":
                    pol, pol_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                    if pol == "parity_check":
                        if entrypoint == "claude-desktop":
                            print(
                                "[Claude Code IPC] Note: 目标 Claude Desktop 会话当前为 bypassPermissions，"
                                "在默认 parity_check 策略下外部消息会被暂留且桌面端无审核卡片（5 分钟过期）；"
                                "建议切为 Auto / Default / Accept Edits。",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                "[Claude Code IPC] Note: 目标会话当前为 bypassPermissions，"
                                "若消息被入站暂留，请在终端底部查看「待审核卡片」（Review it below）并批准。",
                                file=sys.stderr,
                            )
                    elif pol in ("hold", "refuse"):
                        for gline in _format_inbound_guidance(pol, pol_src, entrypoint=entrypoint, permission_mode=perm_mode):
                            print(gline, file=sys.stderr)
        else:
            print(f"[Claude Code IPC] Message successfully delivered to session PID {session['pid']} ({display_name}) [ID: {sess_id}] (Req-ID: {nonce}, Mode: {caller_mode}).")
            if expected_artifact:
                print(f"[Claude Code IPC] Handshake: Expected artifact registered -> {expected_artifact}", file=sys.stderr)
            if session.get("status") == "busy":
                print("[Claude Code IPC] Note: Target session is busy; the message will be processed after its current turn.", file=sys.stderr)
            elif session.get("status") == "waiting":
                wf = session.get("waiting_for") or "user input"
                print(f"[Claude Code IPC] Note: Target session is currently waiting ({wf}); check Claude Desktop to unblock it.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Claude Code IPC] Failed to send message to socket {sock_path}: {e}", file=sys.stderr)
        return False
    finally:
        if owned_listener is not None:
            owned_listener.close()


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


def _extract_ask_user_questions(content) -> list[dict]:
    """Extract AskUserQuestion tool_use blocks from an assistant message content array."""
    if not isinstance(content, list):
        return []
    asks = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name") == "AskUserQuestion":
            asks.append({
                "id": str(part.get("id") or ""),
                "input": part.get("input") if isinstance(part.get("input"), dict) else {},
            })
    return asks


def _extract_tool_result_ids(content) -> set[str]:
    """Extract tool_use_id values from a user message containing tool_result blocks."""
    if not isinstance(content, list):
        return set()
    ids: set[str] = set()
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_result" and part.get("tool_use_id"):
            ids.add(str(part["tool_use_id"]))
    return ids


def format_ask_user_question(tool_input: dict, intro_text: str = "") -> str:
    """Format an AskUserQuestion tool payload into clean readable text for callers and humans."""
    lines = ["[Claude Code Waiting For User Input — AskUserQuestion]"]
    if intro_text and intro_text.strip():
        lines.append("")
        lines.append(intro_text.strip())

    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if isinstance(questions, list) and questions:
        for idx, q in enumerate(questions, start=1):
            if not isinstance(q, dict):
                continue
            header = str(q.get("header") or "").strip()
            multi = bool(q.get("multiSelect"))
            mode_label = "多选" if multi else "单选"
            header_part = f" [{header}]" if header else ""
            lines.append("")
            lines.append(f"Question {idx}{header_part} ({mode_label}):")
            q_text = str(q.get("question") or "").strip()
            if q_text:
                lines.append(q_text)
            options = q.get("options")
            if isinstance(options, list):
                for opt_idx, opt in enumerate(options, start=1):
                    if isinstance(opt, dict):
                        label = str(opt.get("label") or "").strip()
                        desc = str(opt.get("description") or "").strip()
                        if desc:
                            lines.append(f"  {opt_idx}. {label} — {desc}")
                        elif label:
                            lines.append(f"  {opt_idx}. {label}")
                    elif isinstance(opt, str) and opt.strip():
                        lines.append(f"  {opt_idx}. {opt.strip()}")
    else:
        lines.append("")
        lines.append(json.dumps(tool_input, ensure_ascii=False, indent=2))

    return "\n".join(lines).strip()


def read_transcript_messages(transcript_path: str | None, last_n: int = 1, only_final: bool = True) -> list[dict]:
    """
    Parse JSONL transcript and extract assistant response turns.
    Skips non-conversation entries (such as api-request-blob) and surfaces any
    unanswered (pending) AskUserQuestion at the tail of the transcript so callers
    can see when Claude Code is paused waiting for user selection.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return []

    turns: list[dict] = []
    pending_asks: dict[str, dict] = {}
    msg_intro_texts: dict[str, list[str]] = {}
    ask_seq = 0
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    entry_type = entry.get("type")
                    if entry_type and entry_type not in ("assistant", "user"):
                        continue
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")

                    if role == "user":
                        content = msg.get("content")
                        result_ids = _extract_tool_result_ids(content)
                        for rid in result_ids:
                            pending_asks.pop(rid, None)
                        # A fresh plain user prompt cancels any prior unanswered card
                        has_plain_text = (
                            (isinstance(content, str) and bool(content.strip()))
                            or (
                                isinstance(content, list)
                                and any(isinstance(p, dict) and p.get("type") == "text" and str(p.get("text") or "").strip() for p in content)
                            )
                        )
                        if not entry.get("isMeta") and has_plain_text and not result_ids:
                            pending_asks.clear()
                        continue

                    if role != "assistant":
                        continue

                    stop_reason = msg.get("stop_reason")
                    content = msg.get("content")
                    texts = []
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                texts.append(part.get("text", ""))
                    elif isinstance(content, str):
                        texts.append(content)
                    full_text = "\n".join(texts).strip()

                    msg_id = msg.get("id")
                    asks = _extract_ask_user_questions(content)
                    if asks:
                        prior_intro = msg_intro_texts.pop(msg_id, []) if msg_id else []
                        combined_intro = "\n".join(prior_intro + ([full_text] if full_text else [])).strip()
                        for ask in asks:
                            ask_seq += 1
                            ask_key = ask["id"] or f"ask_{ask_seq}"
                            ask_turn = {
                                "timestamp": entry.get("timestamp"),
                                "text": format_ask_user_question(ask["input"], intro_text=combined_intro),
                                "id": msg_id,
                                "stop_reason": stop_reason,
                                "model": msg.get("model", ""),
                                "is_pending_question": False,
                            }
                            pending_asks[ask_key] = ask_turn
                            if not only_final:
                                turns.append(ask_turn)
                        continue

                    is_non_final = stop_reason in ("tool_use", None)
                    if is_non_final:
                        if full_text and msg_id:
                            msg_intro_texts.setdefault(msg_id, []).append(full_text)
                        if only_final:
                            continue

                    if full_text:
                        if not is_non_final:
                            pending_asks.clear()
                        turns.append({
                            "timestamp": entry.get("timestamp"),
                            "text": full_text,
                            "id": msg_id,
                            "stop_reason": stop_reason,
                            "model": msg.get("model", ""),
                        })
                except Exception:
                    continue
    except Exception as e:
        print(f"[Claude Code IPC] Error reading transcript {transcript_path}: {e}", file=sys.stderr)

    # Surface still-unanswered AskUserQuestion(s) at the tail of the transcript
    for ask_turn in pending_asks.values():
        ask_turn["is_pending_question"] = True
        if only_final:
            turns.append(ask_turn)

    return turns[-last_n:]


def read_latest_feedback(
    target: str | None = None,
    n: int = 1,
    only_final: bool = True,
    pick_latest: bool = False,
    allow_cross_project: bool | str = False,
) -> list[dict]:
    """
    Fetch the latest assistant turns from the target session.
    Automatically falls back to offline JSONL transcripts under ~/.claude/projects/
    when no active socket matches (e.g. after Claude Desktop hibernates idle processes).
    """
    candidates = get_active_sessions(exclude_self=False)
    session, status = (
        match_session(
            target=target,
            candidates=candidates,
            pick_latest=pick_latest,
            allow_cross_project=allow_cross_project,
            quiet_not_found=True,
        )
        if candidates
        else (None, "none")
    )

    if status in ("ambiguous", "invalid_target", "cross_project_rejected"):
        return []

    if not session:
        # Offline fallback: scan ~/.claude/projects/*/*.jsonl
        active_ids = {c["session_id"] for c in candidates if c.get("session_id")}
        current_cwd = os.getcwd()
        offline = get_offline_sessions(cwd=current_cwd, exclude_session_ids=active_ids)

        resolved_target, source_kind = resolve_target_session(target)
        if resolved_target and resolved_target not in active_ids:
            direct = find_transcript_path(current_cwd, resolved_target)
            if direct:
                meta = _extract_transcript_metadata(Path(direct))
                if meta and meta["session_id"] not in active_ids and not any(o.get("session_id") == meta["session_id"] for o in offline):
                    offline.insert(0, meta)

        session, status = (
            match_session(
                target=target,
                candidates=offline,
                pick_latest=pick_latest,
                allow_cross_project=allow_cross_project,
                quiet_not_found=False,
            )
            if offline
            else (None, "none")
        )
        if not session:
            if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
                if source_kind != "bindings_file" or not offline:
                    if source_kind == "bindings_file" and resolved_target:
                        print(f"[Claude Code IPC] Bound session '{resolved_target}' not found in active or offline transcripts.", file=sys.stderr)
                    else:
                        print(f"[Claude Code IPC] No matching active or offline session found for target '{target}'.", file=sys.stderr)
            return []

        sid = _preferred_session_id(session)
        title = session.get("name") or "Untitled"
        print(
            f"[Claude Code IPC] Notice: Session \"{title}\" [{sid}] is inactive/hibernated; reading transcript offline.",
            file=sys.stderr,
        )

    transcript_path = session.get("transcript_path")
    if not transcript_path:
        print(f"[Claude Code IPC] No transcript file found for session {session.get('pid') or _preferred_session_id(session)}.", file=sys.stderr)
        return []

    turns = read_transcript_messages(transcript_path, last_n=n, only_final=only_final)

    # Live waitingFor check: even if AskUserQuestion wasn't flushed yet (or another interactive prompt is active)
    if session.get("is_active", bool(session.get("pid"))):
        live_state = _read_live_session_state(session)
        if live_state:
            live_status = live_state.get("status", "")
            live_waiting = live_state.get("waiting_for", "")
        else:
            live_status = session.get("status", "")
            live_waiting = session.get("waiting_for", "")
        if live_status == "waiting" and live_waiting == "input needed":
            if not turns or not turns[-1].get("is_pending_question"):
                sid = _preferred_session_id(session)
                title = session.get("name") or sid
                turns.append({
                    "timestamp": None,
                    "text": (
                        f"[Claude Code Waiting For User Input]\n"
                        f"Session \"{title}\" [{sid}] is paused (status=waiting, waitingFor=\"input needed\").\n"
                        f"Please check Claude Desktop to answer the pending prompt."
                    ),
                    "id": None,
                    "stop_reason": "tool_use",
                    "model": "",
                    "is_pending_question": True,
                })
                turns = turns[-n:]

    return turns


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
    allow_cross_project: bool | str = False,
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Claude Code with request correlation nonce and wait synchronously.
    Uses multi-channel listening:
      1. Duplex reply channel: ephemeral socket ~/.agent-pair/run/reply-<nonce>.sock (+ mailbox)
      2. Fast-fail checks: target PID liveness & socket liveness
      3. Expected artifact auto-detection: checks if `expect_file` has been written and stabilized
      4. Incremental TailReader on the JSONL transcript with unconsumed fast-fail and
         AskUserQuestion / waitingFor="input needed" early-return protection

    Returns:
        tuple[response_text, verdict]
    """
    sender = sender or sender_name or detect_sender()
    try:
        session, status = _resolve_or_await_session(target, pick_latest, auto_await_new, activate_gui, await_timeout, allow_cross_project, debug)
    except NeedsNewSessionError:
        return None, "NEEDS_NEW_THREAD"

    if not session:
        if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
            _report_missing_session(target, debug=debug)
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
        reply_command=None,
        expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
        caller_cwd=current_cwd,
        target_cwd=session.get("cwd"),
        cwd_relation_type=rel,
    )

    receipt_listener = PeerReceiptListener(session["socket_path"]) if session.get("socket_path") else None

    try:
        if receipt_listener is not None:
            receipt_listener.start()

        ok = send_message_to_claude(
            full_message,
            target=_preferred_session_id(session) or str(session["pid"]),
            expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
            reply_socket=reply_sock_path,
            pick_latest=pick_latest,
            auto_await_new=False,
            activate_gui=False,
            allow_cross_project=allow_cross_project,
            debug=debug,
            receipt_listener=receipt_listener,
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
        was_held = False
        pending_ask: dict | None = None
        waiting_input_since: float | None = None
        msg_intro_texts: dict[str, list[str]] = {}
        tail_reader = TailReader(transcript_path, start_offset=initial_offset)
        reply_marker = f"reply --to {nonce}"
        target_sid = _preferred_session_id(session) or str(session.get("pid") or "")
        entrypoint = str(session.get("entrypoint") or "")
        session_name = str(session.get("name") or target_sid)

        def _process_receipts(receipts: list[dict]) -> bool:
            nonlocal is_enqueued, warned_idle, was_held
            for receipt in receipts:
                st = receipt.get("status")
                if st == "held":
                    was_held = True
                    warned_idle = True
                    pol, pol_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                    perm_mode, _ = detect_session_permission_mode(session)
                    for line in _format_receipt_event(
                        receipt,
                        nonce,
                        entrypoint,
                        session_name,
                        target_sid,
                        policy=pol,
                        policy_src=pol_src,
                        permission_mode=perm_mode,
                    ):
                        print(line, file=sys.stderr)
                    if activate_gui and entrypoint == "claude-desktop":
                        if activate_desktop_app("Claude", new_tab=False):
                            print("[Claude Code IPC] Brought Claude Desktop to front (`-g`) so you can switch permission mode.", file=sys.stderr)
                        else:
                            print("[Claude Code IPC] Could not bring Claude Desktop to front automatically.", file=sys.stderr)
                elif st == "delivered":
                    is_enqueued = True
                    for line in _format_receipt_event(receipt, nonce, entrypoint, session_name, target_sid):
                        print(line, file=sys.stderr)
                elif st in ("refused", "dropped", "denied", "expired"):
                    pol, pol_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                    for line in _format_receipt_event(
                        receipt,
                        nonce,
                        entrypoint,
                        session_name,
                        target_sid,
                        policy=pol,
                        policy_src=pol_src,
                    ):
                        print(line, file=sys.stderr)
                    return True
            return False

        if receipt_listener is not None and receipt_listener.socket_path:
            if _process_receipts(receipt_listener.poll(timeout=0.05)):
                return None, "QUEUED_UNCONSUMED"

        while time.time() - start_time < timeout:
            # Channel 1: duplex reply socket / mailbox (also serves as the loop's sleep)
            r_resp, r_verdict = reply_server.poll_response(timeout=0.5)
            if r_resp is not None:
                if debug:
                    print(f"[Claude Code IPC Debug] Response received via duplex channel ({reply_server.channel}).", file=sys.stderr)
                return r_resp, r_verdict

            if receipt_listener is not None and receipt_listener.socket_path:
                if _process_receipts(receipt_listener.poll(timeout=0.0)):
                    return None, "QUEUED_UNCONSUMED"

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
                        entry_type = entry.get("type")
                        if entry_type and entry_type not in ("user", "assistant"):
                            continue
                        msg = entry.get("message", {})
                        if not isinstance(msg, dict):
                            continue
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

                            if pending_ask:
                                result_ids = _extract_tool_result_ids(content_val)
                                if (pending_ask["id"] and pending_ask["id"] in result_ids) or (
                                    not entry.get("isMeta") and raw_content.strip() and not result_ids
                                ):
                                    pending_ask = None
                                    waiting_input_since = None

                            if nonce in raw_content:
                                is_enqueued = True
                                found_request = True
                                if debug:
                                    print(f"[Claude Code IPC Debug] Correlated user turn started for nonce {nonce}", file=sys.stderr)

                        # Step 2: Once correlated request is identified, track assistant tools and final completion
                        elif found_request and role == "assistant":
                            content_list = msg.get("content", [])
                            for c in content_list if isinstance(content_list, list) else []:
                                if isinstance(c, dict) and c.get("type") == "tool_use":
                                    if reply_marker in json.dumps(c.get("input", {}), ensure_ascii=False):
                                        reply_cmd_seen = True
                                    tid = c.get("id")
                                    tname = c.get("name")
                                    if show_progress and tid and tid not in seen_tool_ids:
                                        seen_tool_ids.add(tid)
                                        print(f"[Claude Code IPC] Running tool: {tname}...", file=sys.stderr)

                            msg_id = msg.get("id")
                            stop_reason = msg.get("stop_reason")
                            inline_texts = [
                                p.get("text", "")
                                for p in (content_list if isinstance(content_list, list) else [])
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            inline_text = "\n".join(inline_texts).strip()

                            asks = _extract_ask_user_questions(content_list)
                            if asks:
                                prior_intro = msg_intro_texts.pop(msg_id, []) if msg_id else []
                                combined_intro = "\n".join(prior_intro + ([inline_text] if inline_text else [])).strip()
                                latest_ask = asks[-1]
                                pending_ask = {
                                    "id": latest_ask["id"],
                                    "input": latest_ask["input"],
                                    "intro_text": combined_intro,
                                }
                            elif stop_reason in ("tool_use", None) and inline_text and msg_id:
                                msg_intro_texts.setdefault(msg_id, []).append(inline_text)

                            # Completion check
                            if stop_reason and stop_reason != "tool_use":
                                if inline_text:
                                    pending_ask = None
                                    text, verdict, channel = settle_duplex_reply(reply_server, inline_text, reply_cmd_seen)
                                    if debug:
                                        print(f"[Claude Code IPC Debug] Final answer taken from {channel}.", file=sys.stderr)
                                    return text, verdict

                    except Exception as parse_err:
                        if debug:
                            print(f"[Claude Code IPC Debug] Error parsing line: {parse_err}", file=sys.stderr)
                        continue

            # Early return if target session paused on AskUserQuestion
            if pending_ask is not None:
                formatted_q = format_ask_user_question(pending_ask["input"], intro_text=pending_ask.get("intro_text", ""))
                print(
                    f"\n[Claude Code IPC] Target session \"{session.get('name') or target_sid}\" [{target_sid}] is waiting for user input (AskUserQuestion).",
                    file=sys.stderr,
                )
                print(
                    f"👉 Please answer the selection card in Claude Desktop, then run `agent-pair-ipc claude -r -s {target_sid}` to read the final reply.",
                    file=sys.stderr,
                )
                return formatted_q, "NEEDS_USER_INPUT"

            # Secondary live-state check: status == "waiting" & waitingFor == "input needed"
            # Covers both our active turn (`found_request`) and a queued turn blocked by an earlier card (`is_enqueued`).
            if found_request or is_enqueued:
                live_state = _read_live_session_state(session)
                if live_state.get("status") == "waiting" and live_state.get("waiting_for") == "input needed":
                    if waiting_input_since is None:
                        waiting_input_since = time.time()
                    elif time.time() - waiting_input_since >= _WAITING_FLUSH_GRACE_SECONDS:
                        pending_turns = (
                            read_transcript_messages(transcript_path, last_n=1, only_final=True)
                            if transcript_path
                            else []
                        )
                        if pending_turns and pending_turns[-1].get("is_pending_question"):
                            waiting_msg = pending_turns[-1]["text"]
                        else:
                            waiting_msg = (
                                f"[Claude Code Waiting For User Input]\n"
                                f"Target session \"{session.get('name') or target_sid}\" [{target_sid}] is paused "
                                f"(status=waiting, waitingFor=\"input needed\").\n"
                                f"Please check Claude Desktop to answer the prompt, then run `agent-pair-ipc claude -r -s {target_sid}`."
                            )
                        if not found_request:
                            print(
                                f"\n[Claude Code IPC] Request is queued (Req-ID: {nonce}), but target session is blocked on an earlier pending prompt (waitingFor: input needed).",
                                file=sys.stderr,
                            )
                        else:
                            print(f"\n[Claude Code IPC] Target session entered waiting state (waitingFor: input needed).", file=sys.stderr)
                        return waiting_msg, "NEEDS_USER_INPUT"
                else:
                    waiting_input_since = None

            elapsed = time.time() - start_time

            # Fast-fail: Request was not even enqueued in transcript after fast_fail_timeout
            if not is_enqueued and fast_fail_timeout > 0 and elapsed > fast_fail_timeout:
                policy, policy_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                live_state = _read_live_session_state(session)
                perm_mode, _ = detect_session_permission_mode(session)
                print(f"\n[Claude Code IPC] 提示: 消息已通过 Socket 送达进程，但在 {int(fast_fail_timeout)} 秒内未进入工作队列 (Req-ID: {nonce})。", file=sys.stderr)
                if was_held:
                    print(f"[Claude Code IPC] Delivery status: HELD (Req-ID: {nonce}) — 对端入站网关已确认暂留该消息，切勿盲目重发（5 分钟内切出 bypassPermissions 或批准后会自动放行）。", file=sys.stderr)
                else:
                    print(f"[Claude Code IPC] 这通常是因为目标会话的安全策略正在等待人工审核或模式对齐（入站扣留），或者该会话当前处于挂起/等待用户输入状态。", file=sys.stderr)
                print(f"[Claude Code IPC] 目标会话: \"{session.get('name')}\" [ID: {target_sid}, PID: {session['pid']}]", file=sys.stderr)
                if live_state.get("status") == "waiting":
                    wf = live_state.get("waiting_for") or "user input"
                    print(f"[Claude Code IPC] ⚠️ 检测到目标会话当前处于 waiting 状态 (waitingFor: {wf})，请先在 Claude Desktop 处理当前待选卡片/弹窗。", file=sys.stderr)
                print("\n👉 排查建议（按安全推荐顺序）：", file=sys.stderr)
                for gline in _format_inbound_guidance(
                    policy,
                    policy_src,
                    entrypoint=entrypoint,
                    permission_mode=perm_mode,
                ):
                    print(gline, file=sys.stderr)
                print(f"  • 如稍晚切换模式或批准后对方完成答复，可使用 `agent-pair-ipc claude -r -s {target_sid}` 或 `agent-pair-ipc inbox {nonce}` 取回。\n", file=sys.stderr)
                return None, "QUEUED_UNCONSUMED"

            # Inform if enqueued and waiting for busy previous turn
            if is_enqueued and not found_request and not warned_busy and elapsed > 10.0:
                warned_busy = True
                live_state = _read_live_session_state(session)
                if live_state.get("status") == "waiting":
                    wf = live_state.get("waiting_for") or "user input"
                    print(
                        f"[Claude Code IPC] Notice: Message is queued, but target session is paused in 'waiting' state ({wf}) on an earlier turn. Check Claude Desktop.",
                        file=sys.stderr,
                    )
                else:
                    print(f"[Claude Code IPC] Notice: Message is safely queued in session. Peer is busy processing previous turn; waiting for turn to start...", file=sys.stderr)

            if not is_enqueued and elapsed > 25.0 and not warned_delivery:
                warned_delivery = True
                print(f"[Claude Code IPC] Notice: Request nonce {nonce} has not appeared in transcript after 25s. Peer session may be busy or waiting for mode/approval.", file=sys.stderr)

            if not warned_idle and elapsed > _IDLE_WARN_SECONDS and not is_enqueued:
                warned_idle = True
                live_state = _read_live_session_state(session)
                policy, policy_src = resolve_cross_session_inbound_policy(session.get("cwd"))
                perm_mode, _ = detect_session_permission_mode(session)
                if live_state.get("status") == "waiting":
                    wf = live_state.get("waiting_for") or "user input"
                    print(
                        f"[Claude Code IPC] Notice: Target session is currently in 'waiting' state ({wf}). Check Claude Desktop to unblock it.",
                        file=sys.stderr,
                    )
                elif policy == "refuse":
                    print(
                        f"[Claude Code IPC] Warning: Inferred crossSessionInbound policy is 'refuse' ({policy_src}); inbound message will likely be rejected.",
                        file=sys.stderr,
                    )
                elif entrypoint == "claude-desktop":
                    mode_hint = f" (当前权限模式: {perm_mode})" if perm_mode else ""
                    tip_gui = " (pass `-g` to bring Claude Desktop to front automatically)" if not activate_gui else ""
                    print(
                        f"[Claude Code IPC] Notice: No enqueue event after {int(_IDLE_WARN_SECONDS)}s (inferred policy: {policy}{mode_hint}). "
                        f"Claude Desktop has no inbound review card UI; if session \"{session_name}\" is in bypassPermissions, switch it to Auto / Default / Accept Edits to release the message{tip_gui}.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[Claude Code IPC] Notice: No enqueue event after {int(_IDLE_WARN_SECONDS)}s (inferred policy: {policy}). "
                        f"Check for a pending inbound review card at the bottom of session \"{session_name}\".",
                        file=sys.stderr,
                    )
                if activate_gui and entrypoint == "claude-desktop" and policy != "refuse":
                    if activate_desktop_app("Claude", new_tab=False):
                        print("[Claude Code IPC] Brought Claude Desktop to front (`-g`) so you can switch permission mode or check session state.", file=sys.stderr)
                    else:
                        print("[Claude Code IPC] Could not bring Claude Desktop to front automatically.", file=sys.stderr)

        print(f"[Claude Code IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
        print(f"[Claude Code IPC] A late reply will be kept in the mailbox: agent-pair-ipc inbox {nonce}", file=sys.stderr)
        return None, None
    finally:
        if receipt_listener is not None:
            receipt_listener.close()
        reply_server.close()


def list_sessions(candidates: list[dict] | None = None, debug: bool = False):
    """Print active connectable sessions and recent hibernated sessions for the current project."""
    explicit_candidates = candidates is not None
    if candidates is None:
        candidates = get_active_sessions(exclude_self=False, debug=debug)

    current_cwd = os.getcwd()
    self_ids = get_self_session_identifiers()
    bound_id, bound_src = resolve_target_session(None, cwd=current_cwd)
    bound_id = bound_id if bound_src == "bindings_file" else None

    active_session_ids: set[str] = set()
    if not candidates:
        print("No active Claude Code sessions found.")
    else:
        print(f"Found {len(candidates)} active Claude Code session(s):\n")
        print(f"{'PID':<7} {'KIND':<30} {'STATUS':<18} {'SESSION_ID':<38} {'NAME / TITLE'}")
        print("-" * 125)
        for c in candidates:
            sid = _preferred_session_id(c)
            if c.get("session_id"):
                active_session_ids.add(c["session_id"])
            kind_entry = f"{c['kind']} ({c['entrypoint']})" if c.get("entrypoint") else c.get("kind", "unknown")
            is_self = " (Current Session)" if self_ids["pid"] and str(c["pid"]) == str(self_ids["pid"]) else ""
            relation = cwd_relation(c.get("cwd"), current_cwd)
            is_cwd = " [Current Project]" if relation == "exact" else (" [Linked Project]" if relation == "linked" else (" [Parent of CWD]" if relation == "ancestor" else ""))
            is_bound = " [Bound]" if bound_id and bound_id in (c.get("session_id"), c.get("host_session_id")) else ""
            status = c.get("status") or "-"
            if status == "waiting" and c.get("waiting_for"):
                status = f"waiting ({c['waiting_for']})"
            print(f"{c['pid']:<7} {kind_entry:<30} {status:<18} {sid:<38} {c.get('name', '')}{is_self}{is_cwd}{is_bound}")
            if c.get("host_session_id"):
                print(f"        └─ Host ID:    {c['host_session_id']}")
            perm_mode, _ = detect_session_permission_mode(c)
            if perm_mode:
                mode_note = ""
                if c.get("entrypoint") == "claude-desktop" and perm_mode == "bypassPermissions":
                    mode_note = " (⚠️ Desktop bypassPermissions holds external messages under default parity_check)"
                print(f"        └─ Mode:       {perm_mode}{mode_note}")
            print(f"        └─ Socket:     {c['socket_path']}")
            print(f"        └─ CWD:        {c['cwd']}")
            print(f"        └─ Transcript: {c['transcript_path'] or '(not found)'}\n")

    if not explicit_candidates:
        offline = get_offline_sessions(cwd=current_cwd, exclude_session_ids=active_session_ids, limit=0, debug=debug)
        proj_hibernated = filter_project_sessions(offline, current_cwd)
        if proj_hibernated:
            shown = proj_hibernated[:5]
            print(f"\nRecent hibernated Claude Code session(s) for current project ({len(shown)} of {len(proj_hibernated)}):\n")
            print(f"{'LAST ACTIVE':<20} {'SESSION_ID':<38} {'NAME / TITLE'}")
            print("-" * 95)
            for h in shown:
                ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h.get("mtime", 0.0))) if h.get("mtime") else "unknown"
                is_bound = " [Bound]" if bound_id and bound_id == h.get("session_id") else ""
                print(f"{ts_str:<20} {h['session_id']:<38} {h.get('name') or 'Untitled'}{is_bound}")
            print("\nTip: Hibernated sessions have no active socket. Click one in Claude Desktop to wake it up,")
            print("     or read its latest reply offline with: agent-pair-ipc claude -r -s <session_id>")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-ipc",
        description="Claude Code IPC Communication Utility\nEnables programmatic messaging and response retrieval with active Claude Code sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # 1. List active sessions (and recent hibernated sessions for current project):
  claude-ipc --list

  # 2. Bind / unbind project session (resolves to persistent sessionId):
  claude-ipc --bind <session_id|title|pid>
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

  # 7. Read latest assistant response (automatically falls back offline if hibernated):
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
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target session (resolved to persistent sessionId) to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project session binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest session if multiple match current project CWD")
    parser.add_argument("--await-new-session", "--await-new-thread", dest="await_new_session", action="store_true", help="Wait for a new or newly woken Claude Code session for this project, auto-bind and send (works in non-TTY)")
    parser.add_argument("--await-timeout", type=float, default=DEFAULT_AWAIT_TIMEOUT, help=f"Seconds to wait for a new session (default: {int(DEFAULT_AWAIT_TIMEOUT)})")
    parser.add_argument("--no-auto-new", action="store_true", help="Disable automatic wait-for-new-session when no session exists for project")
    parser.add_argument("-l", "--list", action="store_true", help="List active Claude Code sessions and recent hibernated sessions for current project")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from the target session (supports offline fallback)")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate tool-use turns when reading transcript (default: final turns only)")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-g", "--activate-gui", action="store_true", help="Bring Claude Desktop to front (triggers File > New Session with --await-new-session, or brings Desktop forward during -w when mode switch/attention is needed)")
    parser.add_argument("--intent", default="Read-Only Review", help="Peer cooperation intent declaration (default: 'Read-Only Review')")
    parser.add_argument("--user-context", default=None, help="User prompt authorization context to pass in peer envelope")
    parser.add_argument("--sender", default=None, help="Sender name in the peer envelope (default: $AGENT_PAIR_SENDER or auto-detected)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Claude Code response")
    parser.add_argument("-t", "--timeout", "--wait-timeout", dest="timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--expect-file", "--output-file", dest="expect_file", default=None, help="Expected deliverable file path. Auto-detects completion when file settles with verdict.")
    parser.add_argument("--fast-fail-timeout", type=float, default=45.0, help="Fast fail timeout in seconds if request is not even enqueued (default: 45.0, 0 to disable)")
    parser.add_argument("--doctor", "--check-config", dest="doctor", action="store_true", help="Diagnose local Claude Code cross-session inbound policy cascade (strictly read-only)")
    parser.add_argument("--allow-cross-project", dest="allow_cross_project", metavar="TARGET_DIR", default=None, help="Explicitly allow cross-project message delivery. Must specify target project path, e.g. --allow-cross-project=/path/to/project")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.doctor:
        policy, src = resolve_cross_session_inbound_policy(target_cwd=os.getcwd())
        print("=== Claude Code 跨会话入站策略只读诊断 ===")
        print(f"当前推断生效策略: {policy}")
        print(f"判定来源: {src}")
        print("\n策略说明:")
        print("  • parity_check (默认): 发送方与目标会话模式对齐时放行；bypassPermissions 会话会触发入站暂留（CLI 终端底部显示审核卡片；Claude Desktop 无审核卡片 UI，需切为 Auto/Default/Accept Edits，否则 5 分钟后过期）。")
        print("  • accept: 无条件接收入站消息（注意: 仅在用户全局配置生效，对 bypass 会话将免确认执行）。")
        print("  • hold: 所有入站消息均暂缓扣留待审。")
        print("  • refuse: 拒绝所有入站跨会话消息。")
        print("\n配置与排查建议（只读诊断，本工具绝不自动改写 settings.json）:")
        for gline in _format_inbound_guidance(policy, src, include_summary=False):
            print(gline)
        return EXIT_SIGN_OFF

    if args.bind_target:
        resolved_id, note = resolve_bind_target(args.bind_target, debug=args.debug)
        if not resolved_id:
            return EXIT_ERROR
        bfile = save_project_binding("claude", resolved_id)
        if resolved_id != args.bind_target.strip():
            id_kind = "ephemeral hostSessionId (no sessionId available)" if resolved_id.startswith("local_") else "persistent sessionId"
            print(f"[Claude Code IPC] Resolved '{args.bind_target}' ({note}) -> {id_kind} '{resolved_id}'.")
        print(f"[Claude Code IPC] Bound Claude Code session '{resolved_id}' to {bfile}")
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
        feedback = read_latest_feedback(
            target=target,
            n=args.number,
            only_final=not args.all_turns,
            pick_latest=args.pick_latest,
            allow_cross_project=args.allow_cross_project,
        )
        if not feedback:
            return EXIT_ERROR
        has_pending_input = bool(feedback[-1].get("is_pending_question"))
        for i, turn in enumerate(feedback):
            if len(feedback) > 1:
                print(f"--- Turn {i+1} [{turn.get('timestamp')}] ---")
            print(turn["text"])
        if has_pending_input:
            print(
                "\n[Claude Code IPC] Note: Target session is waiting for user input. "
                "Please answer in Claude Desktop, then re-run `-r`.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_USER_INPUT
        return EXIT_SIGN_OFF

    if args.await_new_session:
        try:
            new_sess = await_new_session(timeout=args.await_timeout, require_tty=False, activate_gui=args.activate_gui, debug=args.debug)
            if not new_sess:
                return EXIT_ERROR
            target = _preferred_session_id(new_sess) or str(new_sess["pid"])
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
            allow_cross_project=args.allow_cross_project,
            debug=args.debug,
        )
        if verdict == "QUEUED_UNCONSUMED":
            return EXIT_QUEUED_UNCONSUMED
        if verdict == "NEEDS_NEW_THREAD":
            return EXIT_NEEDS_NEW_THREAD
        if verdict == "NEEDS_USER_INPUT":
            if resp is not None:
                print("\n=== [Claude Code Waiting For User Input] ===")
                print(resp)
            return EXIT_NEEDS_USER_INPUT
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
            allow_cross_project=args.allow_cross_project,
            debug=args.debug,
        )
        return EXIT_SIGN_OFF if ok else EXIT_ERROR
    except NeedsNewSessionError:
        return EXIT_NEEDS_NEW_THREAD


if __name__ == "__main__":
    sys.exit(main())
