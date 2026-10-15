"""
agent-pair-ipc: Zero-dependency Local IPC & Pair-Review Toolkit for AI Coding Agents.
"""

from __future__ import annotations

__version__ = "0.1.0"

from agent_pair_ipc.claude import (
    get_active_sessions as get_active_claude_sessions,
    list_sessions as list_claude_sessions,
    read_latest_feedback as read_claude_feedback,
    send_and_wait as send_and_wait_claude,
    send_message_to_claude as send_to_claude,
)
from agent_pair_ipc.codex import (
    get_all_sessions as get_active_codex_sessions,
    list_sessions as list_codex_sessions,
    read_latest_feedback as read_codex_feedback,
    send_and_wait as send_and_wait_codex,
    send_message_to_codex as send_to_codex,
)

__all__ = [
    "__version__",
    "send_to_claude",
    "send_and_wait_claude",
    "read_claude_feedback",
    "list_claude_sessions",
    "get_active_claude_sessions",
    "send_to_codex",
    "send_and_wait_codex",
    "read_codex_feedback",
    "list_codex_sessions",
    "get_active_codex_sessions",
]
