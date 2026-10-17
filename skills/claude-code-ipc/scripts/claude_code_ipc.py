#!/usr/bin/env python3
"""Skill entry point: thin shim over the agent-pair-ipc package (no logic lives here)."""
import os
import sys

# skills/<skill>/scripts/<this file> -> repo root; realpath so it also works through symlinked skill dirs
repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
if os.path.isdir(os.path.join(repo_root, "agent_pair_ipc")) and repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from agent_pair_ipc.claude import main

if __name__ == "__main__":
    sys.exit(main())
