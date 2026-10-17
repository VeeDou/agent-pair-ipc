"""Allow `python -m agent_pair_ipc ...` (used by the reply command advertised in Peer Envelopes)."""

import sys

from agent_pair_ipc.cli import main

if __name__ == "__main__":
    sys.exit(main())
