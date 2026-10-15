"""
Unified Command Line Interface for Agent Pair IPC.
Supports subcommands for Claude Code and Codex, plus overall session discovery.
"""

from __future__ import annotations

import argparse
import sys

from agent_pair_ipc import __version__
from agent_pair_ipc import claude, codex


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

Examples:
  agent-pair-ipc list
  agent-pair-ipc claude -w -f ./REVIEW_REQUEST.md
  agent-pair-ipc codex -w -f ./REVIEW_REQUEST.md
  agent-pair-ipc claude -r
"""
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="subcommand", title="subcommands")

    # claude subcommand
    p_claude = subparsers.add_parser(
        "claude",
        add_help=False,
        description="Interact with active Claude Code sessions.",
    )
    # codex subcommand
    p_codex = subparsers.add_parser(
        "codex",
        add_help=False,
        description="Interact with active Codex sessions.",
    )
    # list subcommand
    subparsers.add_parser(
        "list",
        description="List all active Claude Code and Codex sessions.",
    )

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

    # Default to top parser
    parser.parse_args(argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
