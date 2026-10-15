#!/usr/bin/env bash
# Example script: Run tests, compose review package, dispatch to Claude Code or Codex, and evaluate verdict.

set -euo pipefail

TARGET_AGENT="${1:-claude}" # 'claude' or 'codex'
TARGET_SESSION="${2:-}"     # Optional session identifier
REPORT_FILE="docs/reports/LATEST_REVIEW.md"

echo "==> [1/3] Running local unit tests..."
if command -v pytest >/dev/null 2>&1; then
    TEST_OUTPUT=$(pytest -v 2>&1)
else
    TEST_OUTPUT=$(python3 -m unittest discover -s tests -v 2>&1)
fi

echo "==> [2/3] Composing review request package..."
mkdir -p "$(dirname "$REPORT_FILE")"

cat <<EOF > "$REPORT_FILE"
# Phase Delivery & Verification Request

## 1. Summary
Automated review handoff generated at $(date).

## 2. Test Execution Output
\`\`\`text
${TEST_OUTPUT}
\`\`\`

## 3. Review Instructions
Please independently inspect the recent git diff and re-run tests.
Provide your verdict in the standard format:
\`VERDICT: SIGN_OFF\` or \`VERDICT: CHANGES_REQUESTED\`
followed by detailed P0-P3 feedback.
EOF

echo "==> [3/3] Delivering to ${TARGET_AGENT} and waiting for verdict..."
CMD=(agent-pair-ipc "${TARGET_AGENT}" -w -f "$REPORT_FILE")
if [ -n "$TARGET_SESSION" ]; then
    CMD+=(-s "$TARGET_SESSION")
fi

set +e
"${CMD[@]}"
EXIT_CODE=$?
set -e

if [ $EXIT_CODE -eq 0 ]; then
    echo "==> [SUCCESS] Review passed! Signed off by ${TARGET_AGENT}."
elif [ $EXIT_CODE -eq 2 ]; then
    echo "==> [CHANGES REQUESTED] ${TARGET_AGENT} requested changes. Please fix issues and re-submit."
    exit 2
else
    echo "==> [ERROR] Review failed or timed out (Exit: ${EXIT_CODE})."
    exit $EXIT_CODE
fi
