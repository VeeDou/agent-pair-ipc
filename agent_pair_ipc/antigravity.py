"""
Antigravity IPC Communication Utility.
Enables programmatic two-way messaging, live activity tracking, and response retrieval
with active Antigravity Desktop sessions via `agentapi`, SQLite conversation discovery,
the duplex reply channel (reply socket + mailbox), and JSONL transcript monitoring.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
from contextlib import ExitStack, contextmanager
import urllib.parse
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
    spill_enveloped_message,
    unbind_project_binding,
    warn_inline_rich_text,
)

DEFAULT_AWAIT_TIMEOUT = 120.0

_CALLER_CONTEXT_ENV_VARS = (
    "ANTIGRAVITY_PROJECT_ID",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_TRAJECTORY_ID",
    "ANTIGRAVITY_SOURCE_METADATA",
)


def get_app_data_dir() -> Path:
    """Resolve Antigravity's local app data directory."""
    override = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gemini" / "antigravity"


def get_db_path() -> Path:
    return get_app_data_dir() / "conversation_summaries.db"


def get_brain_dir() -> Path:
    return get_app_data_dir() / "brain"


def get_daemon_dir() -> Path:
    return get_app_data_dir() / "daemon"


def find_agentapi_command() -> list[str] | None:
    """Locate the `agentapi` CLI wrapper or `language_server agentapi` fallback."""
    which_bin = shutil.which("agentapi")
    if which_bin:
        return [which_bin]

    local_bin = get_app_data_dir() / "bin" / "agentapi"
    if local_bin.is_file():
        return [str(local_bin)]

    home_bin = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"
    if home_bin.is_file():
        return [str(home_bin)]

    ls_env = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "").strip()
    if ls_env and os.path.isfile(ls_env):
        return [ls_env, "agentapi"]

    app_ls = Path("/Applications/Antigravity.app/Contents/Resources/bin/language_server")
    if app_ls.is_file():
        return [str(app_ls), "agentapi"]

    return None


def discover_active_daemons(daemon_dir: Path | None = None) -> list[dict]:
    """Scan daemon metadata files (`hub_*.json`) for an alive Language Server process."""
    d_dir = daemon_dir or get_daemon_dir()
    if not d_dir.is_dir():
        return []

    candidates: list[tuple[float, Path]] = []
    for p in d_dir.glob("hub_*.json"):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)

    daemons = []
    for _, p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            http_port = data.get("httpPort")
            csrf_token = data.get("csrfToken")
            if pid > 0 and http_port and csrf_token and is_pid_alive(pid):
                daemons.append(data)
        except Exception:
            continue
    return daemons


def discover_active_daemon(daemon_dir: Path | None = None) -> dict | None:
    daemons = discover_active_daemons(daemon_dir)
    return daemons[0] if daemons else None


def route_session_daemons(command: list[str], session_id: str) -> list[dict | None]:
    """Probe without sending; metadata visibility is only a hint, not ownership proof."""
    if os.environ.get("ANTIGRAVITY_LS_ADDRESS"):
        return [None]  # Explicit endpoint is authoritative; never silently override it.
    daemons = discover_active_daemons()
    visible = []
    for daemon in daemons:
        try:
            result = subprocess.run(
                [*command, "get-conversation-metadata", session_id],
                env=build_agentapi_env(daemon), capture_output=True, text=True, timeout=3.0,
            )
            data = json.loads(result.stdout or "{}")
            if result.returncode == 0 and not data.get("error") and data.get("response"):
                visible.append(daemon)
        except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
            continue
    return visible + [d for d in daemons if d not in visible]


@contextmanager
def conversation_database(path: Path):
    """Read live WAL normally; use a validated copy if SQLite cannot open its SHM."""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        conn = None
    if conn is not None:
        try:
            yield conn
        finally:
            conn.close()
        return
    # Never use immutable=1 on the original: it silently ignores live WAL rows.
    with tempfile.TemporaryDirectory(prefix="antigravity-db-") as tmp:
        dest = Path(tmp) / "conversations.db"
        for attempt in range(3):
            wal = Path(str(path) + "-wal")
            def signature():
                return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
                        for p in (path, wal)]
            before = signature()
            shutil.copyfile(path, dest)
            copied_wal = Path(str(dest) + "-wal")
            copied_wal.unlink(missing_ok=True)
            if wal.exists():
                shutil.copyfile(wal, copied_wal)
            if before == signature():
                break
        else:
            raise sqlite3.OperationalError("Conversation database changed during snapshot; retry")
        conn = sqlite3.connect(str(dest))
        try:
            yield conn
        finally:
            conn.close()


def build_agentapi_env(daemon_info: dict | None = None) -> dict[str, str]:
    """
    Prepare subprocess environment for `agentapi`.
    Strips caller-scoped Antigravity metadata (notably `ANTIGRAVITY_PROJECT_ID`) so
    cross-project or external invocations do not fail with RPC PermissionDenied.
    """
    env = os.environ.copy()
    for key in _CALLER_CONTEXT_ENV_VARS:
        env.pop(key, None)

    if daemon_info:
        http_port = daemon_info.get("httpPort")
        csrf_token = daemon_info.get("csrfToken")
        if http_port:
            env["ANTIGRAVITY_LS_ADDRESS"] = f"localhost:{http_port}"
        if csrf_token:
            env["ANTIGRAVITY_CSRF_TOKEN"] = str(csrf_token)

    return env


