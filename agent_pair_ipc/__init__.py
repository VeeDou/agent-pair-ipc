"""
agent-pair-ipc: Zero-dependency Local IPC & Pair-Review Toolkit for AI Coding Agents.
"""

from __future__ import annotations

__version__ = "0.4.0"

from agent_pair_ipc.claude import (
    get_active_sessions as get_active_claude_sessions,
    list_sessions as list_claude_sessions,
    read_latest_feedback as read_claude_feedback,
    send_and_wait as send_and_wait_claude,
    send_file as send_file_to_claude,
    send_message_to_claude as send_to_claude,
)
from agent_pair_ipc.codex import (
    get_all_sessions as get_active_codex_sessions,
    list_sessions as list_codex_sessions,
    read_latest_feedback as read_codex_feedback,
    send_and_wait as send_and_wait_codex,
    send_file as send_file_to_codex,
    send_message_to_codex as send_to_codex,
)
from agent_pair_ipc.common import (
    EphemeralReplyServer,
    ReplyRejectedError,
    activate_desktop_app,
    attach_request_header,
    build_reply_command,
    check_artifact_stable,
    create_safe_payload_file,
    detect_sender,
    extract_nonce,
    extract_peer_envelope,
    extract_verdict,
    format_peer_envelope,
    generate_nonce,
    get_secure_run_dir,
    is_pid_alive,
    list_reply_mailbox,
    open_new_gui_session,
    read_reply_mailbox,
    send_reply,
    spill_large_payload,
)

__all__ = [
    "__version__",
    "send_to_claude",
    "send_file_to_claude",
    "send_and_wait_claude",
    "read_claude_feedback",
    "list_claude_sessions",
    "get_active_claude_sessions",
    "send_to_codex",
    "send_file_to_codex",
    "send_and_wait_codex",
    "read_codex_feedback",
    "list_codex_sessions",
    "get_active_codex_sessions",
    "format_peer_envelope",
    "extract_peer_envelope",
    "create_safe_payload_file",
    "get_secure_run_dir",
    "EphemeralReplyServer",
    "activate_desktop_app",
    "extract_verdict",
    "extract_nonce",
    "generate_nonce",
    "attach_request_header",
    "send_reply",
    "read_reply_mailbox",
    "list_reply_mailbox",
    "ReplyRejectedError",
    "build_reply_command",
    "check_artifact_stable",
    "is_pid_alive",
    "detect_sender",
    "open_new_gui_session",
    "spill_large_payload",
]
