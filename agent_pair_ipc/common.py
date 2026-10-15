"""
Shared utilities for Agent Pair IPC.
Provides helpers for message resolution, payload formatting, correlation nonces, and file handling.
"""

from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path


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