def _parse_workspace_uris(raw_uris: str | None) -> list[str]:
    """Decode JSON list of `file://...` workspace URIs into local directory paths."""
    if not raw_uris:
        return []
    try:
        items = json.loads(raw_uris)
        if not isinstance(items, list):
            return []
    except Exception:
        return []

    paths: list[str] = []
    for uri in items:
        if not isinstance(uri, str) or not uri.strip():
            continue
        parsed = urllib.parse.urlparse(uri.strip())
        if parsed.scheme in ("file", ""):
            decoded = urllib.parse.unquote(parsed.path)
            if len(decoded) > 1 and decoded.endswith("/"):
                decoded = decoded.rstrip("/")
            if decoded and decoded not in paths:
                paths.append(decoded)
    return paths


def _best_cwd_for_session(cwds: list[str], reference_cwd: str | None = None) -> str | None:
    """Pick the workspace path with the strongest CWD relation to `reference_cwd`."""
    if not cwds:
        return None
    ref = reference_cwd or os.getcwd()
    rank = {"exact": 3, "linked": 2, "ancestor": 1, None: 0}
    best = cwds[0]
    best_score = -1
    for path in cwds:
        score = rank.get(cwd_relation(path, ref), 0)
        if score > best_score:
            best_score = score
            best = path
    return best


def _parse_iso_timestamp(ts_str: str | None) -> float:
    """Convert SQLite ISO timestamp to UNIX epoch seconds, returning 0.0 on sentinel/invalid values."""
    if not ts_str or ts_str.startswith("0001-01-01"):
        return 0.0
    try:
        norm = ts_str.strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def find_transcript_path(session_id: str) -> Path | None:
    """Locate `transcript.jsonl` for an Antigravity conversation ID."""
    if not session_id:
        return None
    p = get_brain_dir() / session_id / ".system_generated" / "logs" / "transcript.jsonl"
    return p if p.is_file() else None


def _read_full_step_content(transcript_path: str | Path, step_index: int) -> str | None:
    """Read untruncated `content` for `step_index` from sibling `transcript_full.jsonl`."""
    full_path = Path(transcript_path).parent / "transcript_full.jsonl"
    if not full_path.is_file():
        return None
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("step_index") == step_index:
                        content = entry.get("content")
                        return content if isinstance(content, str) else None
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _extract_entry_content(entry: dict, transcript_path: str | Path | None) -> str:
    """Extract full response text, falling back to `transcript_full.jsonl` when truncated."""
    content = entry.get("content") or ""
    truncated = entry.get("truncated_fields") or []
    if "content" in truncated and transcript_path and entry.get("step_index") is not None:
        full = _read_full_step_content(transcript_path, int(entry["step_index"]))
        if full is not None:
            return full
    return str(content)


def get_all_sessions(limit: int = 50, debug: bool = False, reference_cwd: str | None = None) -> list[dict]:
    """Query `conversation_summaries.db` for top-level non-killed Antigravity conversations."""
    db_path = get_db_path()
    if not db_path.is_file():
        return []

    sessions: list[dict] = []
    conn = None
    db_context = ExitStack()
    try:
        conn = db_context.enter_context(conversation_database(db_path))
        cur = conn.cursor()
        cur.execute(
            """
            SELECT conversation_id, title, workspace_uris, status, not_fully_idle, killed,
                   step_count, last_modified_time, last_user_input_time, project_id
            FROM conversation_summaries
            WHERE (killed = 0 OR killed IS NULL)
              AND (parent_conversation_id = '' OR parent_conversation_id IS NULL)
              AND (nesting_depth = 0 OR nesting_depth IS NULL)
            ORDER BY last_modified_time DESC
            LIMIT ?
            """,
            (limit,),
        )
        ref_cwd = reference_cwd or os.getcwd()
        for row in cur.fetchall():
            cid, title, raw_uris, raw_status, not_fully_idle, _killed, step_count, last_mod, last_user, project_id = row
            if not cid:
                continue
            cwds = _parse_workspace_uris(raw_uris)
            best_cwd = _best_cwd_for_session(cwds, ref_cwd)
            tpath = find_transcript_path(cid)
            mtime = _parse_iso_timestamp(last_mod)
            if tpath:
                try:
                    mtime = max(mtime, tpath.stat().st_mtime)
                except OSError:
                    pass
            is_busy = (raw_status == "CASCADE_RUN_STATUS_RUNNING") or bool(not_fully_idle)
            sessions.append({
                "id": cid,
                "name": title or cid,
                "cwds": cwds,
                "cwd": best_cwd,
                "status": "busy" if is_busy else "idle",
                "raw_status": raw_status or "",
                "not_fully_idle": bool(not_fully_idle),
                "step_count": int(step_count or 0),
                "updated_at": last_mod or last_user or "",
                "project_id": project_id or "",
                "jsonl_path": str(tpath) if tpath else "",
                "mtime": mtime,
            })
    except Exception as e:
        if debug:
            print(f"[Antigravity IPC Debug] Error reading {db_path}: {e}", file=sys.stderr)
    finally:
        if conn is not None:
            try:
                db_context.close()
            except Exception:
                pass

    return sessions


