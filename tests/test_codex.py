"""
Unit tests for agent_pair_ipc.codex
"""

import json
import tempfile
import unittest
from pathlib import Path
from agent_pair_ipc.codex import read_transcript_messages


class TestCodex(unittest.TestCase):
    def test_codex_read_transcript_messages(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tf:
            tf_path = tf.name
            tf.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp/test"}}) + "\n")
            tf.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "bash"}}) + "\n")
            tf.write(
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-10-15T12:00:00Z",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn_123",
                        "last_agent_message": "VERDICT: SIGN_OFF\nAll gate tests verified.",
                    },
                })
                + "\n"
            )

        try:
            turns = read_transcript_messages(tf_path, last_n=5, only_final=True)
            self.assertEqual(len(turns), 1)
            self.assertIn("VERDICT: SIGN_OFF", turns[0]["text"])
            self.assertEqual(turns[0]["turn_id"], "turn_123")
        finally:
            Path(tf_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
