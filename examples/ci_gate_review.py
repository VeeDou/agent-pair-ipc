"""
Example Python script demonstrating programmatic usage of agent-pair-ipc.
Can be integrated into custom CI runners or pre-commit hooks.
"""

from __future__ import annotations

import subprocess
import sys
from agent_pair_ipc import send_and_wait_claude


def run_dual_agent_check():
    # Dynamically run test suite
    test_proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        capture_output=True,
        text=True,
    )
    if test_proc.returncode != 0:
        print("[!] Local unit tests failed. Aborting agent review.", file=sys.stderr)
        print(test_proc.stderr, file=sys.stderr)
        sys.exit(1)

    report_content = f"""
# Automated Gate Verification

Local unit tests passed successfully.
Test output:
```text
{test_proc.stderr.strip() or test_proc.stdout.strip()}
```

Please inspect recent commit changes and confirm architectural alignment.
Format your conclusion starting with `VERDICT: SIGN_OFF` or `VERDICT: CHANGES_REQUESTED`.
"""
    print("[1] Handing off review to Claude Code...")
    response, verdict = send_and_wait_claude(report_content, timeout=180.0)

    if response:
        print("\n--- Claude Code Response ---")
        print(response)
        if verdict == "SIGN_OFF":
            print("\n==> [PASS] Gate verified and signed off by peer agent.")
            sys.exit(0)
        else:
            print("\n==> [FAIL] Peer agent requested changes or did not sign off.")
            sys.exit(2)
    else:
        print("[!] Claude Code review timed out or failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run_dual_agent_check()