def resolve_target_session(target: str | None = None, cwd: str | None = None) -> tuple[str | None, str]:
    """
    Resolve target session through explicit binding priority chain:
      1. Explicit CLI argument / flag (`-s <id_or_title>`)
      2. Environment variable `AGENT_PAIR_ANTIGRAVITY_SESSION`
      3. Project binding file `.agent-pair/bindings.json`
      4. Auto-match fallback (returns None, "auto")
    """
    if target and target.strip():
        return target.strip(), "cli_flag"

    env_target = os.environ.get("AGENT_PAIR_ANTIGRAVITY_SESSION")
    if env_target and env_target.strip():
        return env_target.strip(), "env_var"

    bindings = load_project_bindings(cwd)
    bound = bindings.get("antigravity") or bindings.get("antigravity_session")
    if bound and bound.strip():
        return bound.strip(), "bindings_file"

    return None, "auto"


def _session_cwd_relation(session: dict, current_cwd: str) -> tuple[str | None, str | None]:
    """Evaluate best CWD relation across all workspace URIs attached to a session."""
    cwds = session.get("cwds") or ([session["cwd"]] if session.get("cwd") else [])
    rank = {"exact": 3, "linked": 2, "ancestor": 1, None: 0}
    best_rel: str | None = None
    best_cwd: str | None = session.get("cwd")
    best_score = 0
    for cpath in cwds:
        rel = cwd_relation(cpath, current_cwd)
        score = rank.get(rel, 0)
        if score > best_score:
            best_score = score
            best_rel = rel
            best_cwd = cpath
    return best_rel, best_cwd


def match_session(
    target: str | None = None,
    candidates: list[dict] | None = None,
    allow_cwd_fallback: bool = True,
    pick_latest: bool = False,
    allow_cross_project: bool | str = False,
    debug: bool = False,
) -> tuple[dict | None, str]:
    """
    Match an Antigravity session by ID, ID prefix (>=6), exact/substring title, or CWD affinity.
    Enforces R2 binding validation, R3 path-guard, cross-project blocking, and multi-session disambiguation.
    """
    resolved_target, source_kind = resolve_target_session(target)
    current_cwd = os.getcwd()

    if candidates is None:
        candidates = get_all_sessions(limit=50, debug=debug, reference_cwd=current_cwd)

    if not candidates:
        return None, "none"

    if resolved_target:
        target_str = str(resolved_target).strip()
        matched = None

        for c in candidates:
            if c["id"] == target_str:
                matched = c
                break
        if not matched:
            for c in candidates:
                if c.get("name") == target_str:
                    matched = c
                    break

        if not matched and source_kind == "cli_flag":
            is_path_like = (
                target_str.startswith(("/", "~", "./", "../"))
                or os.path.isdir(os.path.expanduser(target_str))
            )
            if is_path_like:
                print(f"\n[Antigravity IPC] Error: Target session '{target_str}' looks like a filesystem path.", file=sys.stderr)
                print("👉 `-s / --session` requires a conversation ID or title.", file=sys.stderr)
                print("   To target a project directory, `cd` into that directory or configure project aliases in `.agent-pair/config.json`.\n", file=sys.stderr)
                return None, "invalid_target"

        if not matched and len(target_str) >= 6:
            prefix_matches = [c for c in candidates if c["id"].startswith(target_str)]
            if len(prefix_matches) == 1:
                matched = prefix_matches[0]
            elif len(prefix_matches) > 1:
                print(f"\n[Antigravity IPC] Error: Ambiguous ID prefix '{target_str}'! Matches multiple conversations.", file=sys.stderr)
                return None, "ambiguous"

        if not matched:
            name_matches = [c for c in candidates if target_str.lower() in (c.get("name") or "").lower()]
            if len(name_matches) == 1:
                matched = name_matches[0]
            elif len(name_matches) > 1:
                print(f"\n[Antigravity IPC] Error: Ambiguous target '{target_str}'! Matches multiple conversation titles:", file=sys.stderr)
                for m in name_matches:
                    print(f"  • [{m.get('id')}] \"{m.get('name')}\"", file=sys.stderr)
                print("Please specify an exact conversation ID with `-s`.\n", file=sys.stderr)
                return None, "ambiguous"

        if matched:
            relation, matched_cwd = _session_cwd_relation(matched, current_cwd)
            if matched_cwd:
                matched = dict(matched)
                matched["cwd"] = matched_cwd

            if source_kind in ("bindings_file", "env_var"):
                if relation is None:
                    src_label = ".agent-pair/bindings.json" if source_kind == "bindings_file" else "environment variable AGENT_PAIR_ANTIGRAVITY_SESSION"
                    print(f"\n[Antigravity IPC] ⚠️ Warning: 忽略 {src_label} 中的失效绑定！", file=sys.stderr)
                    print(f"   绑定的会话 [{target_str}] 运行在 '{matched.get('cwd')}'，与当前工程 '{current_cwd}' 无关。", file=sys.stderr)
                    print("   👉 自动回退到当前工程会话探测逻辑。可运行 `agent-pair-ipc antigravity --unbind` 清理。\n", file=sys.stderr)
                    resolved_target = None
                else:
                    return matched, "matched"
            else:
                if relation is None:
                    target_cwd = matched.get("cwd") or ""
                    title_info = matched.get("name") or matched.get("id")

                    if not allow_cross_project or allow_cross_project is True or allow_cross_project == "*":
                        print("\n[Antigravity IPC] ❌ Error: 拒绝跨工程串线投递！", file=sys.stderr)
                        print(f"   指定的目标会话 \"{title_info}\" 运行在 '{target_cwd}'，与当前工程 '{current_cwd}' 完全无关！", file=sys.stderr)
                        print("👉 为防止污染无关项目的上下文与聊天记录，工具默认严格阻断跨目录调用。", file=sys.stderr)
                        print("   如确实需要跨目录调用，请显式追加 `--allow-cross-project=<目标工程路径>` 明确确认目标工程路径。\n", file=sys.stderr)
                        return None, "cross_project_rejected"

                    norm_allowed = os.path.realpath(os.path.expanduser(str(allow_cross_project)))
                    target_cwds = matched.get("cwds") or ([target_cwd] if target_cwd else [])
                    norm_targets = {os.path.realpath(p) for p in target_cwds if p}
                    if norm_allowed not in norm_targets:
                        print(f"\n[Antigravity IPC] ❌ Error: `--allow-cross-project` 指定的目标工程路径 '{allow_cross_project}' 与目标会话实际运行路径 '{target_cwd}' 不一致！\n", file=sys.stderr)
                        return None, "cross_project_rejected"

                    print(f"[Antigravity IPC] ⚠️ NOTICE: 跨工程调用已显式允许。目标会话运行在 '{norm_allowed}'，与当前目录 '{current_cwd}' 不一致！", file=sys.stderr)
                return matched, "matched"

        if resolved_target:
            if source_kind == "bindings_file":
                print(f"\n[Antigravity IPC] Error: Bound session '{target_str}' in .agent-pair/bindings.json was not found.", file=sys.stderr)
                print("👉 The bound session may have been deleted or archived.", file=sys.stderr)
                print("   Run with `-s <session_id>` to target another session, or run with `--unbind`.\n", file=sys.stderr)
            return None, "none"

    if not allow_cwd_fallback:
        return None, "none"

    # Normalize each candidate's `cwd` to its best match for `current_cwd` before bucketing
    prepared: list[dict] = []
    for c in candidates:
        rel, best_cwd = _session_cwd_relation(c, current_cwd)
        if rel is not None:
            copy_c = dict(c)
            copy_c["cwd"] = best_cwd
            prepared.append(copy_c)

    cwd_matches = filter_project_sessions(prepared, current_cwd)
    if not cwd_matches:
        return None, "none"

    if len(cwd_matches) == 1:
        chosen = cwd_matches[0]
    elif pick_latest:
        cwd_matches.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
        chosen = cwd_matches[0]
        print(f"[Antigravity IPC] Notice: Multiple sessions found for this project. Using latest due to `--pick-latest`: \"{chosen.get('name')}\" [{chosen['id'][:8]}...]", file=sys.stderr)
    else:
        print(f"\n[Antigravity IPC] Error: Ambiguous sessions! Multiple Antigravity conversations found for directory '{current_cwd}':", file=sys.stderr)
        for m in cwd_matches:
            mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.get("mtime", 0.0))) if m.get("mtime") else m.get("updated_at", "unknown")
            print(f"  • [{m['id']}] \"{m.get('name', 'Untitled')}\" (Status: {m.get('status', 'idle')}, Updated: {mtime_str})", file=sys.stderr)
        print("\nPlease specify a target session with `-s <id_or_title>`, set AGENT_PAIR_ANTIGRAVITY_SESSION,", file=sys.stderr)
        print("save to .agent-pair/bindings.json, or pass `--pick-latest` to confirm.\n", file=sys.stderr)
        return None, "ambiguous"

    mtime = chosen.get("mtime", 0.0)
    if mtime and (time.time() - mtime > 1800.0):
        mins = int((time.time() - mtime) / 60)
        print(f"[Antigravity IPC] Warning: Target session \"{chosen.get('name')}\" [{chosen['id'][:8]}...] was last updated {mins} minutes ago.", file=sys.stderr)

    return chosen, "matched"


