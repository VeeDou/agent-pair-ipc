"""
Shared utilities for Agent Pair IPC.
Provides helpers for message resolution, payload formatting, correlation nonces, and file handling.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

# Standardized Process Exit Codes
EXIT_SIGN_OFF = 0
EXIT_ERROR = 1
EXIT_CHANGES_REQUESTED = 2
EXIT_NO_VERDICT = 3
EXIT_QUEUED_UNCONSUMED = 4


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
    """Find .agent-pair/bindings.json starting from cwd walking up parents."""
    cur = Path(cwd or os.getcwd()).resolve()
    for p in [cur] + list(cur.parents):
        bindings = p / ".agent-pair" / "bindings.json"
        if bindings.is_file():
            return bindings
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
    base = Path(cwd or os.getcwd()).resolve()
    target_dir = base / ".agent-pair"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "bindings.json"
    data = {}
    if target_file.is_file():
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[agent_name] = session_id
    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return target_file


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


def attach_request_header(message: str, nonce: str, sender: str = "Antigravity") -> str:
    """Attach visible correlation nonce and sender header to outgoing message."""
    header = f"[AgentPairIPC-Req-ID: {nonce} | From: {sender}]"
    return f"{header}\n\n{message}"


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
