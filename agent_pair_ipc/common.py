"""
Shared utilities for Agent Pair IPC.
Provides helpers for message resolution, payload formatting, correlation nonces, and file handling.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
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
      - `tool sessA < input` -> msg=<stdin>, target="sessA"
      - `tool -s sessA ...` -> target="sessA" (always prioritized)
    """
    target = session_flag
    msg_text = None

    if file_path:
        fpath = Path(file_path).expanduser().resolve()
        if not fpath.is_file():
            print(f"[Agent Pair IPC] Error: file not found: {file_path}", file=sys.stderr)
            return None, None
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                msg_text = f.read().strip()
        except Exception as e:
            print(f"[Agent Pair IPC] Error reading file {file_path}: {e}", file=sys.stderr)
            return None, None
        # When -f is used, positional 'message' argument acts as target
        if not target and message_arg:
            target = message_arg or target_pos
    elif msg_flag is not None:
        msg_text = msg_flag
        # When -m is used, positional 'message' argument acts as target
        if not target:
            target = message_arg or target_pos
    elif message_arg == "-" or (not message_arg and not sys.stdin.isatty()):
        try:
            msg_text = sys.stdin.read().strip()
        except Exception:
            msg_text = None
        # When stdin is piped, positional 'message' argument acts as target
        if not target and message_arg and message_arg != "-":
            target = message_arg or target_pos
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


def format_peer_envelope(
    message: str,
    nonce: str,
    sender: str = "Antigravity",
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    reply_socket: str | None = None,
    action: str | None = None,
) -> str:
    """
    Format message using the standardized Peer Envelope specification.
    Provides structured request metadata, sender identity, operation boundaries,
    and request correlation identifiers for peer agent collaboration.
    """
    lines = [
        f"[AgentPairIPC-Req-ID: {nonce} | From: {sender}]",
        "",
        "【Agent 对等协作意图声明】",
        f"- 协作类型：{intent}",
        f"- 请求方：{sender} (Local Pair Programming Agent)",
    ]

    # Dynamic operation boundary based on declared intent / action
    intent_lower = intent.lower()
    act_lower = (action or "").lower()
    if "edit" in intent_lower or "fix" in intent_lower or "patch" in intent_lower or act_lower in ("edit", "fix", "patch"):
        boundary = "涉及代码或工程文件修改，需遵从受邀方工程规范与安全审查"
    elif "read-only" in intent_lower or "review" in intent_lower or "audit" in intent_lower or act_lower in ("review", "code_review", "audit"):
        boundary = "只读分析与评审意见，不请求修改系统权限、环境变量或持久化配置"
    else:
        boundary = f"遵从声明意图 [{intent}]，未经受邀方评估不执行超越边界操作"
    lines.append(f"- 操作边界声明：{boundary}")

    # User authorization context: NEVER fabricate quotes
    if user_context and user_context.strip():
        lines.append(f"- 用户授权声明：User prompt: \"{user_context.strip()}\" (由调用方声称，未经受邀方核实)")
    else:
        lines.append("- 用户授权声明：未提供显式用户输入引用 (由调用方 Agent 自主触发对等协作)")

    if reply_socket:
        lines.append(f"- 回调通道：[ap-reply-socket: {reply_socket}]")
    lines.append("----------------------------------------------------------------")
    lines.append(message.strip())
    return "\n".join(lines)