def await_new_session(
    timeout: float = DEFAULT_AWAIT_TIMEOUT,
    cwd: str | None = None,
    require_tty: bool = True,
    activate_gui: bool = False,
    debug: bool = False,
) -> dict | None:
    """
    Watch `conversation_summaries.db` for a newly created Antigravity conversation matching `cwd`.
    In non-interactive environments (non-TTY) when `require_tty=True`, raises `NeedsNewSessionError`.
    """
    target_cwd = cwd or os.getcwd()
    if require_tty and not sys.stderr.isatty():
        print(f"[Antigravity IPC] Notice: No existing Antigravity conversation found for project '{target_cwd}'.", file=sys.stderr)
        print("[Antigravity IPC] Cannot interactively wait for new session in non-interactive environment (non-TTY).", file=sys.stderr)
        print("👉 Open the project in Antigravity Desktop and start a conversation, then retry;", file=sys.stderr)
        print("   or run with `--await-new-session [--await-timeout N]` to wait explicitly, or `-s <conversation_id>`.", file=sys.stderr)
        raise NeedsNewSessionError(f"Non-TTY environment requires an explicit or pre-created Antigravity conversation for {target_cwd}")

    existing_ids = {s["id"] for s in get_all_sessions(limit=100, debug=debug, reference_cwd=target_cwd)}

    print(f"[Antigravity IPC] 当前工程尚未绑定活跃会话。正在等待新会话: '{target_cwd}'", file=sys.stderr)
    if activate_gui:
        if open_new_gui_session("antigravity"):
            print("[Antigravity IPC] 已唤起 Antigravity Desktop 并尝试触发新建会话。", file=sys.stderr)
        else:
            print("[Antigravity IPC] 未能自动唤起 Antigravity Desktop，请在桌面端手动新建对话。", file=sys.stderr)
    else:
        print("[Antigravity IPC] 请在 Antigravity Desktop 中打开本工程并新建对话。", file=sys.stderr)

    start_time = time.time()
    while time.time() - start_time < timeout:
        elapsed = int(time.time() - start_time)
        if sys.stderr.isatty():
            print(f"\r[Antigravity IPC] [侦测中 {elapsed:02d}s / {int(timeout)}s] 等待新会话诞生...", end="", file=sys.stderr, flush=True)

        current_sessions = get_all_sessions(limit=30, debug=debug, reference_cwd=target_cwd)
        new_candidates = []
        for s in current_sessions:
            if s["id"] not in existing_ids:
                rel, matched_cwd = _session_cwd_relation(s, target_cwd)
                if rel is not None:
                    s_copy = dict(s)
                    s_copy["cwd"] = matched_cwd
                    new_candidates.append(s_copy)

        if new_candidates:
            new_candidates.sort(key=lambda x: x.get("mtime", 0.0), reverse=True)
            chosen = new_candidates[0]
            if sys.stderr.isatty():
                print("", file=sys.stderr)
            save_project_binding("antigravity", chosen["id"], cwd=target_cwd)
            print(f"[Antigravity IPC] 监听到新会话诞生: [{chosen['id']}] \"{chosen.get('name')}\" -> 自动完成持久绑定！", file=sys.stderr)
            return chosen

        time.sleep(1.5)

    if sys.stderr.isatty():
        print("", file=sys.stderr)
    print(f"[Antigravity IPC] 等待新会话超时 ({int(timeout)}s)。", file=sys.stderr)
    return None


