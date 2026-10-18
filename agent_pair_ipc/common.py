"""
Shared utilities for Agent Pair IPC.
Provides helpers for message resolution, file-based payloads, the Peer Envelope, correlation nonces,
the duplex reply channel (socket + mailbox), project/session path matching, and desktop GUI activation.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Standardized Process Exit Codes
EXIT_SIGN_OFF = 0
EXIT_ERROR = 1
EXIT_CHANGES_REQUESTED = 2
EXIT_NO_VERDICT = 3
EXIT_QUEUED_UNCONSUMED = 4
EXIT_NEEDS_NEW_THREAD = 5


class NeedsNewSessionError(RuntimeError):
    """Raised when an interactive new session/thread is required but running in a non-interactive environment."""
    pass


class TailReader:
    """
    Incremental binary-safe tail reader for JSONL files.
    Avoids O(N) full re-reads, safely handles multi-byte UTF-8 character splits,
    and maintains buffer state across polls.
    """

    def __init__(self, file_path: str | Path | None = None, start_offset: int = 0):
        self.file_path = str(file_path) if file_path else None
        self.offset = start_offset
        self.buffer = b""

    def set_file(self, file_path: str | Path, start_offset: int = 0) -> None:
        self.file_path = str(file_path)
        self.offset = start_offset
        self.buffer = b""

    def read_new_lines(self) -> list[str]:
        if not self.file_path or not os.path.exists(self.file_path):
            return []
        try:
            size = os.path.getsize(self.file_path)
            if size <= self.offset:
                return []
            with open(self.file_path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
                if not chunk:
                    return []
                self.offset += len(chunk)
                combined = self.buffer + chunk
                raw_lines = combined.split(b"\n")
                self.buffer = raw_lines[-1]

                lines = []
                for rline in raw_lines[:-1]:
                    rline = rline.strip()
                    if rline:
                        lines.append(rline.decode("utf-8", errors="replace"))
                return lines
        except Exception:
            return []


def find_project_bindings_file(cwd: str | None = None) -> Path | None:
    """
    Find .agent-pair/bindings.json starting from cwd walking up parents.
    Stops traversing past the repository root (.git), .agent-pair, or user home directory ($HOME).
    """
    cur = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for p in [cur] + list(cur.parents):
        bindings = p / ".agent-pair" / "bindings.json"
        if bindings.is_file():
            return bindings
        if (p / ".agent-pair").is_dir() or (p / ".git").exists() or p == home or p == p.parent:
            break
    return None


def load_project_bindings(cwd: str | None = None) -> dict[str, str]:
    """Load session bindings for the current project."""
    bfile = find_project_bindings_file(cwd)
    if not bfile:
        return {}
    try:
        with open(bfile, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_project_binding(agent_name: str, session_id: str, cwd: str | None = None) -> Path:
    """Save target session binding for an agent into .agent-pair/bindings.json."""
    cur = Path(cwd or os.getcwd()).resolve()
    existing = find_project_bindings_file(cur)
    if existing:
        target_file = existing
        target_dir = existing.parent
    else:
        target_root = cur
        for p in [cur] + list(cur.parents):
            if (p / ".agent-pair").is_dir() or (p / ".git").exists():
                target_root = p
                break
        target_dir = target_root / ".agent-pair"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "bindings.json"

    data = {}
    if target_file.is_file():
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
        except Exception:
            data = {}
    data[agent_name] = session_id
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return target_file


def unbind_project_binding(agent_name: str, cwd: str | None = None) -> bool:
    """Remove target session binding for an agent from .agent-pair/bindings.json."""
    bfile = find_project_bindings_file(cwd)
    if not bfile or not bfile.is_file():
        return False
    try:
        with open(bfile, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and agent_name in data:
            del data[agent_name]
            with open(bfile, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
    except Exception:
        pass
    return False


def _read_payload_source(file_path: str) -> str | None:
    """Read payload text from a file path, or from stdin when file_path is '-'."""
    if file_path == "-":
        try:
            return sys.stdin.read().strip()
        except Exception as e:
            print(f"[Agent Pair IPC] Error reading stdin: {e}", file=sys.stderr)
            return None
    fpath = Path(file_path).expanduser().resolve()
    if not fpath.is_file():
        print(f"[Agent Pair IPC] Error: file not found: {file_path}", file=sys.stderr)
        return None
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        print(f"[Agent Pair IPC] Error reading file {file_path}: {e}", file=sys.stderr)
        return None


def warn_inline_rich_text(msg_text: str | None) -> None:
    """
    Nudge callers away from `-m "<rich text>"`.
    Once a shell has evaluated backticks / $(...) inside double quotes, the lost content
    cannot be detected here, so the only reliable fix is to stop passing rich text via argv.
    """
    if not msg_text:
        return
    if "\n" in msg_text or "`" in msg_text or "$" in msg_text or len(msg_text) > 400:
        print(
            "[Agent Pair IPC] Hint: rich/multi-line text passed via -m. If it was built inside a double-quoted shell string,\n"
            "  backticks and $(...) may already have been swallowed by the shell. Prefer the file-based pipeline:\n"
            "    agent-pair-ipc <claude|codex> -f payload.md          # from a file\n"
            "    agent-pair-ipc <claude|codex> -f - <<'AP_EOF' ... AP_EOF   # quoted heredoc via stdin",
            file=sys.stderr,
        )


def resolve_message_payload(
    message_arg: str | None = None,
    msg_flag: str | None = None,
    file_path: str | None = None,
    prefix: str | None = None,
    target_pos: str | None = None,
    session_flag: str | None = None,
) -> tuple[str | None, str | None]:
    """
    Resolves the final message string and target session from CLI arguments.
    Handles all permutations of positional arguments, -m, -f, stdin, and -s:
      - `tool "hello"` -> msg="hello", target=None
      - `tool "hello" sessA` -> msg="hello", target="sessA"
      - `tool -m "hello"` -> msg="hello", target=None
      - `tool -m "hello" sessA` -> msg="hello", target="sessA"
      - `tool -f file.md sessA` -> msg=<file>, target="sessA"
      - `tool -f - sessA < input` -> msg=<stdin>, target="sessA"
      - `tool - sessA < input` -> msg=<stdin>, target="sessA"
      - `tool -s sessA ...` -> target="sessA" (always prioritized)
    """
    target = session_flag
    msg_text = None

    if file_path:
        msg_text = _read_payload_source(file_path)
        if msg_text is None:
            return None, None
        # When -f is used, positional 'message' argument acts as target
        if not target:
            target = message_arg or target_pos
    elif msg_flag is not None:
        msg_text = msg_flag
        # When -m is used, positional 'message' argument acts as target
        if not target:
            target = message_arg or target_pos
    elif message_arg == "-" or (not message_arg and not sys.stdin.isatty()):
        msg_text = _read_payload_source("-")
        # When stdin is piped, the remaining positional argument acts as target
        if not target:
            if message_arg and message_arg != "-":
                target = message_arg
            else:
                target = target_pos
    else:
        # Standard positional: tool <msg> [target]
        msg_text = message_arg
        if not target:
            target = target_pos

    if prefix and msg_text:
        msg_text = f"{prefix.strip()}\n\n{msg_text}"

    return msg_text, target


def generate_nonce() -> str:
    """Generate a compact unique correlation ID for matching requests to responses."""
    return f"req_{uuid.uuid4().hex[:12]}"


_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


def is_valid_nonce(nonce: str | None) -> bool:
    """Correlation IDs are used to derive file names, so restrict them to a safe charset."""
    return bool(nonce) and bool(_NONCE_RE.match(nonce))


def detect_sender() -> str:
    """
    Identify the calling agent for the Peer Envelope `From:` field.
    Order: $AGENT_PAIR_SENDER, Claude Code env markers, Codex env markers, default "Antigravity".
    """
    explicit = os.environ.get("AGENT_PAIR_SENDER", "").strip()
    if explicit:
        return explicit
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        return "Claude Code"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        return "Codex"
    return "Antigravity"


def normalize_path(path: str | None) -> str:
    """Resolve symlinks (e.g. macOS /tmp -> /private/tmp) so CWD comparisons are reliable."""
    if not path:
        return ""
    try:
        return os.path.realpath(os.path.expanduser(path))
    except Exception:
        return path


def find_project_config_file(cwd: str | None = None) -> Path | None:
    """
    Find .agent-pair/config.json starting from cwd walking up parents.
    Stops traversing past repository root (.git), .agent-pair, or user home directory ($HOME).
    """
    cur = Path(cwd or os.getcwd()).resolve()
    home = Path.home().resolve()
    for p in [cur] + list(cur.parents):
        cfg = p / ".agent-pair" / "config.json"
        if cfg.is_file():
            return cfg
        if (p / ".agent-pair").is_dir() or (p / ".git").exists() or p == home or p == p.parent:
            break
    return None


def load_project_aliases(cwd: str | None = None) -> list[str]:
    """Load normalized project aliases declared in .agent-pair/config.json."""
    cfg = find_project_config_file(cwd)
    if not cfg:
        return []
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            data = json.load(f)
            aliases = data.get("aliases", [])
            res = []
            for a in aliases:
                if isinstance(a, str) and a.strip():
                    norm = normalize_path(a.strip())
                    if norm and norm not in res:
                        res.append(norm)
            return res
    except Exception:
        return []


def _is_broad_directory(path: str) -> bool:
    """Check if path is too broad (home, root, Volumes, /Users) to be considered a project root."""
    if not path:
        return True
    norm = normalize_path(path)
    home_str = normalize_path(str(Path.home()))
    # Equal to HOME, /, or an ancestor of HOME (e.g. /Users)
    if norm in (home_str, os.sep) or home_str.startswith(norm.rstrip(os.sep) + os.sep):
        return True
    # System folders directly under root
    if norm in ("/Applications", "/Library", "/System", "/Volumes", "/tmp", "/private/tmp", "/var", "/private/var"):
        return True
    # Direct child under /Volumes (e.g. /Volumes/Macintosh HD)
    if norm.startswith("/Volumes/"):
        parts = [p for p in norm.split("/") if p]
        if len(parts) <= 2:
            return True
    return False


def _is_project_root(path: str) -> bool:
    """Check if directory contains markers of an actual project root (.git or .agent-pair)."""
    try:
        p = Path(path)
        if not p.is_dir():
            return False
        return (p / ".git").exists() or (p / ".agent-pair").is_dir()
    except Exception:
        return False


def _scan_top_level_symlinks(dir_path: str, target_path: str) -> bool:
    """
    Scan top-level of dir_path for symlinks pointing to target_path.
    Enforces R4 safety boundaries:
      - Excludes $HOME, /, or /Volumes/* from scanning.
      - Disregards symlinks resolving to broad directories ($HOME, /, /Volumes, etc.).
      - Ancestor relaxation (target_path inside resolved) is ONLY accepted if resolved target
        is a verified project root (has .git or .agent-pair); otherwise requires exact match.
    Single-level only, non-recursive, safe against symlink loops.
    """
    try:
        p = Path(dir_path)
        if not p.is_dir():
            return False
        if _is_broad_directory(dir_path):
            return False
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        resolved = normalize_path(entry.path)
                        if not resolved or _is_broad_directory(resolved):
                            continue
                        if resolved == target_path:
                            return True
                        # Ancestor relaxation only if resolved target is a project root (.git or .agent-pair)
                        if _is_project_root(resolved) and target_path.startswith(resolved.rstrip(os.sep) + os.sep):
                            return True
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        pass
    return False


def cwd_relation(session_cwd: str | None, cwd: str | None) -> str | None:
    """
    Classify how a session's CWD relates to the caller's CWD:
      - "exact":    same directory (after symlink resolution)
      - "linked":   aliased in .agent-pair/config.json or connected via top-level symlink
      - "ancestor": session runs at a parent directory of the caller (e.g. repo root vs. subdir)
      - None:       unrelated
    $HOME and / never count as ancestors, so a home-directory session can't claim every project.
    """
    a = normalize_path(session_cwd)
    b = normalize_path(cwd)
    if not a or not b:
        return None
    if a == b:
        return "exact"
    if _is_broad_directory(a):
        return None

    # 1. Configured project aliases (bi-directional check)
    a_aliases = load_project_aliases(a)
    b_aliases = load_project_aliases(b)
    if a in b_aliases or b in a_aliases:
        return "linked"

    # 2. Top-level symlink scan (R4 safe boundary)
    if _scan_top_level_symlinks(a, b) or _scan_top_level_symlinks(b, a):
        return "linked"

    # 3. Ancestor check: b is a subdirectory inside a
    if b.startswith(a.rstrip(os.sep) + os.sep):
        return "ancestor"

    return None


def filter_project_sessions(candidates: list[dict], cwd: str | None = None) -> list[dict]:
    """
    Return sessions whose CWD relates to the caller's CWD, strictly prioritizing:
      1. exact: same directory
      2. linked: project aliases or top-level symlinks
      3. ancestor: parent directory
    Single-pass bucketing to avoid redundant scans.
    """
    cwd = cwd or os.getcwd()
    exact = []
    linked = []
    ancestor = []
    for c in candidates:
        rel = cwd_relation(c.get("cwd"), cwd)
        if rel == "exact":
            exact.append(c)
        elif rel == "linked":
            linked.append(c)
        elif rel == "ancestor":
            ancestor.append(c)

    if exact:
        return exact
    if linked:
        return linked
    return ancestor


def get_secure_run_dir() -> Path:
    """
    Get or create secure runtime directory for sockets and ephemeral IPC files.
    Prefers ~/.agent-pair/run with mode 0o700.
    Falls back to <tempdir>/agent-pair-<uid> with mode 0o700 if home is not accessible.
    """
    try:
        run_dir = Path.home() / ".agent-pair" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(run_dir, 0o700)
        except Exception:
            pass
        return run_dir
    except Exception:
        uid = os.getuid() if hasattr(os, "getuid") else "shared"
        tmp_run = Path(tempfile.gettempdir()) / f"agent-pair-{uid}"
        tmp_run.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(tmp_run, 0o700)
        except Exception:
            pass
        return tmp_run


def get_reply_mailbox_dir() -> Path:
    """Directory holding replies that arrived after the requester stopped listening (0o700)."""
    box = get_secure_run_dir() / "replies"
    box.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(box, 0o700)
    except Exception:
        pass
    return box


def reply_socket_path(nonce: str, socket_dir: Path | None = None) -> str:
    return str((socket_dir or get_secure_run_dir()) / f"reply-{nonce}.sock")


def reply_mailbox_path(nonce: str, mailbox_dir: Path | None = None) -> Path:
    return (mailbox_dir or get_reply_mailbox_dir()) / f"{nonce}.json"


def create_safe_payload_file(content: str, prefix: str = "ap_payload_", suffix: str = ".md") -> str:
    """
    Safely create an ephemeral payload file with strict permissions (0o600)
    in the user-private runtime directory (~/.agent-pair/run).
    Avoids shell quoting and argument-length limitations.
    Returns the absolute file path.
    """
    run_dir = get_secure_run_dir()
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(run_dir))
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return str(Path(path).resolve())
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(path):
            try:
                os.unlink(path)
            except Exception:
                pass
        raise


# `codex queue --message` passes the payload through argv (macOS ARG_MAX is 1 MiB, shared with env).
DEFAULT_SPILL_THRESHOLD_BYTES = 128 * 1024


def spill_large_payload(message: str, threshold_bytes: int = DEFAULT_SPILL_THRESHOLD_BYTES) -> tuple[str, str | None]:
    """
    Keep oversized bodies out of argv: write them to a 0o600 payload file and return a short
    pointer message instead. Returns (message_to_send, payload_path_or_None).
    """
    size = len(message.encode("utf-8"))
    if size <= threshold_bytes:
        return message, None
    path = create_safe_payload_file(message, prefix="ap_payload_")
    pointer = (
        f"【文件化 Payload】本次请求正文过长（{size} 字节），已完整保存到本地文件：\n"
        f"{path}\n"
        "请先完整读取该文件，再按其中的要求处理。"
    )
    return pointer, path


ENVELOPE_SEPARATOR = "-" * 64


def spill_enveloped_message(message: str, threshold_bytes: int = DEFAULT_SPILL_THRESHOLD_BYTES) -> tuple[str, str | None]:
    """
    Like spill_large_payload, but keeps a Peer Envelope header (nonce, intent, reply instructions)
    inline so correlation and authorization context survive; only the body is moved to a file.
    """
    if len(message.encode("utf-8")) <= threshold_bytes:
        return message, None
    sep = "\n" + ENVELOPE_SEPARATOR + "\n"
    idx = message.find(sep)
    if idx == -1:
        return spill_large_payload(message, threshold_bytes=0)
    header = message[: idx + len(sep)]
    pointer, path = spill_large_payload(message[idx + len(sep):], threshold_bytes=0)
    return header + pointer, path


def cleanup_stale_run_files(max_age_seconds: float = 3 * 24 * 3600) -> int:
    """Remove old payload files, mailbox replies, and orphaned reply sockets. Returns count removed."""
    removed = 0
    now = time.time()
    try:
        run_dir = get_secure_run_dir()
        targets = list(run_dir.glob("ap_payload_*")) + list(run_dir.glob("reply-*.sock"))
        box = run_dir / "replies"
        if box.is_dir():
            targets += list(box.glob("*.json"))
        for p in targets:
            try:
                if now - p.lstat().st_mtime > max_age_seconds:
                    p.unlink()
                    removed += 1
            except Exception:
                continue
    except Exception:
        pass
    return removed


def build_reply_command() -> str:
    """
    Absolute command the peer agent should run to answer a request.
    Uses this interpreter plus the repo's bin script when available, so it works even if
    `agent-pair-ipc` is not on the receiving agent's PATH.
    """
    import shlex
    bin_script = Path(__file__).resolve().parent.parent / "bin" / "agent-pair-ipc"
    if bin_script.is_file():
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(bin_script))}"
    return f"{shlex.quote(sys.executable)} -m agent_pair_ipc"


def _intent_boundary(intent: str, action: str | None = None) -> str:
    intent_lower = intent.lower()
    act_lower = (action or "").lower()
    if any(k in intent_lower for k in ("notif", "fyi", "通知", "同步")) or act_lower in ("notify", "notification"):
        return "仅为信息同步通知，不请求执行任何操作；如通知中提到上线/发布等变更，请以你的用户确认为准"
    if "edit" in intent_lower or "fix" in intent_lower or "patch" in intent_lower or act_lower in ("edit", "fix", "patch"):
        return "涉及代码或工程文件修改，需遵从受邀方工程规范与安全审查"
    if "read-only" in intent_lower or "review" in intent_lower or "audit" in intent_lower or act_lower in ("review", "code_review", "audit"):
        return "只读分析与评审意见，不请求修改系统权限、环境变量或持久化配置"
    return f"遵从声明意图 [{intent}]，未经受邀方评估不执行超越边界操作"


def is_pid_alive(pid: int | str | None) -> bool:
    """Check whether a process with the given PID is currently alive on the system."""
    if not pid:
        return False
    try:
        ipid = int(pid)
    except (ValueError, TypeError):
        return False
    if ipid <= 0:
        return False
    try:
        os.kill(ipid, 0)
        return True
    except OSError as err:
        import errno
        return err.errno == errno.EPERM


def check_artifact_stable(
    file_path: str | Path | None,
    initial_mtime: float = 0.0,
    initial_size: int = 0,
    min_stable_seconds: float = 3.0,
    require_verdict: bool = True,
) -> tuple[bool, str | None, str | None]:
    """
    Check if an expected output artifact file has been created or updated and stabilized.
    
    Args:
        file_path: Path to the expected deliverable.
        initial_mtime: File mtime before request was dispatched.
        initial_size: File size before request was dispatched.
        min_stable_seconds: Seconds mtime must remain unchanged before considering file ready.
        require_verdict: If True, requires that a standard VERDICT line (SIGN_OFF / CHANGES_REQUESTED)
                         is present in the file before returning ready. This prevents premature
                         capture of intermediate working drafts while an agent is still editing.
                         
    Returns:
        tuple[is_ready, content, verdict]
    """
    if not file_path:
        return False, None, None
    p = Path(file_path).expanduser().resolve()
    if not p.is_file():
        return False, None, None
    try:
        st = p.stat()
        if st.st_size == 0:
            return False, None, None

        if initial_mtime > 0:
            is_updated = (st.st_mtime > initial_mtime)
        else:
            is_updated = (st.st_size > 0)

        if not is_updated:
            return False, None, None

        # Check stability: mtime has settled for min_stable_seconds
        now = time.time()
        if (now - st.st_mtime) < min_stable_seconds:
            return False, None, None

        content = p.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return False, None, None

        verdict = extract_verdict(content)
        if require_verdict and not verdict:
            # File is updated, but still in-progress (lacks standardized VERDICT line)
            return False, None, None

        return True, content, verdict
    except Exception:
        return False, None, None


def format_peer_envelope(
    message: str,
    nonce: str,
    sender: str = "Antigravity",
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    reply_socket: str | None = None,
    action: str | None = None,
    reply_command: str | None = None,
    expected_artifact: str | None = None,
    caller_cwd: str | None = None,
    target_cwd: str | None = None,
    cwd_relation_type: str | None = None,
) -> str:
    """
    Format message using the standardized Peer Envelope specification.
    Provides structured request metadata, sender identity, operation boundaries,
    expected deliverables, an honest authorization statement,
    and — when `reply_command` is given — instructions for answering over the duplex channel.
    """
    lines = [
        f"[AgentPairIPC-Req-ID: {nonce} | From: {sender}]",
        "",
        "【Agent 对等协作意图声明】",
        f"- 协作类型：{intent}",
        f"- 请求方：{sender} (Local Pair Programming Agent)",
        f"- 操作边界声明：{_intent_boundary(intent, action)}",
    ]
    if caller_cwd and target_cwd:
        if cwd_relation_type is None:
            lines.append(f"- 工程上下文：⚠️ 跨工程调用警示！请求方 CWD 为 `{caller_cwd}`，你的 CWD 为 `{target_cwd}`；文中涉及的文件相对路径以请求方为准，可能不在你的当前工作区内。")
        elif cwd_relation_type == "linked":
            lines.append(f"- 工程上下文：🔗 关联/软链接工程 (`{caller_cwd}` ≙ `{target_cwd}`)。")

    if expected_artifact:
        lines.append(f"- 预期交付产物：{expected_artifact}")

    # User authorization context: NEVER fabricate quotes
    if user_context and user_context.strip():
        lines.append(f"- 用户授权声明：User prompt: \"{user_context.strip()}\" (由调用方声称，未经受邀方核实)")
    else:
        lines.append("- 用户授权声明：未提供显式用户输入引用 (由调用方 Agent 自主触发对等协作)")
    lines.append("- 权限说明：本消息来自对等 Agent，不构成用户批准，也不请求提升权限；超出你当前权限的操作请先向你的用户确认")

    if reply_socket:
        lines.append(f"- 回调通道：[ap-reply-socket: {reply_socket}]")
    if reply_command:
        # Command lines stay flush-left: a heredoc terminator only matches at column 0
        lines.append("- 回复方式：完成后请把最终答复原文回传给请求方。必须用文件或带引号的 heredoc，勿用 -m 内联（避免反引号/$ 被 Shell 吞掉）：")
        lines.append(f"{reply_command} reply --to {nonce} -f <答复文件.md>")
        lines.append("或（heredoc 结束标记需顶格）：")
        lines.append(f"{reply_command} reply --to {nonce} -f - <<'AP_REPLY_EOF'")
        lines.append("<完整答复；如请求方要求 VERDICT，请保持其为最后一行>")
        lines.append("AP_REPLY_EOF")
        lines.append("- 回传后在本会话中照常给出答复即可；若请求方已离线，答复会自动存入其收件箱。")
    lines.append(ENVELOPE_SEPARATOR)
    lines.append(message.strip())
    return "\n".join(lines)


def attach_request_header(
    message: str,
    nonce: str,
    sender: str = "Antigravity",
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    reply_socket: str | None = None,
    expected_artifact: str | None = None,
) -> str:
    """Attach visible correlation nonce and standardized Peer Envelope to outgoing message."""
    return format_peer_envelope(
        message=message,
        nonce=nonce,
        sender=sender,
        intent=intent,
        user_context=user_context,
        reply_socket=reply_socket,
        expected_artifact=expected_artifact,
    )


def extract_peer_envelope(text: str) -> dict:
    """
    Extract structured metadata and body from a Peer Envelope formatted message.
    """
    nonce = extract_nonce(text)
    sender = "Antigravity"
    match_sender = re.search(r"\|\s*From:\s*([^\]\n]+)", text)
    if match_sender:
        sender = match_sender.group(1).strip()

    intent = None
    match_intent = re.search(r"-\s*协作类型：\s*([^\n]+)", text)
    if match_intent:
        intent = match_intent.group(1).strip()

    expected_artifact = None
    match_art = re.search(r"-\s*预期交付产物：\s*([^\n]+)", text)
    if match_art:
        expected_artifact = match_art.group(1).strip()

    user_context = None
    match_ctx = re.search(r"-\s*用户授权(?:声明|上下文)：\s*(?:User\s*(?:explicit\s*)?prompt:\s*)?\"?([^\n\"]+?)\"?(?:\s*\(.*?\))?$", text, re.MULTILINE)
    if match_ctx:
        raw_val = match_ctx.group(1).strip()
        if not raw_val.startswith("未提供显式用户输入引用"):
            user_context = raw_val

    reply_socket = None
    match_sock = re.search(r"\[ap-reply-socket:\s*([^\]\n]+)\]", text)
    if match_sock:
        reply_socket = match_sock.group(1).strip()

    sep_match = re.search(r"-{10,}\s*\n(.*)$", text, re.DOTALL)
    if sep_match:
        body = sep_match.group(1).strip()
    else:
        body = text.strip()

    return {
        "nonce": nonce,
        "sender": sender,
        "intent": intent,
        "expected_artifact": expected_artifact,
        "user_context": user_context,
        "reply_socket": reply_socket,
        "body": body,
    }


def extract_nonce(text: str) -> str | None:
    """Extract correlation nonce from message text if present."""
    match = re.search(r"\[AgentPairIPC-Req-ID:\s*([a-zA-Z0-9_-]+)", text)
    return match.group(1) if match else None


def extract_verdict(text: str) -> str | None:
    """
    Extract standardized verdict from assistant response text.
    Strictly verifies that the LAST non-empty line of the response is a standalone verdict line:
      ^VERDICT: (SIGN_OFF|CHANGES_REQUESTED)$
    This ensures prompt specifications or code block quotations earlier in the document
    can never trigger false positives.
    """
    if not text:
        return None
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    match = re.match(r"^VERDICT:\s*(SIGN_OFF|CHANGES_REQUESTED)$", lines[-1])
    if match:
        return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Duplex reply channel (protocol 2.0)
#
# Requester: binds ~/.agent-pair/run/reply-<nonce>.sock and advertises `reply --to <nonce>`.
# Replier:   connects, sends ONE JSON frame, half-closes (SHUT_WR), reads ONE JSON ack line.
#   frame = {"x_agent_ipc_version": "2.0", "type": "reply" | "progress",
#            "correlation_id": "<nonce>", "sender": "...", "content": "..."}
#   ack   = {"ok": true, "correlation_id": "<nonce>"}  |  {"ok": false, "error": "..."}
# If nobody is listening, a final reply is stored in ~/.agent-pair/run/replies/<nonce>.json
# (the "mailbox") so the requester can pick it up later with `agent-pair-ipc inbox <nonce>`.
# ---------------------------------------------------------------------------

IPC_PROTOCOL_VERSION = "2.0"
MAX_REPLY_BYTES = 32 * 1024 * 1024


class ReplyRejectedError(RuntimeError):
    """The requester's reply server refused the frame (e.g. correlation ID mismatch)."""
    pass


def _parse_reply_frame(raw_bytes: bytes) -> dict:
    """Decode a reply frame; plain text (legacy v1 replies) is treated as a final reply."""
    raw_str = raw_bytes.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw_str)
        if isinstance(data, dict):
            content = data.get("content")
            if content is None:
                content = data.get("text") or data.get("message") or ""
            return {
                "type": str(data.get("type") or "reply"),
                "correlation_id": data.get("correlation_id") or data.get("x_correlation_id"),
                "sender": data.get("sender"),
                "content": str(content),
            }
    except Exception:
        pass
    return {"type": "reply", "correlation_id": None, "sender": None, "content": raw_str}


def write_reply_mailbox(nonce: str, content: str, sender: str | None = None, mailbox_dir: Path | None = None) -> Path:
    """Atomically store a final reply for a requester that is not currently listening (0o600)."""
    if not is_valid_nonce(nonce):
        raise ValueError(f"invalid correlation id: {nonce!r}")
    box = mailbox_dir or get_reply_mailbox_dir()
    target = reply_mailbox_path(nonce, box)
    fd, tmp = tempfile.mkstemp(prefix=f".{nonce}.", suffix=".tmp", dir=str(box))
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({
                "x_agent_ipc_version": IPC_PROTOCOL_VERSION,
                "correlation_id": nonce,
                "sender": sender,
                "content": content,
                "received_at": time.time(),
            }, f, ensure_ascii=False)
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass
        raise
    return target


def read_reply_mailbox(nonce: str, consume: bool = False, mailbox_dir: Path | None = None) -> dict | None:
    """Return a stored mailbox reply ({correlation_id, sender, content, received_at}) or None."""
    if not is_valid_nonce(nonce):
        return None
    path = reply_mailbox_path(nonce, mailbox_dir)
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if consume:
        try:
            path.unlink()
        except Exception:
            pass
    return data if isinstance(data, dict) else None


def list_reply_mailbox(mailbox_dir: Path | None = None) -> list[dict]:
    """List stored mailbox replies, newest first."""
    box = mailbox_dir or get_reply_mailbox_dir()
    items = []
    for p in box.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("correlation_id", p.stem)
                items.append(data)
        except Exception:
            continue
    items.sort(key=lambda d: d.get("received_at") or 0.0, reverse=True)
    return items


def send_reply(
    nonce: str,
    content: str,
    kind: str = "reply",
    sender: str | None = None,
    timeout: float = 10.0,
    socket_dir: Path | None = None,
    mailbox_dir: Path | None = None,
    allow_mailbox: bool = True,
) -> str:
    """
    Answer a correlated request over the duplex reply socket.
    The socket path is always derived from the nonce inside the private run dir, never taken
    from message text, so a forged envelope cannot redirect replies to an arbitrary socket.

    Returns "socket" when the requester acknowledged the frame, or "mailbox" when nobody was
    listening and the final reply was stored for later pickup. Progress frames are best-effort
    and return "dropped" if the requester is gone.
    Raises ReplyRejectedError if the requester explicitly refused the frame.
    """
    import socket as _socket

    if not is_valid_nonce(nonce):
        raise ValueError(f"invalid correlation id: {nonce!r}")
    sender = sender or detect_sender()
    frame = json.dumps({
        "x_agent_ipc_version": IPC_PROTOCOL_VERSION,
        "type": kind,
        "correlation_id": nonce,
        "sender": sender,
        "content": content,
    }, ensure_ascii=False).encode("utf-8") + b"\n"

    sock_path = reply_socket_path(nonce, socket_dir)
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(sock_path)
            s.sendall(frame)
            try:
                s.shutdown(_socket.SHUT_WR)
            except Exception:
                pass
            ack_raw = b""
            while b"\n" not in ack_raw and len(ack_raw) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                ack_raw += chunk
        finally:
            s.close()
        ack = {}
        try:
            ack = json.loads(ack_raw.decode("utf-8", errors="replace").strip() or "{}")
        except Exception:
            ack = {}
        if isinstance(ack, dict) and ack.get("ok") is False:
            raise ReplyRejectedError(str(ack.get("error") or "rejected by requester"))
        if isinstance(ack, dict) and ack.get("ok") is True:
            return "socket"
        # No ack (requester closed mid-read): fall through to mailbox for final replies
    except ReplyRejectedError:
        raise
    except (FileNotFoundError, ConnectionRefusedError, ConnectionResetError, BrokenPipeError, _socket.timeout, OSError):
        pass

    if kind != "reply" or not allow_mailbox:
        return "dropped"
    write_reply_mailbox(nonce, content, sender=sender, mailbox_dir=mailbox_dir)
    return "mailbox"


class EphemeralReplyServer:
    """
    Ephemeral Unix Domain Socket server for bi-directional peer responses.
    Listens at ~/.agent-pair/run/reply-<nonce>.sock with 0o600 permissions.
    Accepts responses directly from the peer agent without requiring JSONL transcript polling,
    acknowledges every frame, rejects frames carrying another request's correlation ID,
    relays progress frames, and also checks the mailbox for replies written while unbound.
    """

    def __init__(
        self,
        nonce: str,
        timeout: float = 300.0,
        socket_dir: Path | None = None,
        mailbox_dir: Path | None = None,
        on_progress=None,
    ):
        self.nonce = nonce
        base_dir = socket_dir or get_secure_run_dir()
        self.socket_path = str(base_dir / f"reply-{nonce}.sock")
        self.mailbox_dir = mailbox_dir
        self.timeout = timeout
        self.on_progress = on_progress
        self.progress: list[str] = []
        self.reply_sender: str | None = None
        self.channel: str | None = None
        self.server_sock = None
        self._response_text: str | None = None
        self._verdict: str | None = None
        self._is_closed = False

    def start(self) -> str:
        """Bind and start listening on the ephemeral reply socket."""
        import socket
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Bind under a restrictive umask so the socket is never briefly world-connectable
        old_umask = os.umask(0o177)
        try:
            sock.bind(self.socket_path)
        finally:
            os.umask(old_umask)
        try:
            os.chmod(self.socket_path, 0o600)
        except Exception:
            pass
        sock.listen(8)
        sock.setblocking(False)
        self.server_sock = sock
        return self.socket_path

    def _send_ack(self, client_sock, ok: bool, error: str | None = None) -> None:
        ack = {"ok": ok, "correlation_id": self.nonce}
        if error:
            ack["error"] = error
        try:
            client_sock.sendall((json.dumps(ack) + "\n").encode("utf-8"))
        except Exception:
            pass

    def _finish(self, content: str, sender: str | None, channel: str) -> tuple[str, str | None]:
        # Peer review P1.1: ALWAYS derive the verdict from the content's last line,
        # never from any self-declared top-level JSON "verdict" field.
        self._response_text = content
        self._verdict = extract_verdict(content)
        self.reply_sender = sender
        self.channel = channel
        return self._response_text, self._verdict

    def _handle_client(self, client_sock) -> tuple[str | None, str | None]:
        client_sock.settimeout(5.0)
        try:
            chunks = []
            total = 0
            while True:
                data = client_sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if total > MAX_REPLY_BYTES:
                    self._send_ack(client_sock, False, "reply too large")
                    return None, None
            raw_bytes = b"".join(chunks)
            if not raw_bytes.strip():
                return None, None

            frame = _parse_reply_frame(raw_bytes)
            cid = frame.get("correlation_id")
            if cid and cid != self.nonce:
                self._send_ack(client_sock, False, f"correlation_id mismatch (expected {self.nonce})")
                return None, None

            if frame["type"] == "progress":
                self.progress.append(frame["content"])
                self._send_ack(client_sock, True)
                if self.on_progress:
                    try:
                        self.on_progress(frame["content"], frame.get("sender"))
                    except Exception:
                        pass
                return None, None

            self._send_ack(client_sock, True)
            return self._finish(frame["content"], frame.get("sender"), "socket")
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def check_mailbox(self) -> tuple[str | None, str | None]:
        """Pick up a reply that was stored in the mailbox (e.g. while the socket was unavailable)."""
        if self._response_text is not None:
            return self._response_text, self._verdict
        data = read_reply_mailbox(self.nonce, consume=True, mailbox_dir=self.mailbox_dir)
        if data and data.get("content") is not None:
            return self._finish(str(data["content"]), data.get("sender"), "mailbox")
        return None, None

    def poll_response(self, timeout: float = 0.0) -> tuple[str | None, str | None]:
        """
        Poll for an incoming final response on the reply socket (and the mailbox).
        Progress frames are consumed and relayed while waiting.
        Returns (response_text, verdict) if received, or (None, None) if not yet available.
        """
        if self._response_text is not None:
            return self._response_text, self._verdict

        import select
        deadline = time.time() + max(timeout, 0.0)
        while True:
            if self.server_sock and not self._is_closed:
                try:
                    remaining = max(0.0, deadline - time.time())
                    rlist, _, _ = select.select([self.server_sock], [], [], remaining)
                    if rlist:
                        client_sock, _ = self.server_sock.accept()
                        resp, verdict = self._handle_client(client_sock)
                        if resp is not None:
                            return resp, verdict
                        if time.time() < deadline:
                            continue
                except Exception:
                    pass

            resp, verdict = self.check_mailbox()
            if resp is not None:
                return resp, verdict
            if time.time() >= deadline:
                return None, None
            time.sleep(min(0.1, max(0.0, deadline - time.time())))

    def close(self) -> None:
        """Close server socket, remove socket file, and drop any duplicate mailbox copy of a delivered reply."""
        self._is_closed = True
        if self._response_text is not None:
            # A replier that timed out waiting for our ack stores an at-least-once mailbox copy
            read_reply_mailbox(self.nonce, consume=True, mailbox_dir=self.mailbox_dir)
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except Exception:
                pass

    def __enter__(self) -> EphemeralReplyServer:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Desktop GUI lifecycle: "GUI-native creation, IPC attaches afterwards"
# ---------------------------------------------------------------------------

# Menu items verified against the macOS menu bars of Claude Desktop (File > New Session, Cmd+N)
# and ChatGPT.app, which hosts the Codex desktop experience (File > New Chat).
GUI_PROFILES: dict[str, dict] = {
    "claude": {"apps": ["Claude"], "menu_items": ["New Session", "New Chat"], "key": "n"},
    "codex": {"apps": ["ChatGPT", "Codex"], "menu_items": ["New Thread", "New Chat"], "key": "n"},
}


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def activate_desktop_app(
    app_name: str,
    new_tab: bool = False,
    menu_items: list[str] | None = None,
    key: str | None = None,
) -> bool:
    """
    Activate a desktop application on macOS via AppleScript / System Events and optionally
    ask it to create a new session.

    New-session strategy:
      1. Click the first existing item from `menu_items` in the app's File menu (robust against
         shortcut changes across app versions).
      2. Otherwise send Cmd+<key> ($AGENT_PAIR_GUI_NEW_SESSION_KEY overrides, default "n").

    Security and Focus Safety:
    - Verifies the target process exists and is confirmed frontmost before clicking or issuing
      keystrokes, preventing input from being delivered to unintended applications.
    - Requires macOS Accessibility permissions for the calling terminal or process.

    Returns True if successfully executed, False otherwise.
    """
    if sys.platform != "darwin":
        return False

    import subprocess
    try:
        cmd_activate = ["osascript", "-e", f"tell application {_applescript_string(app_name)} to activate"]
        res = subprocess.run(cmd_activate, capture_output=True, text=True, timeout=5.0)
        if res.returncode != 0:
            return False

        if new_tab:
            time.sleep(0.3)
            key = (os.environ.get("AGENT_PAIR_GUI_NEW_SESSION_KEY") or key or "n")[:1]
            items = menu_items or []
            items_list = "{" + ", ".join(_applescript_string(i) for i in items) + "}"
            proc = _applescript_string(app_name)
            script = f'''tell application "System Events"
    if exists process {proc} then
        tell process {proc}
            set frontmost to true
            repeat 10 times
                if frontmost then exit repeat
                delay 0.1
            end repeat
            if not frontmost then return "not_frontmost"
            repeat with itemName in {items_list}
                try
                    click menu item (itemName as text) of menu 1 of menu bar item "File" of menu bar 1
                    return "ok:menu"
                end try
            end repeat
            keystroke {_applescript_string(key)} using command down
            return "ok:key"
        end tell
    else
        return "not_found"
    end if
end tell'''
            cmd_tab = ["osascript", "-e", script]
            res_tab = subprocess.run(cmd_tab, capture_output=True, text=True, timeout=8.0)
            return res_tab.returncode == 0 and "ok" in res_tab.stdout

        return True
    except Exception:
        return False


def open_new_gui_session(agent: str) -> bool:
    """Bring the agent's desktop client to front and trigger its native "new session" action."""
    profile = GUI_PROFILES.get(agent)
    if not profile:
        return False
    for app in profile["apps"]:
        if activate_desktop_app(app, new_tab=True, menu_items=profile["menu_items"], key=profile["key"]):
            return True
    return False


def settle_duplex_reply(
    reply_server: EphemeralReplyServer | None,
    transcript_text: str,
    reply_cmd_seen: bool = False,
) -> tuple[str, str | None, str]:
    """
    The transcript showed a final answer; give the duplex channel a moment to deliver too.
    A reply already sitting in the socket backlog returns immediately; if the peer was seen
    running `reply --to <nonce>`, wait a little longer for the frame to land.
    Prefers the duplex reply (it is the peer's deliberate answer, not its chat summary).
    Returns (text, verdict, channel) with channel in {"socket", "mailbox", "transcript"}.
    """
    if reply_server is not None:
        resp, verdict = reply_server.poll_response(timeout=5.0 if reply_cmd_seen else 1.0)
        if resp is not None:
            return resp, verdict, reply_server.channel or "socket"
    return transcript_text, extract_verdict(transcript_text), "transcript"


def print_progress_frame(prefix: str):
    """Build an on_progress callback that relays peer progress frames to stderr."""
    def _cb(text: str, sender: str | None) -> None:
        who = f" {sender}" if sender else ""
        first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        print(f"[{prefix}] Progress from{who}: {first[:200]}", file=sys.stderr)
    return _cb