def attach_request_header(
    message: str,
    nonce: str,
    sender: str = "Antigravity",
    intent: str = "Read-Only Review",
    user_context: str | None = None,
    reply_socket: str | None = None,
) -> str:
    """Attach visible correlation nonce and standardized Peer Envelope to outgoing message."""
    return format_peer_envelope(
        message=message,
        nonce=nonce,
        sender=sender,
        intent=intent,
        user_context=user_context,
        reply_socket=reply_socket,
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


class EphemeralReplyServer:
    """
    Ephemeral Unix Domain Socket server for bi-directional peer responses.
    Listens at ~/.agent-pair/run/reply-<nonce>.sock with 0o600 permissions.
    Accepts responses directly from the peer agent without requiring JSONL transcript polling.
    """

    def __init__(self, nonce: str, timeout: float = 300.0, socket_dir: Path | None = None):
        self.nonce = nonce
        base_dir = socket_dir or get_secure_run_dir()
        self.socket_path = str(base_dir / f"reply-{nonce}.sock")
        self.timeout = timeout
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
        sock.bind(self.socket_path)
        try:
            os.chmod(self.socket_path, 0o600)
        except Exception:
            pass
        sock.listen(1)
        sock.setblocking(False)
        self.server_sock = sock
        return self.socket_path

    def poll_response(self, timeout: float = 0.0) -> tuple[str | None, str | None]:
        """
        Poll for an incoming response on the reply socket.
        Returns (response_text, verdict) if received, or (None, None) if not yet available.
        """
        if self._response_text is not None:
            return self._response_text, self._verdict
        if not self.server_sock or self._is_closed:
            return None, None

        import select
        try:
            rlist, _, _ = select.select([self.server_sock], [], [], timeout)
            if not rlist:
                return None, None

            client_sock, _ = self.server_sock.accept()
            client_sock.settimeout(5.0)
            chunks = []
            try:
                while True:
                    data = client_sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
            finally:
                try:
                    client_sock.close()
                except Exception:
                    pass

            raw_bytes = b"".join(chunks)
            if not raw_bytes:
                return None, None

            raw_str = raw_bytes.decode("utf-8", errors="replace").strip()
            # Try parsing as JSON envelope
            content = None
            try:
                data = json.loads(raw_str)
                if isinstance(data, dict):
                    content = data.get("content") or data.get("text") or data.get("message")
            except Exception:
                pass

            if content is None:
                content = raw_str

            # In accordance with peer review P1.1:
            # ALWAYS derive verdict strictly from extract_verdict(content).
            # Do NOT trust any top-level JSON "verdict" field, which could be spoofed.
            verdict = extract_verdict(content)
            self._response_text = content
            self._verdict = verdict
            return self._response_text, self._verdict

        except Exception:
            return None, None

    def close(self) -> None:
        """Close server socket and remove socket file."""
        self._is_closed = True
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


def activate_desktop_app(app_name: str, new_tab: bool = False) -> bool:
    """
    Activate a desktop application on macOS via AppleScript / System Events.
    Supports creating a new tab (Cmd+T) in supported apps (Claude Desktop, ChatGPT/Codex).

    Security and Focus Safety:
    - Verifies the target process exists and is confirmed frontmost before issuing keystrokes,
      preventing shortcut keys from being delivered to unintended applications.
    - Note: Generating simulated keystrokes requires macOS Accessibility permissions
      for the calling terminal or process.

    Returns True if successfully executed, False otherwise.
    """
    if sys.platform != "darwin":
        return False

    import subprocess
    import time
    try:
        cmd_activate = ["osascript", "-e", f'tell application "{app_name}" to activate']
        res = subprocess.run(cmd_activate, capture_output=True, text=True, timeout=5.0)
        if res.returncode != 0:
            return False

        if new_tab:
            time.sleep(0.3)
            # Ensure process exists, set frontmost to true, and verify frontmost before keystroke
            script = f'''tell application "System Events"
    if exists process "{app_name}" then
        tell process "{app_name}"
            set frontmost to true
            repeat 5 times
                if frontmost then exit repeat
                delay 0.1
            end repeat
            if frontmost then
                keystroke "t" using command down
                return "ok"
            else
                return "not_frontmost"
            end if
        end tell
    else
        return "not_found"
    end if
end tell'''
            cmd_tab = ["osascript", "-e", script]
            res_tab = subprocess.run(cmd_tab, capture_output=True, text=True, timeout=5.0)
            return res_tab.returncode == 0 and "ok" in res_tab.stdout

        return True
    except Exception:
        return False