def read_transcript_messages(jsonl_path: str | None, last_n: int = 1, only_final: bool = True) -> list[dict]:
    """Parse Antigravity JSONL transcript and extract assistant response turns."""
    if not jsonl_path or not os.path.exists(jsonl_path):
        return []

    turns: list[dict] = []
    seen_steps: set[int | str] = set()

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("source") != "MODEL" or entry.get("type") != "PLANNER_RESPONSE":
                        continue
                    if entry.get("status") != "DONE":
                        continue

                    tool_calls = entry.get("tool_calls") or []
                    if only_final and tool_calls:
                        continue

                    text = _extract_entry_content(entry, jsonl_path).strip()
                    if not text:
                        continue

                    key = entry.get("step_index") if entry.get("step_index") is not None else text
                    if key not in seen_steps:
                        seen_steps.add(key)
                        turns.append({
                            "timestamp": entry.get("created_at"),
                            "step_index": entry.get("step_index"),
                            "text": text,
                            "type": "PLANNER_RESPONSE",
                        })
                except Exception:
                    continue
    except Exception as e:
        print(f"[Antigravity IPC] Error reading transcript {jsonl_path}: {e}", file=sys.stderr)

    return turns[-last_n:]


def read_latest_feedback(
    target: str | None = None,
    n: int = 1,
    only_final: bool = True,
    pick_latest: bool = False,
) -> list[dict]:
    """Fetch the latest assistant turns from target Antigravity conversation."""
    session, status = match_session(target=target, pick_latest=pick_latest)
    if not session:
        if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
            print(f"[Antigravity IPC] No matching session found for target '{target}'.", file=sys.stderr)
        return []

    jsonl_path = session.get("jsonl_path") or str(find_transcript_path(session["id"]) or "")
    if not jsonl_path:
        print(f"[Antigravity IPC] No transcript file found for conversation {session.get('id')}.", file=sys.stderr)
        return []

    return read_transcript_messages(jsonl_path, last_n=n, only_final=only_final)


def _report_missing_session(target: str | None) -> None:
    if target:
        print(f"[Antigravity IPC] Error: No matching conversation found for target '{target}'.", file=sys.stderr)
    else:
        print(f"[Antigravity IPC] Error: No Antigravity conversation matches current working directory '{os.getcwd()}'.", file=sys.stderr)
        print("Please specify a target with `-s <conversation_id>` or run `antigravity-ipc --list`.", file=sys.stderr)


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
    session, status = match_session(
        target=target,
        pick_latest=pick_latest,
        allow_cross_project=allow_cross_project,
        debug=debug,
    )
    if session or status in ("ambiguous", "invalid_target", "cross_project_rejected"):
        return session, status

    resolved_target, _ = resolve_target_session(target)
    if resolved_target is None and auto_await_new:
        session = await_new_session(
            timeout=await_timeout,
            cwd=os.getcwd(),
            activate_gui=activate_gui,
            debug=debug,
        )
        return session, ("matched" if session else "none")
    return None, status


