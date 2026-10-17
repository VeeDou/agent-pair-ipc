"""
Unified Command Line Interface for Agent Pair IPC.
Supports subcommands for Claude Code and Codex, overall session discovery,
and both ends of the duplex reply channel (`reply` for the peer, `inbox` for the requester).
"""

from __future__ import annotations

import argparse
import sys
import time

from agent_pair_ipc import __version__
from agent_pair_ipc import claude, codex
from agent_pair_ipc.common import (
    EXIT_CHANGES_REQUESTED,
    EXIT_ERROR,
    EXIT_NO_VERDICT,
    EXIT_SIGN_OFF,
    ReplyRejectedError,
    extract_verdict,
    is_valid_nonce,
    list_reply_mailbox,
    read_reply_mailbox,
    resolve_message_payload,
    send_reply,
)


def _verdict_exit_code(text: str) -> int:
    verdict = extract_verdict(text)
    if verdict == "SIGN_OFF":
        return EXIT_SIGN_OFF
    if verdict == "CHANGES_REQUESTED":
        return EXIT_CHANGES_REQUESTED
    return EXIT_NO_VERDICT


def reply_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-pair-ipc reply",
        description="Answer a correlated peer request over the duplex reply channel.\n"
                    "Delivers to the requester's live reply socket; if it is no longer listening,\n"
                    "the reply is stored in its mailbox (~/.agent-pair/run/replies/).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  agent-pair-ipc reply --to req_0123456789ab -f answer.md
  agent-pair-ipc reply --to req_0123456789ab -f - <<'AP_REPLY_EOF'
  Found 1 issue in `rules.yaml` ...
  VERDICT: CHANGES_REQUESTED
  AP_REPLY_EOF
  agent-pair-ipc reply --to req_0123456789ab --progress -m "Running test suite"
""",
    )
    parser.add_argument("--to", dest="nonce", required=True, help="Request correlation ID from the envelope ([AgentPairIPC-Req-ID: ...])")
    parser.add_argument("-f", "--file", dest="file_path", default=None, help="File with the reply text ('-' reads stdin; recommended)")
    parser.add_argument("-m", "--msg", dest="msg_flag", default=None, help="Short inline reply text (avoid for rich text)")
    parser.add_argument("--progress", action="store_true", help="Send a progress update instead of the final reply")
    parser.add_argument("--sender", default=None, help="Sender name (default: $AGENT_PAIR_SENDER or auto-detected)")
    parser.add_argument("--no-mailbox", action="store_true", help="Fail instead of storing the reply when the requester is not listening")
    args = parser.parse_args(argv)

    if not is_valid_nonce(args.nonce):
        print(f"[Agent Pair IPC] Error: invalid request ID '{args.nonce}'.", file=sys.stderr)
        return EXIT_ERROR

    text, _ = resolve_message_payload(msg_flag=args.msg_flag, file_path=args.file_path)
    if not text:
        print("[Agent Pair IPC] Error: empty reply. Provide -f <file>, -f - (stdin) or -m <text>.", file=sys.stderr)
        return EXIT_ERROR

    kind = "progress" if args.progress else "reply"
    try:
        channel = send_reply(args.nonce, text, kind=kind, sender=args.sender, allow_mailbox=not args.no_mailbox)
    except ReplyRejectedError as e:
        print(f"[Agent Pair IPC] Reply rejected by requester: {e}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as e:
        print(f"[Agent Pair IPC] Failed to send reply: {e}", file=sys.stderr)
        return EXIT_ERROR

    if channel == "socket":
        print(f"[Agent Pair IPC] {'Progress' if args.progress else 'Reply'} delivered to requester (Req-ID: {args.nonce}).")
        return EXIT_SIGN_OFF
    if channel == "mailbox":
        print(f"[Agent Pair IPC] Requester is not listening; reply stored in its mailbox (Req-ID: {args.nonce}).")
        print(f"[Agent Pair IPC] The requester can collect it with: agent-pair-ipc inbox {args.nonce}")
        return EXIT_SIGN_OFF
    print(f"[Agent Pair IPC] Requester is not listening; {'progress update dropped' if args.progress else 'reply not stored (--no-mailbox)'}.", file=sys.stderr)
    return EXIT_ERROR


def inbox_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-pair-ipc inbox",
        description="List or read peer replies that arrived after the requester stopped waiting.",
    )
    parser.add_argument("nonce", nargs="?", default=None, help="Request ID to read (omit to list all stored replies)")
    parser.add_argument("--keep", action="store_true", help="Do not remove the reply from the mailbox after reading")
    args = parser.parse_args(argv)

    if not args.nonce:
        items = list_reply_mailbox()
        if not items:
            print("Mailbox is empty.")
            return EXIT_SIGN_OFF
        print(f"Found {len(items)} stored peer repl{'y' if len(items) == 1 else 'ies'}:\n")
        for d in items:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.get("received_at") or 0))
            first = (str(d.get("content") or "").strip().splitlines() or [""])[0]
            print(f"  {d.get('correlation_id'):<20} {ts}  from {d.get('sender') or '?'}: {first[:70]}")
        return EXIT_SIGN_OFF

    data = read_reply_mailbox(args.nonce, consume=not args.keep)
    if not data:
        print(f"[Agent Pair IPC] No stored reply for '{args.nonce}'.", file=sys.stderr)
        return EXIT_ERROR
    text = str(data.get("content") or "")
    print(f"=== [Peer Reply from {data.get('sender') or 'peer'} | Req-ID: {args.nonce}] ===")
    print(text)
    return _verdict_exit_code(text)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="agent-pair-ipc",
        description="Zero-dependency Local IPC & Pair-Review Toolkit for Claude Code, Codex, and Antigravity.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Subcommands:
  claude      Interact with local active Claude Code sessions
  codex       Interact with local active Codex sessions
  list        List all active sessions across Claude Code and Codex
  reply       (peer side) Answer a request over the duplex reply channel
  inbox       (requester side) Read replies that arrived after waiting ended

Examples:
  agent-pair-ipc list
  agent-pair-ipc claude -w -f ./REVIEW_REQUEST.md
  agent-pair-ipc codex -w -f - <<'AP_EOF'
  ...rich markdown with `backticks` and $VARS...
  AP_EOF
  agent-pair-ipc reply --to req_0123456789ab -f answer.md
  agent-pair-ipc inbox
"""
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="subcommand", title="subcommands")
    for name, desc in (
        ("claude", "Interact with active Claude Code sessions."),
        ("codex", "Interact with active Codex sessions."),
        ("reply", "Answer a request over the duplex reply channel."),
        ("inbox", "Read peer replies stored in the mailbox."),
    ):
        subparsers.add_parser(name, add_help=False, description=desc)
    subparsers.add_parser("list", description="List all active Claude Code and Codex sessions.")

    if not argv:
        parser.print_help()
        return 0

    first_arg = argv[0]
    if first_arg in ("-v", "--version"):
        parser.parse_args(argv)
        return 0

    if first_arg == "list":
        print("=== Claude Code Sessions ===")
        claude.list_sessions()
        print("\n" + "=" * 40 + "\n")
        print("=== Codex Sessions ===")
        codex.list_sessions()
        return 0

    if first_arg == "claude":
        return claude.main(argv[1:])

    if first_arg == "codex":
        return codex.main(argv[1:])

    if first_arg == "reply":
        return reply_main(argv[1:])

    if first_arg == "inbox":
        return inbox_main(argv[1:])

    # Default to top parser
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