def send_message_to_antigravity(
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
    title: str = "Agent Pair IPC",
    debug: bool = False,
) -> bool:
    """
    Inject a message into a living Antigravity conversation via `agentapi send-message`.
    Discovers the active Language Server daemon from `daemon/hub_*.json` so external
    processes (Claude Code, Codex, plain shells) can invoke `agentapi` directly.
    """
    session, status = _resolve_or_await_session(
        target, pick_latest, auto_await_new, activate_gui, await_timeout, allow_cross_project, debug
    )
    if not session:
        if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
            _report_missing_session(target)
        return False

    session_id = session["id"]
    display_name = session.get("name") or session_id

    if session.get("status") == "busy":
        print(f"[Antigravity IPC] Notice: Target conversation \"{display_name}\" [{session_id}] is currently busy; message will be queued as a high-priority system message.", file=sys.stderr)

    current_cwd = os.getcwd()
    rel = cwd_relation(session.get("cwd"), current_cwd)

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

    message, payload_path = spill_enveloped_message(message)
    if payload_path:
        print(f"[Antigravity IPC] Large payload moved to file-based pipeline: {payload_path}", file=sys.stderr)

    agentapi_cmd = find_agentapi_command()
    if not agentapi_cmd:
        print("[Antigravity IPC] Error: `agentapi` executable not found. Ensure Antigravity Desktop is installed.", file=sys.stderr)
        return False

    routes = route_session_daemons(agentapi_cmd, session_id)
    if not routes:
        print("[Antigravity IPC] Error: No active desktop Language Server found.", file=sys.stderr)
        return False

    cmd = [*agentapi_cmd, "send-message", f"--title={title}", session_id, message]
    try:
        for index, daemon in enumerate(routes):
            result = subprocess.run(cmd, env=build_agentapi_env(daemon), capture_output=True, text=True, timeout=15.0)
            try:
                parsed = json.loads(result.stdout or "{}")
                rpc_error = parsed.get("error", "") if isinstance(parsed, dict) else ""
            except ValueError:
                rpc_error = ""
            err_msg = str(rpc_error or result.stderr.strip() or
                          (result.stdout.strip() if result.returncode else ""))
            if not result.returncode and not rpc_error:
                break
            # Retry only an explicit rejection, never timeout/unknown delivery state.
            missing = any(marker in err_msg.lower() for marker in (
                "trajectory not found", "conversation not found",
            ))
            if missing and index + 1 < len(routes):
                if debug:
                    print("[Antigravity IPC Debug] Target absent on this daemon; trying next desktop instance.", file=sys.stderr)
                continue
            print(f"[Antigravity IPC] Delivery failed: {err_msg}", file=sys.stderr)
            if missing:
                print("[Antigravity IPC] No daemon accepted the target trajectory. Open the conversation in Desktop and retry; CLI conversations are separate.", file=sys.stderr)
            return False

        print(f"[Antigravity IPC] Message delivered to conversation \"{display_name}\" [{session_id}] (Req-ID: {nonce}).")
        if expected_artifact:
            print(f"[Antigravity IPC] Handshake: Expected artifact registered -> {expected_artifact}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Antigravity IPC] Failed to execute `agentapi send-message`: {e}", file=sys.stderr)
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
    allow_cross_project: bool | str = False,
    debug: bool = False,
) -> bool | tuple[str | None, str | None]:
    """Send file content directly to Antigravity without shell-escaping loss."""
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        print(f"[Antigravity IPC] Error: file not found: {file_path}", file=sys.stderr)
        return (None, None) if wait else False
    try:
        content = fpath.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"[Antigravity IPC] Error reading {file_path}: {e}", file=sys.stderr)
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
        allow_cross_project=allow_cross_project,
        debug=debug,
    )
    if wait:
        return send_and_wait(
            content,
            timeout=timeout,
            fast_fail_timeout=fast_fail_timeout,
            expect_file=expect_file,
            **common_kwargs,
        )
    return send_message_to_antigravity(
        content,
        expected_artifact=str(expect_file) if expect_file else None,
        **common_kwargs,
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
    allow_cross_project: bool | str = False,
    debug: bool = False,
) -> tuple[str | None, str | None]:
    """
    Send message to Antigravity with correlation nonce and wait synchronously.
    Multi-channel completion detection:
      1. Duplex reply channel: ephemeral socket ~/.agent-pair/run/reply-<nonce>.sock (+ mailbox)
      2. Expected deliverable auto-detection (`--expect-file`)
      3. Incremental TailReader on Antigravity `transcript.jsonl` (+ `transcript_full.jsonl`)
    """
    sender = sender or sender_name or detect_sender()
    try:
        session, status = _resolve_or_await_session(
            target, pick_latest, auto_await_new, activate_gui, await_timeout, allow_cross_project, debug
        )
    except NeedsNewSessionError:
        return None, "NEEDS_NEW_THREAD"

    if not session:
        if status not in ("ambiguous", "invalid_target", "cross_project_rejected"):
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

    jsonl_path = session.get("jsonl_path") or str(find_transcript_path(session["id"]) or "")
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
        on_progress=print_progress_frame("Antigravity IPC Activity") if show_progress else None,
    )
    reply_sock_path = None
    try:
        reply_sock_path = reply_server.start()
    except Exception as e:
        if debug:
            print(f"[Antigravity IPC Debug] Could not start ephemeral reply server (mailbox still active): {e}", file=sys.stderr)

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

    try:
        ok = send_message_to_antigravity(
            full_message,
            target=session["id"],
            expected_artifact=str(expect_path) if expect_path else (str(expect_file) if expect_file else None),
            reply_socket=reply_sock_path,
            pick_latest=pick_latest,
            auto_await_new=False,
            activate_gui=False,
            allow_cross_project=allow_cross_project,
            title=f"{sender}: {intent}",
            debug=debug,
        )
        if not ok:
            return None, None

        print(f"[Antigravity IPC] Waiting for Antigravity response (Req-ID: {nonce}, timeout: {int(timeout)}s)...", file=sys.stderr)
        start_time = time.time()
        seen_steps: set[int] = set()
        found_request = False
        reply_cmd_seen = False
        tail_reader = TailReader(jsonl_path, start_offset=initial_offset)
        reply_marker = f"reply --to {nonce}"

        while time.time() - start_time < timeout:
            # Channel 1: duplex reply socket / mailbox (also serves as poll interval)
            r_resp, r_verdict = reply_server.poll_response(timeout=0.5)
            if r_resp is not None:
                if debug:
                    print(f"[Antigravity IPC Debug] Response received via duplex channel ({reply_server.channel}).", file=sys.stderr)
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
                    print(f"\n[Antigravity IPC] Detected completed deliverable with verdict [{art_verdict}]: {expect_path} ({len(art_content)} bytes).", file=sys.stderr)
                    return art_content, art_verdict

            # Channel 3: Incremental JSONL transcript reading
            if not jsonl_path or not os.path.exists(jsonl_path):
                p = find_transcript_path(session["id"])
                if p:
                    jsonl_path = str(p)
                    session["jsonl_path"] = jsonl_path
                    tail_reader.set_file(jsonl_path, start_offset=0)
                else:
                    if (time.time() - start_time) > fast_fail_timeout:
                        print(f"\n[Antigravity IPC] Notice: Message sent but transcript was not initialized within {int(fast_fail_timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
                        return None, "QUEUED_UNCONSUMED"
                    continue

            elapsed = time.time() - start_time
            if not found_request and fast_fail_timeout > 0 and elapsed > fast_fail_timeout:
                print(f"\n[Antigravity IPC] Notice: Message is queued but was not consumed within {int(fast_fail_timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
                print(f"[Antigravity IPC] Target conversation \"{session.get('name')}\" [{session['id']}] might be busy or waiting for user approval.", file=sys.stderr)
                print(f"[Antigravity IPC] Action: Collect a later reply with `agent-pair-ipc inbox {nonce}` or `antigravity-ipc -r`.\n", file=sys.stderr)
                return None, "QUEUED_UNCONSUMED"

            new_lines = tail_reader.read_new_lines()
            for line in new_lines:
                try:
                    entry = json.loads(line)
                    if not found_request:
                        if nonce in line:
                            found_request = True
                            if debug:
                                print(f"[Antigravity IPC Debug] Detected correlated request with nonce {nonce}", file=sys.stderr)
                        continue

                    if entry.get("source") != "MODEL" or entry.get("type") != "PLANNER_RESPONSE":
                        continue
                    if entry.get("status") != "DONE":
                        continue

                    step_idx = entry.get("step_index")
                    tool_calls = entry.get("tool_calls") or []
                    if tool_calls:
                        if reply_marker in line:
                            reply_cmd_seen = True
                        if show_progress and (step_idx is None or step_idx not in seen_steps):
                            if step_idx is not None:
                                seen_steps.add(step_idx)
                            names = [str(tc.get("name") or "tool") for tc in tool_calls if isinstance(tc, dict)]
                            print(f"[Antigravity IPC Activity] Running tool: {', '.join(names) or 'tool'}...", file=sys.stderr)
                        continue

                    if entry.get("thinking") and show_progress and (step_idx is None or step_idx not in seen_steps):
                        if step_idx is not None:
                            seen_steps.add(step_idx)
                        print("[Antigravity IPC Activity] Reasoning in progress...", file=sys.stderr)

                    final_text = _extract_entry_content(entry, jsonl_path).strip()
                    if final_text:
                        text, verdict, channel = settle_duplex_reply(reply_server, final_text, reply_cmd_seen)
                        if debug:
                            print(f"[Antigravity IPC Debug] Final answer taken from {channel}.", file=sys.stderr)
                        return text, verdict
                except Exception as parse_err:
                    if debug:
                        print(f"[Antigravity IPC Debug] Parse error: {parse_err}", file=sys.stderr)
                    continue

        print(f"[Antigravity IPC] Timed out waiting for response after {int(timeout)}s (Req-ID: {nonce}).", file=sys.stderr)
        print(f"[Antigravity IPC] A late reply will be kept in the mailbox: agent-pair-ipc inbox {nonce}", file=sys.stderr)
        return None, None
    finally:
        reply_server.close()


def list_sessions(debug: bool = False):
    """List recent Antigravity conversations with CWD affinity and status metadata."""
    sessions = get_all_sessions(limit=30, debug=debug)
    if not sessions:
        print("No Antigravity conversations found in ~/.gemini/antigravity/conversation_summaries.db.")
        return

    current_cwd = os.getcwd()
    bound_id = load_project_bindings().get("antigravity")

    print(f"Found {len(sessions)} recent Antigravity conversation(s):\n")
    print(f"{'ID':<38} {'STATUS':<7} {'TITLE'}")
    print("-" * 95)
    for s in sessions:
        tags = []
        relation, matched_cwd = _session_cwd_relation(s, current_cwd)
        if relation == "exact":
            tags.append("Current Project")
        elif relation == "linked":
            tags.append("Linked Project")
        elif relation == "ancestor":
            tags.append("Parent of CWD")
        if bound_id and (s["id"] == bound_id or s["id"].startswith(bound_id)):
            tags.append("Bound Target")
        tag_str = f" [{', '.join(tags)}]" if tags else ""

        print(f"{s['id']:<38} {s.get('status', 'idle'):<7} {s.get('name', '')}{tag_str}")
        if s.get("updated_at"):
            print(f"        └─ Updated:    {s['updated_at']}")
        display_cwd = matched_cwd or s.get("cwd")
        if display_cwd:
            print(f"        └─ CWD:        {display_cwd}")
        if s.get("jsonl_path"):
            print(f"        └─ Transcript: {s['jsonl_path']}")
        print()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antigravity-ipc",
        description="Antigravity IPC Communication Utility\nEnables Claude Code, Codex, and local tools to message and pair with Antigravity Desktop sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # 1. List recent Antigravity conversations:
  antigravity-ipc --list

  # 2. Bind current project to a specific conversation:
  antigravity-ipc --bind 53eed744-a64b-4f7c-8009-3720c6c1e0a8

  # 3. Unbind project conversation:
  antigravity-ipc --unbind

  # 4. Send review request and wait synchronously:
  antigravity-ipc -w -f docs/reports/REVIEW_REQUEST.md

  # 5. Send rich text via quoted heredoc:
  antigravity-ipc -w -f - <<'AP_EOF'
  Please review `agent_pair_ipc/antigravity.py` and end with `VERDICT: SIGN_OFF`.
  AP_EOF

  # 6. Read latest response from Antigravity:
  antigravity-ipc -r
"""
    )
    parser.add_argument("message", nargs="?", default=None, help="Message text or target session if -m/-f/stdin is provided")
    parser.add_argument("target_pos", nargs="?", default=None, help="Target conversation ID or title (positional alternative to -s/--session)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Explicit message text to send")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="Path to file whose content should be sent ('-' reads stdin)")
    parser.add_argument("--prefix", dest="prefix", default=None, help="Optional text prefix to prepend before file/stdin content")
    parser.add_argument("-s", "--session", dest="session_target", default=None, help="Target conversation ID or title")
    parser.add_argument("--bind", dest="bind_target", default=None, help="Bind target conversation ID to current project in .agent-pair/bindings.json")
    parser.add_argument("--unbind", action="store_true", help="Remove the project Antigravity binding in .agent-pair/bindings.json")
    parser.add_argument("--pick-latest", action="store_true", help="Pick the latest conversation if multiple match current project CWD")
    parser.add_argument("--await-new-session", "--await-new-thread", dest="await_new_session", action="store_true", help="Wait for a new conversation to appear for current project")
    parser.add_argument("--await-timeout", type=float, default=DEFAULT_AWAIT_TIMEOUT, help=f"Seconds to wait for a new conversation (default: {int(DEFAULT_AWAIT_TIMEOUT)})")
    parser.add_argument("-l", "--list", action="store_true", help="List recent Antigravity conversations")
    parser.add_argument("-r", "--read", action="store_true", help="Read the latest assistant feedback/response from target conversation")
    parser.add_argument("-a", "--all-turns", action="store_true", help="Include intermediate tool-call turns when reading transcript")
    parser.add_argument("-n", "--number", type=int, default=1, help="Number of assistant turns to read (default: 1)")
    parser.add_argument("-g", "--activate-gui", action="store_true", help="Activate Antigravity Desktop and trigger new conversation when waiting")
    parser.add_argument("--intent", default="Read-Only Review", help="Peer cooperation intent declaration (default: 'Read-Only Review')")
    parser.add_argument("--user-context", default=None, help="User prompt authorization context to pass in peer envelope")
    parser.add_argument("--sender", default=None, help="Sender name in the peer envelope (default: $AGENT_PAIR_SENDER or auto-detected)")
    parser.add_argument("-w", "--wait", action="store_true", help="Send message and synchronously wait for Antigravity response")
    parser.add_argument("-t", "--timeout", "--wait-timeout", dest="timeout", type=float, default=300.0, help="Wait timeout in seconds (default: 300)")
    parser.add_argument("--expect-file", "--output-file", dest="expect_file", default=None, help="Expected deliverable file path")
    parser.add_argument("--fast-fail", type=float, default=15.0, help="Fast fail timeout in seconds if nonce not consumed (default: 15.0)")
    parser.add_argument("--no-auto-new", action="store_true", help="Disable automatic wait-for-new-session when no conversation exists for project")
    parser.add_argument("--allow-cross-project", dest="allow_cross_project", metavar="TARGET_DIR", default=None, help="Explicitly allow cross-project delivery to TARGET_DIR")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.bind_target:
        bfile = save_project_binding("antigravity", args.bind_target)
        print(f"[Antigravity IPC] Bound Antigravity conversation '{args.bind_target}' to {bfile}")
        return EXIT_SIGN_OFF

    if args.unbind:
        if unbind_project_binding("antigravity"):
            print("[Antigravity IPC] Removed Antigravity binding from .agent-pair/bindings.json.")
        else:
            print("[Antigravity IPC] No active Antigravity binding found to remove.")
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
        )
        if not feedback:
            return EXIT_ERROR
        for i, turn in enumerate(feedback):
            if len(feedback) > 1:
                print(f"--- Turn {i+1} [{turn.get('timestamp')}] ---")
            print(turn["text"])
        return EXIT_SIGN_OFF

    if args.await_new_session:
        try:
            new_sess = await_new_session(
                timeout=args.await_timeout,
                require_tty=False,
                activate_gui=args.activate_gui,
                debug=args.debug,
            )
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
            allow_cross_project=args.allow_cross_project,
            debug=args.debug,
        )
        if verdict == "QUEUED_UNCONSUMED":
            return EXIT_QUEUED_UNCONSUMED
        if verdict == "NEEDS_NEW_THREAD":
            return EXIT_NEEDS_NEW_THREAD
        if resp is not None:
            print("\n=== [Antigravity Response] ===")
            print(resp)
            if verdict == "SIGN_OFF":
                return EXIT_SIGN_OFF
            if verdict == "CHANGES_REQUESTED":
                return EXIT_CHANGES_REQUESTED
            print("\n[Antigravity IPC] Note: No standardized VERDICT line (SIGN_OFF / CHANGES_REQUESTED) found.", file=sys.stderr)
            return EXIT_NO_VERDICT
        return EXIT_ERROR

    try:
        ok = send_message_to_antigravity(
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
